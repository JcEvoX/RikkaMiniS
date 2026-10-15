package com.rikkaminis.app.util

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [audit2-PERF-03] Pins the decode-sizing arithmetic that keeps the
 * `read_image` / attachment paths from materialising a full-resolution bitmap.
 *
 * Expectations are written as literals on purpose — deriving them from the
 * implementation would let a regression prove itself correct.
 */
class ImageDecodeSizingTest {

    /**
     * Models the production pipeline: `BitmapFactory` clamps a sampled edge to
     * at least 1 (`max(1, w / sample)`), then [ImageDecodeSizing.scaledSize]
     * performs the exact-bound pass (`ImageDecoding.shrinkToBound`).
     */
    private fun outputSize(width: Int, height: Int, maxEdge: Int): PixelSize {
        val sample = ImageDecodeSizing.sampleSizeFor(width, height, maxEdge)
        return ImageDecodeSizing.scaledSize(
            maxOf(1, width / sample),
            maxOf(1, height / sample),
            maxEdge,
        )
    }

    @Test
    fun `output size stays inside the bound and never collapses to zero`() {
        val cases = listOf(
            Triple(4000, 3000, 2000) to PixelSize(2000, 1500),
            Triple(8000, 6000, 2000) to PixelSize(2000, 1500),
            // The crash case: 2000/2 = 1000 wide, 1/2 = 0 tall without the clamp.
            Triple(4000, 1, 2000) to PixelSize(2000, 1),
            Triple(1, 4000, 2000) to PixelSize(1, 2000),
            Triple(2000, 2000, 2000) to PixelSize(2000, 2000),
        )
        for ((dims, expected) in cases) {
            val (w, h, maxEdge) = dims
            val out = outputSize(w, h, maxEdge)
            assertEquals("${w}x$h @$maxEdge width", expected.width, out.width)
            assertEquals("${w}x$h @$maxEdge height", expected.height, out.height)
            assertTrue("${w}x$h @$maxEdge -> ${out.width}x${out.height}", out.width in 1..maxEdge)
            assertTrue("${w}x$h @$maxEdge -> ${out.width}x${out.height}", out.height in 1..maxEdge)
        }
    }

    @Test
    fun `sampling never leaves the longest edge above the bound`() {
        for ((w, h) in listOf(1 to 1, 2000 to 2000, 2001 to 10, 4000 to 3000, 8000 to 6000, 4000 to 1, 1 to 4000, 3000 to 17920)) {
            val s = ImageDecodeSizing.sampleSizeFor(w, h, 2000)
            val sampledW = w / s
            val sampledH = h / s
            assertTrue("${w}x$h sample=$s -> ${sampledW}x$sampledH exceeds 2000", maxOf(sampledW, sampledH) <= 2000)
        }
    }

    @Test
    fun `sampling is a power of two and never overshoots by more than one step`() {
        for ((w, h) in listOf(4000 to 3000, 8000 to 6000, 4000 to 1, 1 to 4000, 3000 to 17920, 1080 to 2400)) {
            val s = ImageDecodeSizing.sampleSizeFor(w, h, 2000)
            assertTrue("${w}x$h sample=$s must be a power of two", s > 0 && (s and (s - 1)) == 0)
            // One step coarser would have blown the bound => the sampled bitmap is
            // within 2x of the target (the standard inSampleSize guarantee).
            assertTrue("${w}x$h sample=$s is more than one step below the target", maxOf(w, h) / s > 1000)
        }
    }

    @Test
    fun `sampling never upsamples and tolerates degenerate probes`() {
        assertEquals(1, ImageDecodeSizing.sampleSizeFor(2000, 2000, 2000))
        assertEquals(1, ImageDecodeSizing.sampleSizeFor(512, 512, 2000))
        assertEquals(2, ImageDecodeSizing.sampleSizeFor(2001, 10, 2000))
        assertEquals(1, ImageDecodeSizing.sampleSizeFor(0, 2400, 2000))
        assertEquals(1, ImageDecodeSizing.sampleSizeFor(1080, 0, 2000))
        assertEquals(1, ImageDecodeSizing.sampleSizeFor(1080, 2400, 0))
    }

    @Test
    fun `scaled size keeps the aspect ratio and never upscales`() {
        assertEquals(PixelSize(2000, 1500), ImageDecodeSizing.scaledSize(4000, 3000, 2000))
        assertEquals(PixelSize(2000, 1), ImageDecodeSizing.scaledSize(4000, 1, 2000))
        assertEquals(PixelSize(1, 2000), ImageDecodeSizing.scaledSize(1, 4000, 2000))
        assertEquals(PixelSize(1500, 2000), ImageDecodeSizing.scaledSize(1500, 2000, 2000))
        // Inside the bound => untouched, no upscale.
        assertEquals(PixelSize(100, 50), ImageDecodeSizing.scaledSize(100, 50, 2000))
        assertEquals(PixelSize(2000, 2000), ImageDecodeSizing.scaledSize(2000, 2000, 2000))
        // Degenerate probe results can never become 0x0.
        assertEquals(PixelSize(1, 1), ImageDecodeSizing.scaledSize(0, 0, 2000))
    }

    @Test
    fun `fitsWithin is the exact complement of needing a decode bound`() {
        assertTrue(ImageDecodeSizing.fitsWithin(2000, 2000, 2000))
        assertTrue(ImageDecodeSizing.fitsWithin(2000, 10, 2000))
        assertTrue(ImageDecodeSizing.fitsWithin(1, 1, 2000))
        assertFalse(ImageDecodeSizing.fitsWithin(2001, 2000, 2000))
        assertFalse(ImageDecodeSizing.fitsWithin(2000, 2001, 2000))
        assertFalse(ImageDecodeSizing.fitsWithin(0, 2000, 2000))
        assertFalse(ImageDecodeSizing.fitsWithin(2000, 2000, 0))
    }
}
