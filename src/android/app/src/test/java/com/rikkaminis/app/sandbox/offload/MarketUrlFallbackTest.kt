package com.rikkaminis.app.sandbox.offload

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [dv29-A3 A-07] `android-open`'s Huawei `market://` fallback: the package name
 * parsed out of the link.
 *
 * Before: `substringAfter("details?id=")` used its receiver as the
 * missing-delimiter default, so a link without an `id` produced the WHOLE url
 * as a "package name" and the handler opened
 * `https://appgallery.huawei.com/app/market://search?q=foo`.
 */
class MarketUrlFallbackTest {

    @Test
    fun `a market link without an id has no package`() {
        assertNull(marketPackageOf("market://search?q=foo"))
        assertNull(marketPackageOf("market://details"))
        assertNull(marketPackageOf("market://details?hl=zh"))
        assertNull(marketPackageOf("market://details?id="))
        assertNull(marketPackageOf("market://"))
        assertNull(marketPackageOf("market://details?id=&hl=zh"))
    }

    @Test
    fun `a non-market url is never treated as a market link`() {
        assertNull(marketPackageOf("https://play.google.com/store/apps/details?id=com.example.app"))
        assertNull(marketPackageOf(""))
        assertNull(marketPackageOf("tel:10086"))
    }

    @Test
    fun `a market link with an id answers the package`() {
        assertEquals("com.example.app", marketPackageOf("market://details?id=com.example.app"))
        assertEquals(
            "com.example.app",
            marketPackageOf("market://details?id=com.example.app&hl=zh-CN"),
        )
        assertEquals(
            "com.example.app",
            marketPackageOf("market://details?hl=zh-CN&id=com.example.app"),
        )
        assertEquals("com.example.app", marketPackageOf("market://search?id=com.example.app&q=x"))
    }
}
