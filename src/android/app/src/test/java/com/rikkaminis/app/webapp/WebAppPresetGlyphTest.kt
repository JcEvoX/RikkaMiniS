package com.rikkaminis.app.webapp

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM tests for the preset→glyph dispatch added by [sweep1014-b7b]:
 * every preset must map to its own glyph (8 presets → 8 distinct glyphs),
 * and no named preset may silently fall back to the generic glyph.
 */
class WebAppPresetGlyphTest {

    @Test
    fun `all 8 presets dispatch to distinct glyphs`() {
        val presets = IconPreset.values().toList()
        assertEquals(8, presets.size)
        val glyphs = presets.map { presetGlyphKind(it) }
        assertEquals(8, glyphs.toSet().size)
    }

    @Test
    fun `named presets never fall back to generic glyph`() {
        val named = IconPreset.values().filter { it != IconPreset.Generic }
        assertEquals(7, named.size)
        for (preset in named) {
            assertTrue(
                "preset ${preset.name} fell back to Generic",
                presetGlyphKind(preset) != PresetGlyph.Generic,
            )
        }
        assertEquals(PresetGlyph.Generic, presetGlyphKind(IconPreset.Generic))
    }

    @Test
    fun `each preset maps to its intended glyph`() {
        assertEquals(PresetGlyph.Globe, presetGlyphKind(IconPreset.Globe))
        assertEquals(PresetGlyph.Chart, presetGlyphKind(IconPreset.Chart))
        assertEquals(PresetGlyph.Book, presetGlyphKind(IconPreset.Book))
        assertEquals(PresetGlyph.AppGrid, presetGlyphKind(IconPreset.App))
        assertEquals(PresetGlyph.Video, presetGlyphKind(IconPreset.Video))
        assertEquals(PresetGlyph.Game, presetGlyphKind(IconPreset.Game))
        assertEquals(PresetGlyph.Music, presetGlyphKind(IconPreset.Music))
        assertEquals(PresetGlyph.Generic, presetGlyphKind(IconPreset.Generic))
    }
}
