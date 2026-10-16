package com.rikkaminis.app.conversation

import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage

/**
 * [feat/compact-pin-v0-1005] Compact pin v0 — user verbatim text is pinned
 * out of the LLM rewrite path.
 *
 * Experiment D (compact-exp-1004 `REPORT_DE.md`) proved "covering decay":
 * a ground-truth constraint stated once in R1 is 0/10 recalled in R2 —
 * later same-topic turns overwrite it (recency wins), and no summary-side
 * prompt clause (MUST PRESERVE / SUPERSEDED) changes that, because every
 * summary pass rewrites the whole text through the LLM. Pinning turns
 * "user said" from probabilistic retention into deterministic retention:
 *
 *  1. [extractPinnedUserMessages] mechanically extracts verbatim user text
 *     from the folded region (same range as the fold — not full history);
 *  2. the pinned payload NEVER enters an LLM call: the rewrite chain strips
 *     any existing pinned block from the summary before building the LLM
 *     input ([stripPinnedSection]) and re-appends after the LLM returns
 *     ([appendPinnedSection]);
 *  3. the block is embedded INSIDE the summary string (zero DB/marker
 *     migration) so it rides the existing `<context-summary>` injection
 *     (effectiveAgentHistory) untouched — the model sees the verbatim user
 *     text every turn.
 *
 * Upgrade trigger (task brief §2): if round-trip tests catch the embedded
 * block being rewritten by a later LLM pass and stripping proves
 * unreliable, the block moves to a dedicated DB column — that is a separate
 * task; this file stays the single home of the pin format until then.
 */

/** Token cap for the pinned payload ([ContextCompactor.estimateTokens] same口径). */
const val PINNED_USER_MESSAGES_TOKEN_LIMIT: Long = 8000L

private const val PINNED_OPEN = "<pinned-user-messages>"
private const val PINNED_CLOSE = "</pinned-user-messages>"

// Non-greedy so a hallucinated second CLOSE inside the block cannot swallow
// beyond the first real block; [\s\S] spans newlines (JS-style dot would not).
// Group 1 = the carried payload (header + pinned user messages).
private val PINNED_BLOCK_REGEX =
    Regex("<pinned-user-messages>([\\s\\S]*?)</pinned-user-messages>")

private const val PINNED_HEADER =
    "Verbatim user messages from the earlier (compacted) conversation — " +
        "carried through unchanged, never rewritten by any summary pass."

/**
 * Extract verbatim user text from the folded region [messages], in
 * chronological order.
 *
 * Included: USER messages' [LLMMessage.content] plus any structured
 * [AgentContentPart.Text] parts (both are user-authored text, mirroring
 * what [com.rikkaminis.app.ui.chat.buildConversationTextForSummary] renders
 * as user text), de-duplicated WITHIN each message so the double-filled
 * (content == Text part) shape pins once — see the collection comment below.
 * Excluded: assistant messages and tool-result carriers
 * (USER messages whose contentParts are all ToolResult) — tools/assistant
 * content is the summarizer's job, pinning is only for what the user said.
 *
 * Token cap: [tokenLimit] via [ContextCompactor.estimateTokens]. Under the
 * cap the full set is returned. Over the cap, the most recent messages that
 * fit are kept plus — always — the earliest message (task brief §2
 * 「最近 N 条 + 最早 1 条」): the earliest user message most likely carries
 * the original task statement, which is exactly what the decay experiment
 * showed gets overwritten. A single message larger than the whole cap is
 * still pinned alone (it cannot be split; verbatim is the point).
 *
 * Empty result → callers must leave the summary untouched (byte-identical).
 */
fun extractPinnedUserMessages(
    messages: List<LLMMessage>,
    tokenLimit: Long = PINNED_USER_MESSAGES_TOKEN_LIMIT,
): List<String> {
    val texts = ArrayList<String>()
    for (msg in messages) {
        if (msg.role != LLMMessage.Role.USER) continue
        if (msg.contentParts.isNotEmpty() && msg.contentParts.all { it is AgentContentPart.ToolResult }) {
            continue
        }
        // [fix/da1018-compact-pin] Same-message de-duplication. Every USER
        // message in this app is DOUBLE-FILLED: the main send path puts the
        // trimmed text in BOTH `content` and the first Text part
        // (ChatViewModel:3713-3714), and every engine-side reminder mirrors it
        // (AgentLoopEngine:2205/2713/2780/2844/3105, ChatToolExecutors:413/444,
        // EmptyTurnRecovery:90, SanitizeAgentHistory:136, ChatCompactionLogic
        // :497). Collecting both copies pinned each user line twice, which (a)
        // ate the [PINNED_USER_MESSAGES_TOKEN_LIMIT] budget at 2× the real
        // content, so the over-cap trim dropped older real messages early, and
        // (b) duplicated the text inside the summary string that rides every
        // request. Both sources are still read — parts carry text `content`
        // does not (per-image captions, `[image …]` placeholders, the
        // attached-files XML) — only EXACT duplicates within one message
        // collapse. Insertion order is preserved (content first, then parts),
        // and duplicates ACROSS messages are deliberately left alone: the pin
        // stays a faithful chronological record, not a global de-dup set. This
        // is the third site of the same family: [ContextEstimator
        // .estimateMessageTokensFull] and [buildConversationTextForSummary]
        // were fixed earlier (audit-1015-bug01b).
        val seenInMessage = LinkedHashSet<String>()
        if (msg.content.isNotBlank()) seenInMessage.add(msg.content)
        for (part in msg.contentParts) {
            if (part is AgentContentPart.Text && part.text.isNotBlank()) seenInMessage.add(part.text)
        }
        texts.addAll(seenInMessage)
    }
    if (texts.isEmpty()) return emptyList()

    val total = texts.sumOf { ContextCompactor.estimateTokens(it) }
    if (total <= tokenLimit) return texts

    // Over the cap: walk from the newest, keep what fits.
    val recent = ArrayList<String>()
    var used = 0L
    for (i in texts.indices.reversed()) {
        val t = ContextCompactor.estimateTokens(texts[i])
        if (used + t > tokenLimit) break
        recent.add(0, texts[i])
        used += t
    }
    // Earliest message is pinned unconditionally (see KDoc).
    if (recent.none { it == texts.first() }) {
        recent.add(0, texts.first())
    }
    return recent
}

/**
 * Raw inner text of an existing `<pinned-user-messages>` block inside
 * [summary], or null when there is none.
 *
 * Returns the carried payload verbatim (header + previously pinned user
 * messages), NOT parsed back into individual messages — multi-round
 * rewriting carries the block as opaque text; only the append side knows
 * the message separator. Edge newlines around the payload are trimmed so
 * repeated strip→append cycles cannot accumulate separator drift.
 */
fun pinnedSectionInner(summary: String?): String? {
    if (summary.isNullOrEmpty()) return null
    return PINNED_BLOCK_REGEX.find(summary)?.groupValues?.get(1)?.trim('\n')
}

/**
 * Remove any `<pinned-user-messages>` block from [summary].
 *
 * Idempotent: a summary with no block is returned byte-identical (the
 * rewrite chain relies on this — no-pin sessions must keep today's
 * behavior exactly). When a block is stripped, the `\n\n` separator the
 * append added is dropped with it so the base the LLM sees is the same
 * text it would have seen before pinning existed.
 */
fun stripPinnedSection(summary: String?): String? {
    if (summary.isNullOrEmpty()) return summary
    val stripped = PINNED_BLOCK_REGEX.replace(summary, "")
    if (stripped == summary) return summary // no block — byte-identical no-op
    return stripped.trimEnd()
}

/**
 * Append a `<pinned-user-messages>` block carrying [pinned] (this fold's
 * verbatim user text) plus [carriedInner] (the previous round's payload,
 * opaque). Order: carried first (oldest first), then the new messages.
 *
 * Idempotent: appending the exact block that is already present is a
 * no-op, and a defensive [stripPinnedSection] guarantees a summary can
 * never hold two blocks. Empty [pinned] + null/blank [carriedInner] →
 * [summary] returned byte-identical (the empty-pin contract).
 */
fun appendPinnedSection(
    summary: String,
    pinned: List<String>,
    carriedInner: String? = null,
): String {
    val parts = ArrayList<String>()
    if (!carriedInner.isNullOrBlank()) parts.add(carriedInner)
    for (p in pinned) {
        if (p.isNotBlank()) parts.add(p)
    }
    if (parts.isEmpty()) return summary

    // carriedInner is a previously built payload and already contains the
    // header line — prepend the header ONLY when there is no carried text,
    // so multi-round cycles never duplicate it and the carried payload
    // always stays at the top of the block.
    val inner = if (carriedInner.isNullOrBlank()) {
        (listOf(PINNED_HEADER) + parts).joinToString("\n\n")
    } else {
        parts.joinToString("\n\n")
    }
    val block = PINNED_OPEN + "\n" + inner + "\n" + PINNED_CLOSE
    if (summary.contains(block)) return summary // idempotent
    val base = stripPinnedSection(summary) // defensive: never two blocks
    return if (base.isNullOrBlank()) block else base + "\n\n" + block
}
