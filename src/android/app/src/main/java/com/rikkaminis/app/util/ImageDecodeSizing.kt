package com.rikkaminis.app.util

/** Integer width/height pair. Decode arithmetic stays free of `android.util.Size`. */
data class PixelSize(val width: Int, val height: Int)

/**
 * [audit2-PERF-03] Pure decode-sizing arithmetic for every path that has to
 * bound a decoded bitmap's longest edge.
 *
 * Why this lives here: the `read_image` / attachment paths used to
 * `BitmapFactory.decode*()` at FULL resolution and shrink afterwards, so a
 * 12 Mpx phone photo allocated ~48 MB (8000x6000 -> ~192 MB) before any
 * down-scaling — the transient allocation is what OOMs, not the kept bitmap.
 * The bounded alternative is a two-pass decode (`inJustDecodeBounds` ->
 * sampling -> real decode), and the arithmetic that drives it is plain integer
 * math, so it is unit-testable without a decoder.
 *
 * ## Two sampling windows, two contracts — do not mix them
 *
 * - [sampleSizeFor]        decoded bitmap IS the final bitmap  -> window `<= maxEdge`
 * - [sampleSizeForExactScale] a [scaledSize] pass follows     -> window `< 2 * maxEdge`
 *
 * The second window exists to PRESERVE the output resolution of the old
 * single-pass resize: sampling down to `<= maxEdge` and then never scaling back
 * up silently shrinks every image whose longest edge is not a power-of-two
 * multiple of the target (a 1284x2778 screenshot came out 642x1389 instead of
 * 924x2000 — 48% of the pre-fix pixels). A window of `< 2 * maxEdge` keeps one
 * more power of two of detail alive so the exact pass lands on the old edge.
 * The price is a decode bound of `(2 * maxEdge)^2` pixels instead of
 * `maxEdge^2`: 64 MB instead of 16 MB at maxEdge = 2000, still bounded, and no
 * worse than the pre-fix peak for the same band.
 *
 * Mixing them is a silent failure in both directions: the exact-scale window on
 * a caller that has no [scaledSize] pass yields oversized bitmaps (display
 * thumbnails grow to 4x the pixels — `DisplayBitmapLimitsTest` pins that), and
 * the bounded window on a caller that does normalise silently loses resolution.
 *
 * No Android types in this file on purpose — it is compiled and exercised by a
 * plain JVM probe.
 */
object ImageDecodeSizing {

    /**
     * True when `width x height` already fits inside [maxEdge] on both axes,
     * i.e. no decode-time bound is needed. Callers use this to skip the decode
     * entirely (they fall back to the unmodified source bytes).
     */
    fun fitsWithin(width: Int, height: Int, maxEdge: Int): Boolean =
        width in 1..maxEdge && height in 1..maxEdge

    /**
     * Power-of-two `inSampleSize` for callers whose DECODED bitmap is the final
     * bitmap: `max(width, height) / sample <= maxEdge` always holds afterwards.
     *
     * Use this when nothing will resize the bitmap again — the display-side
     * thumbnail guard (`DisplayBitmapLimits`) relies on the hard bound to keep
     * native memory at ~1 MB per thumbnail. Pair it with
     * [sampleSizeForExactScale] only if a [scaledSize] pass follows.
     *
     * Returns 1 for images already within the bound (never upsamples).
     */
    fun sampleSizeFor(width: Int, height: Int, maxEdge: Int): Int {
        if (width <= 0 || height <= 0 || maxEdge <= 0) return 1
        var sample = 1
        while (width / sample > maxEdge || height / sample > maxEdge) sample *= 2
        return sample
    }

    /**
     * Power-of-two `inSampleSize` for callers that normalise the exact edge
     * afterwards with [scaledSize]: the decoded bitmap stays inside
     * `< 2 * maxEdge`, which is the smallest power-of-two division that does not
     * throw away resolution the old single-pass resize would have kept.
     *
     * The caller MUST run [scaledSize] on the decoded dimensions — this window
     * deliberately allows the decoded bitmap to be up to twice the target.
     *
     * Returns 1 for images already inside the window (never upsamples).
     */
    fun sampleSizeForExactScale(width: Int, height: Int, maxEdge: Int): Int {
        if (width <= 0 || height <= 0 || maxEdge <= 0) return 1
        val ceiling = maxEdge * 2
        var sample = 1
        while (width / sample > ceiling || height / sample > ceiling) sample *= 2
        return sample
    }

    /**
     * Target size for `width x height` with its longest edge capped at
     * [maxEdge], preserving the aspect ratio. This is the formula the three
     * audited call sites used inline before the fix, so applying it to the
     * SOURCE dimensions reproduces the old output size exactly — the decoded
     * dimensions are already rounded by `inSampleSize` and would drift by a
     * pixel (12345x6789 -> 1999x1100 instead of 2000x1099).
     *
     * [coerceAtLeast] is load-bearing, not defensive noise: an extremely
     * elongated image (e.g. 4000x1) scales its SHORT edge by the ratio of the
     * LONG edge, so `(1 * 0.5f).toInt()` is `0` — and
     * `Bitmap.createScaledBitmap` throws `IllegalArgumentException` unless both
     * dimensions are > 0, which turned every such `read_image` call into
     * "Error reading image: width and height must be > 0".
     *
     * Never upscales: an image already inside the bound is returned unchanged.
     * A degenerate `width`/`height` (<= 0, which a failed bounds probe can
     * produce) collapses to 1x1 rather than 0.
     */
    fun scaledSize(width: Int, height: Int, maxEdge: Int): PixelSize {
        if (width <= 0 || height <= 0) return PixelSize(1, 1)
        if (maxEdge <= 0) return PixelSize(width, height)
        val longest = maxOf(width, height)
        if (longest <= maxEdge) return PixelSize(width, height)
        val scale = maxEdge.toFloat() / longest
        return PixelSize(
            (width * scale).toInt().coerceAtLeast(1),
            (height * scale).toInt().coerceAtLeast(1),
        )
    }
}
