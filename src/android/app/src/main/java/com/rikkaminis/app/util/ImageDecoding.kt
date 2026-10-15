package com.rikkaminis.app.util

import android.graphics.Bitmap
import android.graphics.BitmapFactory

/**
 * A decoded bitmap plus the dimensions of the SOURCE it was decoded from.
 *
 * The two are not the same once sampling kicks in, and callers need both:
 * `read_image` reports the source dimensions in its metadata string while
 * sending the (possibly sampled) pixels to the model.
 */
data class DecodedImage(val bitmap: Bitmap, val sourceWidth: Int, val sourceHeight: Int)

/**
 * [audit2-PERF-03] The single bounded-decode entry point for the tool /
 * attachment paths (`tools.ReadImageTool`, `ui.chat.ChatPromptAndTools`).
 *
 * Every one of them used to call `BitmapFactory.decodeFile` /
 * `decodeByteArray` with no `Options`, i.e. it materialised the FULL-resolution
 * bitmap first and shrank it afterwards. A 4000x3000 (12 Mpx) photo is ~48 MB
 * of ARGB_8888 at peak, 8000x6000 is ~192 MB; against the 256-512 MB heap
 * ceiling that transient allocation is exactly the `Failed to allocate` /
 * native-abort shape. `inJustDecodeBounds` reads only the header, so
 * [ImageDecodeSizing.sampleSizeForExactScale] can pick a power-of-two
 * `inSampleSize` and the oversized bitmap is never allocated: the decode is
 * bounded at `(2 * maxEdge)^2` pixels (16 Mpx / 64 MB at maxEdge = 2000)
 * whatever the source size, and the [shrinkToBound] pass then lands on exactly
 * the size the old single-pass resize produced.
 *
 * This mirrors `provider.ImageBudget.compressBytes` on the network path and
 * `ui.DisplayBitmapLimits.decodeFileBounded` on the display path; the three
 * audited call sites now share THIS one instead of each rolling their own.
 *
 * ponytail: sampling window is `< 2 * maxEdge` and the exact pass never scales
 * back UP | 天花板: the decode bound is 64 MB rather than 16 MB at maxEdge =
 * 2000, and an image whose longest edge sits in `(maxEdge, 2 * maxEdge]` is
 * decoded at source resolution (that band cost the same before the fix, so it
 * is not a regression — just not a reduction) | 升级触发: a `Failed to
 * allocate` / OOM lands on that band — then decode straight to the target with
 * `ImageDecoder.setTargetSize` (API 28+, one allocation at the final size, no
 * window compromise) and keep this two-pass as the API 26-27 fallback.
 * (Do NOT "fix" it by lowering the window to `<= maxEdge` alone: that is what
 * silently cost 50%+ of the output resolution in the first cut.)
 *
 * Error contract: every entry point returns null on any failure (undecodable header,
 * decoder returning null, `OutOfMemoryError` from the decoder itself) — the
 * same "null means caller keeps what it had" contract the callers had.
 */
object ImageDecoding {

    /** Header-only probe of an encoded byte array. Null when undecodable. */
    fun probeBytes(data: ByteArray): PixelSize? = try {
        if (data.isEmpty()) {
            null
        } else {
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeByteArray(data, 0, data.size, bounds)
            sizeOrNull(bounds)
        }
    } catch (_: Throwable) {
        null
    }

    /** Header-only probe of a file. Null when undecodable. */
    fun probeFile(path: String): PixelSize? = try {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(path, bounds)
        sizeOrNull(bounds)
    } catch (_: Throwable) {
        null
    }

    /**
     * Two-pass decode of [path]: bounds probe, sampled decode, then an exact
     * shrink to the target computed from the SOURCE dimensions (the same
     * inputs the removed single-pass resize used, so the output size is
     * pixel-identical to it and decoder rounding — decoders return
     * `ceil(source / inSampleSize)` — cannot shift the output by even 1 px).
     *
     * Pass [source] when the caller already probed (e.g. to decide whether the
     * image needs bounding at all) to skip a second header read.
     */
    fun decodeFileBounded(path: String, maxEdge: Int, source: PixelSize? = null): DecodedImage? = try {
        val src = source ?: probeFile(path)
        if (src == null) {
            null
        } else {
            val opts = BitmapFactory.Options().apply {
                inSampleSize = ImageDecodeSizing.sampleSizeForExactScale(src.width, src.height, maxEdge)
            }
            BitmapFactory.decodeFile(path, opts)?.let {
                DecodedImage(shrinkToBound(it, src.width, src.height, maxEdge), src.width, src.height)
            }
        }
    } catch (_: Throwable) {
        null
    }

    /** Byte-array twin of [decodeFileBounded]. */
    fun decodeBytesBounded(data: ByteArray, maxEdge: Int, source: PixelSize? = null): DecodedImage? = try {
        val src = source ?: probeBytes(data)
        if (src == null || data.isEmpty()) {
            null
        } else {
            val opts = BitmapFactory.Options().apply {
                inSampleSize = ImageDecodeSizing.sampleSizeForExactScale(src.width, src.height, maxEdge)
            }
            BitmapFactory.decodeByteArray(data, 0, data.size, opts)?.let {
                DecodedImage(shrinkToBound(it, src.width, src.height, maxEdge), src.width, src.height)
            }
        }
    } catch (_: Throwable) {
        null
    }

    private fun sizeOrNull(options: BitmapFactory.Options): PixelSize? =
        if (options.outWidth > 0 && options.outHeight > 0) {
            PixelSize(options.outWidth, options.outHeight)
        } else {
            null
        }

    /**
     * Exact-bound pass. Normally a no-op (the window in
     * [ImageDecodeSizing.sampleSizeForExactScale] keeps the sampled bitmap at
     * or above the target); it exists because a decoder rounds `source /
     * inSampleSize` UP, so the sampled bitmap can land a pixel or two above the
     * target, and because the target has to be the size the removed
     * single-pass resize produced.
     *
     * The target is computed from the SOURCE dimensions — the very inputs the
     * old inline `createScaledBitmap(bitmap, w * scale, h * scale)` used — so
     * the output is pixel-identical to the pre-fix resize and decoder rounding
     * cannot shift it. Deriving the target from `bitmap.width/height` instead
     * (an earlier cut of this fix did) applies the same ratio to
     * already-rounded inputs and drifts by 1 px (12345x6789: 2000x1099 became
     * 1999x1100).
     *
     * Recycles the intermediate so the owner only ever recycles one bitmap.
     */
    private fun shrinkToBound(bitmap: Bitmap, sourceWidth: Int, sourceHeight: Int, maxEdge: Int): Bitmap {
        val target = ImageDecodeSizing.scaledSize(sourceWidth, sourceHeight, maxEdge)
        if (target.width == bitmap.width && target.height == bitmap.height) return bitmap
        // Never upscale. The sampling window above guarantees the decoded
        // bitmap is at least as large as the target, so this branch only fires
        // when the bounds header disagreed with what the decoder actually
        // handed back — in which case returning the real pixels beats inventing
        // them, and the shrunken output keeps the regression visible instead of
        // hiding it behind a right-sized blur.
        if (bitmap.width <= target.width && bitmap.height <= target.height) return bitmap
        return try {
            val scaled = Bitmap.createScaledBitmap(bitmap, target.width, target.height, true)
            if (scaled !== bitmap) bitmap.recycle()
            scaled
        } catch (t: Throwable) {
            // Never leak the sampled bitmap on the error path (native memory),
            // then let the caller's catch turn it into the null contract.
            bitmap.recycle()
            throw t
        }
    }
}
