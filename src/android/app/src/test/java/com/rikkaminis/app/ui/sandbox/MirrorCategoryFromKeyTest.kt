package com.rikkaminis.app.ui.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [da1021-C] MirrorCategory.fromKey route-parameter resolution.
 *
 * Expectations are hard-coded literals (COMMON §3): the route tokens
 * "alpine"/"pip"/"npm" and the entry order are asserted directly, never
 * derived from the function under test.
 */
class MirrorCategoryFromKeyTest {

    @Test
    fun `known keys resolve to the right entries`() {
        assertEquals(MirrorCategory.ALPINE, MirrorCategory.fromKey("alpine"))
        assertEquals(MirrorCategory.PIP, MirrorCategory.fromKey("pip"))
        assertEquals(MirrorCategory.NPM, MirrorCategory.fromKey("npm"))
    }

    @Test
    fun `unknown keys are fail-closed null`() {
        assertNull(MirrorCategory.fromKey("alpne"))
        assertNull(MirrorCategory.fromKey(""))
        assertNull(MirrorCategory.fromKey("Alpine")) // case-sensitive token
        assertNull(MirrorCategory.fromKey("alpine ")) // no whitespace tolerance
        assertNull(MirrorCategory.fromKey("apt"))
    }

    @Test
    fun `keys are the exact route tokens emitted by the builder`() {
        assertEquals("alpine", MirrorCategory.ALPINE.key)
        assertEquals("pip", MirrorCategory.PIP.key)
        assertEquals("npm", MirrorCategory.NPM.key)
    }
}
