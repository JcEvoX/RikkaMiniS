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
 * [ImageDecodeSizing.sampleSizeFor] can pick a power-of-two `inSampleSize`
 * and the oversized bitmap is never allocated at all.
 *
 * This mirrors `provider.ImageBudget.compressBytes` on the network path and
 * `ui.DisplayBitmapLimits.decodeFileBounded` on the display path; the three
 * audited call sites now share THIS one instead of each rolling their own.
 *
 * ponytail: sampling targets `<= maxEdge` and never scales back UP | 天花板: a
 * source whose longest edge is not a power-of-two multiple of maxEdge comes out
 * smaller than the old single-pass scale did (3000x2000 -> 1500x1000 instead of
 * 2000x1333; peak allocation 12 MB -> 6 MB) | 升级触发: a caller needs an exact
 * target edge, or a quality complaint lands on non-2^n-aligned photos — then
 * switch to ImageBudget's `maxEdge * 2` sampling plus one upscale to the target.
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
     * shrink only when the decoded bitmap still exceeds [maxEdge] (decoders
     * round a `/ sampleSize` up, so a 4001-px edge can come back as 2001).
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
                inSampleSize = ImageDecodeSizing.sampleSizeFor(src.width, src.height, maxEdge)
            }
            BitmapFactory.decodeFile(path, opts)?.let {
                DecodedImage(shrinkToBound(it, maxEdge), src.width, src.height)
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
                inSampleSize = ImageDecodeSizing.sampleSizeFor(src.width, src.height, maxEdge)
            }
            BitmapFactory.decodeByteArray(data, 0, data.size, opts)?.let {
                DecodedImage(shrinkToBound(it, maxEdge), src.width, src.height)
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
     * Exact-bound pass. Normally a no-op (`inSampleSize` already brought the
     * longest edge under the bound); it exists for the decoder-rounding case.
     * Recycles the intermediate so the owner only ever recycles one bitmap.
     */
    private fun shrinkToBound(bitmap: Bitmap, maxEdge: Int): Bitmap {
        val target = ImageDecodeSizing.scaledSize(bitmap.width, bitmap.height, maxEdge)
        if (target.width == bitmap.width && target.height == bitmap.height) return bitmap
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
