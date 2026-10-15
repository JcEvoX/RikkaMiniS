package com.rikkaminis.app.util

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [audit2-PERF-03] Pins the decode-sizing arithmetic that keeps the
 * `read_image` / attachment paths from materialising a full-resolution bitmap.
 *
 * ## What the production pipeline actually does
 *
 * 1. `inJustDecodeBounds` reads the header -> source `w x h`;
 * 2. `inSampleSize = sampleSizeForExactScale(w, h, maxEdge)`;
 * 3. the decoder returns `max(1, w / sample) x max(1, h / sample)`
 *    (Skia rounds the sampled edge UP, so the ceil model below is the
 *    pessimistic one);
 * 4. `shrinkToBound` scales that bitmap to `scaledSize(w, h, maxEdge)` —
 *    the target is computed from the SOURCE dimensions, i.e. the exact inputs
 *    the removed single-pass `createScaledBitmap` used, which is what makes the
 *    output pixel-identical to the pre-fix behaviour (deriving it from the
 *    already-rounded decoded dimensions drifts by 1 px).
 *
 * So the final size is [ImageDecodeSizing.scaledSize] of the source, and the
 * sampling window only decides how much real detail the intermediate keeps
 * (it must never fall BELOW the target, or step 4 would upscale).
 *
 * Expectations are literals on purpose — deriving them from the implementation
 * would let a regression prove itself correct. [preFixSize] is a verbatim copy
 * of the removed single-pass resize, used as the behaviour-parity oracle (its
 * own literals are asserted too, so the oracle itself is pinned).
 */
class ImageDecodeSizingTest {

    private companion object {
        const val MAX_EDGE = 2000

        /** Floor model of the decoder: `BitmapFactory` returning `src / sample`. */
        fun decodedSizeFloor(width: Int, height: Int, maxEdge: Int): PixelSize {
            val sample = ImageDecodeSizing.sampleSizeForExactScale(width, height, maxEdge)
            return PixelSize(maxOf(1, width / sample), maxOf(1, height / sample))
        }

        /** Decoder rounding the sampled edge UP (Skia's behaviour). */
        fun decodedSizeCeil(width: Int, height: Int, maxEdge: Int): PixelSize {
            val sample = ImageDecodeSizing.sampleSizeForExactScale(width, height, maxEdge)
            return PixelSize(
                maxOf(1, (width + sample - 1) / sample),
                maxOf(1, (height + sample - 1) / sample),
            )
        }

        /** Final size of the whole pipeline (step 4 targets the source dims). */
        fun outputSize(width: Int, height: Int, maxEdge: Int): PixelSize =
            ImageDecodeSizing.scaledSize(width, height, maxEdge)

        /**
         * The resize the three audited call sites used to run inline, on the
         * FULL-resolution bitmap: `scale = maxEdge / max(w, h)`. The parity
         * oracle for the new pipeline.
         */
        fun preFixSize(width: Int, height: Int, maxEdge: Int): PixelSize {
            if (width <= maxEdge && height <= maxEdge) return PixelSize(width, height)
            val scale = maxEdge.toFloat() / maxOf(width, height)
            return PixelSize((width * scale).toInt(), (height * scale).toInt())
        }
    }

    @Test
    fun `required sizes keep the pre-fix resolution (literals, not formulas)`() {
        // w, h, sample, expected w, expected h  — every value written out.
        val table = listOf(
            intArrayOf(1284, 2778, 1, 924, 2000),   // this device's screenshots
            intArrayOf(1080, 2400, 1, 900, 2000),   // phone screenshots
            intArrayOf(3000, 2000, 1, 2000, 1333),  // camera portrait
            intArrayOf(4000, 3000, 1, 2000, 1500),  // 12 Mpx
            intArrayOf(8000, 6000, 2, 2000, 1500),  // 48 Mpx
            intArrayOf(4000, 1, 1, 2000, 1),        // elongated: old path threw here
            intArrayOf(1, 4000, 1, 1, 2000),
            intArrayOf(2000, 2000, 1, 2000, 2000),  // already inside the bound
        )
        for (row in table) {
            val (w, h, expSample, expW, expH) = row
            assertEquals(
                "${w}x$h sample",
                expSample,
                ImageDecodeSizing.sampleSizeForExactScale(w, h, MAX_EDGE),
            )
            val out = outputSize(w, h, MAX_EDGE)
            assertEquals("${w}x$h width", expW, out.width)
            assertEquals("${w}x$h height", expH, out.height)
            // The intermediate must never be smaller than the target, otherwise
            // step 4 silently upscales a bitmap that lost real detail.
            val (dw, dh) = decodedSizeFloor(w, h, MAX_EDGE)
            assertTrue(
                "${w}x$h decoded ${dw}x$dh must not be smaller than target ${out.width}x${out.height}",
                dw >= out.width && dh >= out.height,
            )
            // …and never larger than the 2x window (the memory bound).
            val (cw, ch) = decodedSizeCeil(w, h, MAX_EDGE)
            assertTrue("${w}x$h decoded $cw x $ch exceeds the 2x window", cw <= 2 * MAX_EDGE && ch <= 2 * MAX_EDGE)
        }
    }

    @Test
    fun `pre-fix oracle is pinned by literals (not derived from the new code)`() {
        assertEquals(PixelSize(924, 2000), preFixSize(1284, 2778, MAX_EDGE))
        assertEquals(PixelSize(900, 2000), preFixSize(1080, 2400, MAX_EDGE))
        assertEquals(PixelSize(2000, 1333), preFixSize(3000, 2000, MAX_EDGE))
        assertEquals(PixelSize(2000, 1500), preFixSize(4000, 3000, MAX_EDGE))
        // The bug this fix exists for: a 0-height target.
        assertEquals(PixelSize(2000, 0), preFixSize(4000, 1, MAX_EDGE))
    }

    @Test
    fun `output size matches the pre-fix single-pass resize, case by case`() {
        val sizes = listOf(
            1284 to 2778, 1080 to 2400, 3000 to 2000, 4000 to 3000, 8000 to 6000,
            2000 to 2000, 17 to 4096, 2400 to 1080, 4321 to 2887, 5001 to 3001,
            4096 to 3072, 6000 to 4000, 999 to 999, 100000 to 50000, 12345 to 6789,
            17920 to 3000,
        )
        for ((w, h) in sizes) {
            val expected = preFixSize(w, h, MAX_EDGE)
            val out = outputSize(w, h, MAX_EDGE)
            assertEquals("${w}x$h parity", expected, out)
        }
    }

    @Test
    fun `elongated images keep a non-zero short edge (the crash the old path had)`() {
        assertEquals(PixelSize(2000, 1), outputSize(4000, 1, MAX_EDGE))
        assertEquals(PixelSize(1, 2000), outputSize(1, 4000, MAX_EDGE))
        // The only intended divergence from the pre-fix behaviour, deliberately:
        // the old path asked the decoder for a 0-dimension bitmap and threw.
        assertEquals(PixelSize(2000, 0), preFixSize(4000, 1, MAX_EDGE))
        assertEquals(PixelSize(0, 2000), preFixSize(1, 4000, MAX_EDGE))
    }

    @Test
    fun `output size stays inside the bound and never collapses to zero`() {
        for ((w, h) in listOf(1284 to 2778, 1080 to 2400, 3000 to 2000, 4000 to 3000, 8000 to 6000, 4000 to 1, 1 to 4000, 2000 to 2000, 4321 to 2887, 17920 to 3000)) {
            val out = outputSize(w, h, MAX_EDGE)
            assertTrue("${w}x$h -> ${out.width}x${out.height}", out.width in 1..MAX_EDGE)
            assertTrue("${w}x$h -> ${out.width}x${out.height}", out.height in 1..MAX_EDGE)
        }
    }

    @Test
    fun `exact-scale sampling keeps one power of two of detail above the target`() {
        for ((w, h) in listOf(1284 to 2778, 1080 to 2400, 3000 to 2000, 8000 to 6000, 4321 to 2887, 17920 to 3000, 4000 to 1, 60000 to 1)) {
            val s = ImageDecodeSizing.sampleSizeForExactScale(w, h, MAX_EDGE)
            assertTrue("${w}x$h sample=$s must be a power of two", s > 0 && (s and (s - 1)) == 0)
            val sampled = maxOf(w / s, h / s)
            assertTrue("${w}x$h sample=$s -> $sampled exceeds the 2x window", sampled <= 2 * MAX_EDGE)
            // Never falls below the target unless the source itself is smaller.
            if (maxOf(w, h) > MAX_EDGE) {
                assertTrue("${w}x$h sample=$s -> $sampled dropped below the target", sampled > MAX_EDGE)
            }
        }
    }

    @Test
    fun `display contract - sampleSizeFor bounds the decoded bitmap itself`() {
        // No exact pass follows, so the decoded bitmap must already be inside.
        assertEquals(8, ImageDecodeSizing.sampleSizeFor(1080, 2400, 512))
        assertEquals(2, ImageDecodeSizing.sampleSizeFor(513, 10, 512))
        assertEquals(1, ImageDecodeSizing.sampleSizeFor(512, 512, 512))
        assertEquals(1, ImageDecodeSizing.sampleSizeFor(100, 100, 512))
        for ((w, h) in listOf(1080 to 2400, 513 to 10, 3000 to 17920, 4000 to 3000)) {
            val s = ImageDecodeSizing.sampleSizeFor(w, h, 512)
            assertTrue("${w}x$h sample=$s", maxOf(w / s, h / s) <= 512)
        }
    }

    @Test
    fun `both windows tolerate degenerate probes`() {
        assertEquals(1, ImageDecodeSizing.sampleSizeFor(0, 2400, 512))
        assertEquals(1, ImageDecodeSizing.sampleSizeFor(1080, 0, 512))
        assertEquals(1, ImageDecodeSizing.sampleSizeFor(1080, 2400, 0))
        assertEquals(1, ImageDecodeSizing.sampleSizeForExactScale(0, 2400, MAX_EDGE))
        assertEquals(1, ImageDecodeSizing.sampleSizeForExactScale(1080, 0, MAX_EDGE))
        assertEquals(1, ImageDecodeSizing.sampleSizeForExactScale(1080, 2400, 0))
        assertEquals(1, ImageDecodeSizing.sampleSizeForExactScale(2000, 2000, MAX_EDGE))
    }

    @Test
    fun `scaled size keeps the aspect ratio and never upscales`() {
        assertEquals(PixelSize(2000, 1500), ImageDecodeSizing.scaledSize(4000, 3000, MAX_EDGE))
        assertEquals(PixelSize(2000, 1), ImageDecodeSizing.scaledSize(4000, 1, MAX_EDGE))
        assertEquals(PixelSize(1, 2000), ImageDecodeSizing.scaledSize(1, 4000, MAX_EDGE))
        assertEquals(PixelSize(1500, 2000), ImageDecodeSizing.scaledSize(1500, 2000, MAX_EDGE))
        // Inside the bound => untouched, no upscale.
        assertEquals(PixelSize(100, 50), ImageDecodeSizing.scaledSize(100, 50, MAX_EDGE))
        assertEquals(PixelSize(2000, 2000), ImageDecodeSizing.scaledSize(2000, 2000, MAX_EDGE))
        // Degenerate probe results can never become 0x0.
        assertEquals(PixelSize(1, 1), ImageDecodeSizing.scaledSize(0, 0, MAX_EDGE))
    }

    @Test
    fun `fitsWithin is the exact complement of needing a decode bound`() {
        assertTrue(ImageDecodeSizing.fitsWithin(2000, 2000, MAX_EDGE))
        assertTrue(ImageDecodeSizing.fitsWithin(2000, 10, MAX_EDGE))
        assertTrue(ImageDecodeSizing.fitsWithin(1, 1, MAX_EDGE))
        assertFalse(ImageDecodeSizing.fitsWithin(2001, 2000, MAX_EDGE))
        assertFalse(ImageDecodeSizing.fitsWithin(2000, 2001, MAX_EDGE))
        assertFalse(ImageDecodeSizing.fitsWithin(0, 2000, MAX_EDGE))
        assertFalse(ImageDecodeSizing.fitsWithin(2000, 2000, 0))
    }
}
