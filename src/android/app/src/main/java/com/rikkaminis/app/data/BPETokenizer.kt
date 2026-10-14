package com.rikkaminis.app.data

import android.content.Context
import android.graphics.BitmapFactory
import kotlin.math.ceil
import kotlin.math.max
import kotlin.math.round

/**
 * Approximate BPE token counter. Mirrors iOS `BPETokenizer` (cl100k_base)
 * but without the full vocabulary — we ship a lightweight heuristic by
 * default so the app size doesn't balloon by ~2 MB before a caller actually
 * needs tokenization. [loadVocabularyFromAssets] is the opt-in higher-fidelity
 * path.
 *
 * [F-230] Note: no `cl100k_base.tiktoken` asset ships in this repo and nothing
 * calls [loadVocabularyFromAssets] today, so the heuristic below is always the
 * live path. The loader is kept because it is the documented upgrade route —
 * but the class KDoc previously described it as though it were wired up.
 *
 * The heuristic is a per-character-class weighted estimate
 * (`0.28·letters + 0.35·digits + 0.5·punctuation + 0.2·whitespace + 1.0·non-ASCII`).
 *
 * [F-230] Accuracy, measured against real `cl100k_base` (tiktoken 0.14.0, 18
 * samples) — the previous claim of "within ±15%" was false in both directions:
 *
 *   | content        | old `cp/3` | this estimator |
 *   |----------------|-----------|----------------|
 *   | English prose  |   +53…+80%|      +24…+47%  |
 *   | CJK prose      |   −63…−68%|       −12…+5%  |
 *   | code / JSON    |   −16…+31%|      −27…+40%  |
 *   | base64-ish     |       −56%|           −59% |
 *
 * Worst case ≈ ±60%; typical mixed prose/JSON is within ~±25%. The `cp/3` form
 * was systematically wrong for CJK (every CJK codepoint is ~1 token, not ⅓),
 * so Chinese text was under-counted by ~3× — the direction that silently
 * overruns a context window. The class-weighted form removes that bias
 * (under-counting cases dropped from 11/18 to 8/18 on the same corpus). No
 * character-class heuristic reaches ±15% — token-dense payloads like base64 or
 * minified JSON are not predictable from character counts at all — so treat
 * this as a budget estimate, not a count. For real fidelity, load the actual
 * vocabulary via [loadVocabularyFromAssets].
 *
 * Image tokens: iOS uses `ceil(w/32) * ceil(h/32)` with a 2048px long-edge
 * cap and a floor of 85 tokens. We match that exactly so the context bar
 * stays visually identical across platforms.
 *
 * This class is thread-safe once [loadVocabularyFromAssets] finishes (or if
 * the heuristic path is used). Vocabulary loading is one-shot via [@Volatile].
 */
object BPETokenizer {
    const val TOKENS_PER_MESSAGE = 3
    const val TOKENS_PER_REPLY = 3
    private const val IMAGE_GRID = 32
    private const val IMAGE_MAX_EDGE = 2048
    private const val IMAGE_MIN_TOKENS = 85
    private const val IMAGE_FAILURE_FALLBACK = 1000

    // [F-230] Per-character-class token weights, calibrated against real
    // cl100k_base counts (tiktoken 0.14.0). See heuristicTokenCount + class KDoc.
    private const val W_LETTER = 0.28
    private const val W_DIGIT = 0.35
    private const val W_PUNCT = 0.5
    private const val W_SPACE = 0.2
    private const val W_NON_ASCII = 1.0

    @Volatile private var encoder: Map<List<Byte>, Int>? = null

    /**
     * Count tokens in [text] using the loaded vocabulary if present, otherwise
     * a codepoint-based heuristic. Returns 0 for empty input.
     */
    fun countTokens(text: String): Int {
        if (text.isEmpty()) return 0
        val vocab = encoder
        if (vocab == null) return heuristicTokenCount(text)
        return bpeEncode(text.toByteArray(Charsets.UTF_8), vocab)
    }

    /**
     * Approximate token count for an image payload. Matches iOS heuristic:
     * scale long-edge to 2048px, grid at 32×32, min 85 tokens, return 1000
     * if decoding fails (i.e. treat as a costly opaque blob).
     */
    fun countImageTokens(data: ByteArray): Int {
        if (data.isEmpty()) return 0
        val opts = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(data, 0, data.size, opts)
        val w = opts.outWidth.toFloat()
        val h = opts.outHeight.toFloat()
        if (w <= 0f || h <= 0f) return IMAGE_FAILURE_FALLBACK
        val longEdge = max(w, h)
        val scale = if (longEdge > IMAGE_MAX_EDGE) IMAGE_MAX_EDGE / longEdge else 1f
        val scaledW = w * scale
        val scaledH = h * scale
        val tokens = (ceil(scaledW / IMAGE_GRID) * ceil(scaledH / IMAGE_GRID)).toInt()
        return max(IMAGE_MIN_TOKENS, tokens)
    }

    /**
     * Opt-in: load a cl100k_base vocabulary from `assets/<fileName>`. File
     * must match tiktoken's format — one `<base64-bytes> <rank>` per line.
     * No-op if already loaded or file is missing.
     */
    fun loadVocabularyFromAssets(context: Context, fileName: String = "cl100k_base.tiktoken"): Boolean {
        if (encoder != null) return true
        return try {
            val dict = HashMap<List<Byte>, Int>(100_300)
            context.assets.open(fileName).bufferedReader(Charsets.UTF_8).useLines { lines ->
                for (line in lines) {
                    val space = line.indexOf(' ')
                    if (space <= 0) continue
                    val decoded = runCatching {
                        android.util.Base64.decode(
                            line.substring(0, space),
                            android.util.Base64.DEFAULT or android.util.Base64.NO_WRAP,
                        )
                    }.getOrNull() ?: continue
                    val rank = line.substring(space + 1).trim().toIntOrNull() ?: continue
                    dict[decoded.toList()] = rank
                }
            }
            if (dict.isNotEmpty()) {
                encoder = dict
                true
            } else false
        } catch (_: Exception) {
            false
        }
    }

    /**
     * [F-230] Character-class weighted estimate. BPE merges text into
     * sub-word units, so the token density of a character depends on its
     * class: an ASCII letter or digit is usually part of a merged word
     * (~0.3 token each), punctuation tends to stand alone (~0.5), runs of
     * whitespace collapse (~0.2), and a non-ASCII codepoint is typically its
     * own token (~1.0). The previous flat `codepointCount / 3` assumed the
     * English density for everything, which under-counted CJK by ~3x.
     *
     * Weights are least-squares calibrated against real cl100k_base counts;
     * see the class KDoc for measured accuracy and its limits.
     */
    private fun heuristicTokenCount(text: String): Int {
        var letters = 0
        var digits = 0
        var punct = 0
        var spaces = 0
        var nonAscii = 0
        var i = 0
        while (i < text.length) {
            val c = text[i]
            val code = c.code
            // Surrogate pair => one non-ASCII codepoint (emoji, CJK ext, …).
            if (Character.isHighSurrogate(c) && i + 1 < text.length) {
                nonAscii++
                i += 2
                continue
            }
            when {
                code >= 128 -> nonAscii++
                c.isLetter() -> letters++
                c.isDigit() -> digits++
                c.isWhitespace() -> spaces++
                else -> punct++
            }
            i++
        }
        val estimate = W_LETTER * letters + W_DIGIT * digits +
            W_PUNCT * punct + W_SPACE * spaces + W_NON_ASCII * nonAscii
        return max(1, round(estimate).toInt())
    }

    /**
     * Greedy BPE: treat each byte as its own segment, repeatedly merge the
     * adjacent pair with the lowest rank that's still in the vocabulary, stop
     * when no mergeable pair remains. Mirrors iOS `bytePairEncode`. Returns
     * the token COUNT (one per surviving segment).
     *
     * [sweep1014-P4a] Counting semantics: segments whose bytes are not in the
     * vocabulary were previously dropped silently by a trailing `mapNotNull`
     * — under-counting exactly the inputs the loaded vocabulary does not
     * cover. Unknown segments now contribute 1 token each (one token per
     * unknown unit: conservative, errs toward over-counting rather than
     * hiding bytes). Registered here rather than logged — an unknown segment
     * means the loaded vocabulary is incomplete, and the count staying
     * monotone in the input length is the invariant callers rely on.
     *
     * [sweep1014-P4a] Performance: the previous implementation re-sliced +
     * re-listed both segments of EVERY adjacent pair on EVERY scan pass —
     * O(N²) byte-array copies and object allocations per call (a 10 KB
     * message = ~10⁸ allocations). This version keeps (start, end) index
     * ranges, merges via a doubly linked list, and pushes each pair into a
     * rank-ordered min-heap once per content change: initial N−1 lookups +
     * 2 lookups per merge = O(N) vocabulary lookups total (each allocating
     * one `List<Byte>` key), instead of O(N²) copy+list per pass.
     *
     * Tie-breaking matches the previous left-to-right scan: the min-heap is
     * ordered by (rank, node index), so equal ranks pop leftmost-first, the
     * same pair a full rescan would have picked (`if (rank < minRank)` keeps
     * the first minimum encountered). Stale heap entries (pairs whose bytes
     * changed since they were pushed) are discarded on pop by recomputing the
     * pair's current rank: every content change re-offers the affected pairs,
     * so a fresh entry for the current state is always present.
     */
    private fun bpeEncode(bytes: ByteArray, vocab: Map<List<Byte>, Int>): Int {
        if (bytes.isEmpty()) return 0
        val n = bytes.size
        // Segment boundaries as index ranges — no per-segment byte copies.
        val segStart = IntArray(n) { it }
        val segEnd = IntArray(n) { it + 1 }
        // Doubly linked list over segment nodes; node 0 is always the head
        // (merges fold right-hand segments into their left neighbor).
        val prev = IntArray(n) { it - 1 }
        val next = IntArray(n) { if (it + 1 < n) it + 1 else -1 }

        fun lookup(from: Int, until: Int): Int? = vocab[byteSliceList(bytes, from, until)]
        // Heap entry packs (rank, nodeIdx): negative ranks order first (they
        // sort below any non-negative rank), equal ranks tie-break by node
        // index = leftmost, matching the old full-rescan pick.
        fun pack(rank: Int, idx: Int): Long = (rank.toLong() shl 32) or idx.toLong()
        val heap = java.util.PriorityQueue<Long>(n)
        for (i in 0 until n - 1) {
            lookup(segStart[i], segEnd[i + 1])?.let { heap.add(pack(it, i)) }
        }

        while (heap.isNotEmpty()) {
            val entry = heap.poll()
            val i = (entry and 0xFFFFFFFFL).toInt()
            val j = next[i]
            if (j < 0) continue // merged away / no right neighbor
            // Stale check: recompute the pair's current rank. Equal means the
            // entry still describes this pair's current bytes.
            val cur = lookup(segStart[i], segEnd[j]) ?: continue
            if (cur != (entry shr 32).toInt()) continue
            // Merge j into i.
            segEnd[i] = segEnd[j]
            next[i] = next[j]
            next[j] = -1
            if (next[i] >= 0) prev[next[i]] = i
            // The pairs touching the merged segment changed: re-offer them.
            if (next[i] >= 0) {
                lookup(segStart[i], segEnd[next[i]])?.let { heap.add(pack(it, i)) }
            }
            val p = prev[i]
            if (p >= 0) {
                lookup(segStart[p], segEnd[i])?.let { heap.add(pack(it, p)) }
            }
        }

        // Every surviving segment = 1 token (vocab-covered or not — see KDoc).
        var count = 0
        var i = 0
        while (i >= 0) {
            count++
            i = next[i]
        }
        return count
    }

    /** Vocabulary key for `bytes[from until until]`, without a ByteArray copy. */
    private fun byteSliceList(bytes: ByteArray, from: Int, until: Int): List<Byte> {
        val out = ArrayList<Byte>(until - from)
        for (k in from until until) out.add(bytes[k])
        return out
    }
}
