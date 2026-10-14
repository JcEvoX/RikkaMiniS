package com.rikkaminis.app.ui.chat

/**
 * URI path-semantics percent-decode: decodes `%XX` escapes only and leaves
 * literal `+` intact. [java.net.URLDecoder.decode] follows form-encoding
 * semantics, which corrupts path/filename components containing `+`
 * (`c++_primer.md` → `c  _primer.md`); `android.net.Uri.decode` throws on
 * malformed escapes and is Android-only, so neither fits.
 *
 * Malformed escapes (a bare `%`, e.g. `100%.txt`) are left verbatim instead
 * of throwing — this runs during Compose composition via the chat link
 * render resolver ([ChatLinkResolver]), so a throw here is an app crash (B6).
 *
 * Consecutive `%XX` bytes are collected and decoded as one UTF-8 sequence so
 * multi-byte characters (`%E4%B8%AD` → 中) survive; any byte that fails to
 * form a valid sequence surfaces as U+FFFD in the decoded output.
 *
 * JVM-pure (no Android imports) so it is unit-testable without Robolectric.
 */
internal fun decodeUriPath(input: String): String {
    if ('%' !in input) return input
    val out = StringBuilder(input.length)
    val bytes = ByteArray(input.length)
    var n = 0
    fun flushBytes() {
        if (n > 0) {
            out.append(String(bytes, 0, n, Charsets.UTF_8))
            n = 0
        }
    }
    var i = 0
    while (i < input.length) {
        val c = input[i]
        val h1 = if (i + 2 < input.length) hexValue(input[i + 1]) else -1
        val h2 = if (h1 >= 0) hexValue(input[i + 2]) else -1
        if (c == '%' && h1 >= 0 && h2 >= 0) {
            bytes[n++] = ((h1 shl 4) or h2).toByte()
            i += 3
        } else {
            flushBytes()
            out.append(c)
            i++
        }
    }
    flushBytes()
    return out.toString()
}

private fun hexValue(c: Char): Int = when (c) {
    in '0'..'9' -> c - '0'
    in 'a'..'f' -> c - 'a' + 10
    in 'A'..'F' -> c - 'A' + 10
    else -> -1
}
