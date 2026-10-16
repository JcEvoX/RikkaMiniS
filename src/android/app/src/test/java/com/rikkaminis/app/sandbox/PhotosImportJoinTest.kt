package com.rikkaminis.app.sandbox

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * [deep-audit-1016c FIX-B A] The guest→host first hop of `android-photos
 * import`: normalize `--path` against the shell cwd.
 *
 * This is the REAL GuestPathMapper.kt compiled as-is (it has no Android
 * dependency), so nothing here can drift from the shipped normalizer. The
 * second hop — session-aware resolution — is pinned by
 * SessionPathResolutionTest's three arms (no session → global; session hit →
 * session; session refuse → null, never global) and by
 * PhotosImportShapeTest for the call site.
 */
class PhotosImportJoinTest {

    @Test
    fun `relative path joins against the shell cwd`() {
        assertEquals(
            "/var/minis/workspace/shot.png",
            GuestPathMapper.absoluteGuestPath("/var/minis/workspace", "shot.png"),
        )
        assertEquals(
            "/var/minis/workspace/sub/shot.png",
            GuestPathMapper.absoluteGuestPath("/var/minis/workspace", "sub/shot.png"),
        )
    }

    @Test
    fun `absolute path passes through, normalized`() {
        assertEquals(
            "/var/minis/workspace/a.png",
            GuestPathMapper.absoluteGuestPath("/anywhere", "/var/minis/workspace/./a.png"),
        )
        assertEquals(
            "/var/minis/workspace/a/b/c.png",
            GuestPathMapper.absoluteGuestPath("/var/minis/workspace/", "a//b/./c.png"),
        )
    }

    @Test
    fun `empty or relative cwd treats the path as root-anchored`() {
        assertEquals("/x.png", GuestPathMapper.absoluteGuestPath("", "x.png"))
        assertEquals("/x.png", GuestPathMapper.absoluteGuestPath("root", "x.png"))
    }

    @Test
    fun `dot-dot that climbs above the sandbox root is refused`() {
        assertNull(GuestPathMapper.absoluteGuestPath("", "../x.png"))
        assertNull(GuestPathMapper.absoluteGuestPath("/", "../x.png"))
        assertNull(GuestPathMapper.absoluteGuestPath("/var/minis/workspace", "../../../../etc/passwd"))
    }

    @Test
    fun `nested dot-dot is normalized and leaves the shorter path`() {
        // Not a refusal — the normalizer clamps inside the tree; the session
        // resolver is what enforces containment (and this path matches no
        // mounted tree in practice).
        assertEquals(
            "/var/minis/workspace/x.png",
            GuestPathMapper.absoluteGuestPath("/var/minis/workspace", "sub/../x.png"),
        )
    }

    @Test
    fun `percent-encoded traversal is NOT decoded - it stays a literal name inside the tree`() {
        // Deliberate negative decision: the guest filesystem stores literal
        // '%2e' names, so decoding here would CREATE an escape vector rather
        // than close one. Keeping the segment literal means the result is
        // contained by construction; the resolver then simply finds no file.
        assertEquals(
            "/var/minis/workspace/%2e%2e%2fx.png",
            GuestPathMapper.absoluteGuestPath("/var/minis/workspace", "%2e%2e%2fx.png"),
        )
        assertEquals(
            "/var/minis/workspace/..%2fx",
            GuestPathMapper.absoluteGuestPath("/var/minis/workspace", "..%2fx"),
        )
    }
}
