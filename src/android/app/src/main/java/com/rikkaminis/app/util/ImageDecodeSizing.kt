package com.rikkaminis.app.util

/** Integer width/height pair. Decode arithmetic stays free of `android.util.Size`. */
data class PixelSize(val width: Int, val height: Int)

/**
 * [audit2-PERF-03] Pure decode-sizing arithmetic for every path that has to
 * bound a decoded bitmap's longest edge.
 *
 * Why this lives here: the three `read_image` / attachment paths used to
 * `BitmapFactory.decode*()` at FULL resolution and shrink afterwards, so a
 * 12 Mpx phone photo allocated ~48 MB (8000x6000 -> ~192 MB) before any
 * down-scaling — the transient allocation is what OOMs, not the kept bitmap.
 * The bounded alternative is a two-pass decode (`inJustDecodeBounds` ->
 * [sampleSizeFor] -> real decode), and the arithmetic that drives it is plain
 * integer math, so it is unit-testable without a decoder (see
 * `DisplayBitmapLimits` for the display-side consumer).
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
     * Power-of-two `inSampleSize` that brings `max(width, height)` to at most
     * [maxEdge]. Pure arithmetic, so it is unit-testable without a decoder.
     * Returns 1 for images already within the bound (never upsamples).
     */
    fun sampleSizeFor(width: Int, height: Int, maxEdge: Int): Int {
        if (width <= 0 || height <= 0 || maxEdge <= 0) return 1
        var sample = 1
        while (width / sample > maxEdge || height / sample > maxEdge) sample *= 2
        return sample
    }

    /**
     * Target size for `width x height` with its longest edge capped at
     * [maxEdge], preserving the aspect ratio.
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
