package com.rikkaminis.app.ui.settings

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * [dv15-B2 / deep-audit-1036 B-08] Source-shape probe for the accessibility
 * self-rescue wiring.
 *
 * These are *shape* defects: which screen the two rescue cards are reachable
 * from, and whether the readiness decision is one shared mapping or two forked
 * copies. None of it is observable from a JVM unit test without a live service,
 * so each property is asserted on the production text — with comments stripped
 * first, because the fix's own explanatory comments quote the very expressions
 * being checked for (a mention in prose must not satisfy a shape probe).
 *
 * One arm per property, each reporting its own reason, so the mutation table can
 * show that exactly one arm flips per mutation. A missing file is a violation,
 * not an exception, so the pre-fix tree (where the two new files do not exist
 * yet) still yields per-arm reasons.
 */
class A11ySelfRescueShapeTest {

    private val settingsDir = "src/android/app/src/main/java/com/rikkaminis/app/ui/settings"
    private val liveRel = "$settingsDir/OffloadPermissionScreen.kt"
    private val deadRel = "$settingsDir/SystemPermissionsScreen.kt"
    private val sharedRel = "$settingsDir/A11yDiagnosticsUi.kt"
    private val statusRel = "$settingsDir/A11yStatus.kt"

    private fun sourceFileOrNull(rel: String): File? {
        val viaAndroid = "src/android/app/$rel"
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val d = dir
            if (File(d, rel).isFile) return File(d, rel)
            if (File(d, viaAndroid).isFile) return File(d, viaAndroid)
            dir = d.parentFile
        }
        return null
    }

    /** Comment lines blanked out (offsets preserved) so prose cannot satisfy an arm. */
    private fun codeOnly(body: String): String = body.lines().joinToString("\n") { line ->
        val t = line.trimStart()
        if (t.startsWith("//") || t.startsWith("*") || t.startsWith("/*")) {
            " ".repeat(line.length)
        } else {
            line
        }
    }

    private fun code(rel: String): String = sourceFileOrNull(rel)?.let { codeOnly(it.readText()) } ?: ""

    private fun countAll(needle: String, rels: List<String>): List<String> =
        rels.filter { code(it).contains(needle) }

    // ── arms (empty list = the shape holds) ──────────────────────────────────

    /** Arm A1: the misreporting OR-form is gone from the reachable screen. */
    private fun misreportViolations(): List<String> {
        val src = code(liveRel)
        if (src.isEmpty()) return listOf("$liveRel is missing")
        val out = mutableListOf<String>()
        if (src.contains("isA11yServiceEnabled") && src.contains("|| MinisAccessibilityService")) {
            out += "the reachable screen still ORs the listing with the live instance " +
                "(a listed-but-killed service reports ready)"
        }
        if (src.contains("getInstance() != null")) {
            out += "the reachable screen still consults getInstance() directly"
        }
        return out
    }

    /** Arm A2: readiness is derived from the shared classification. */
    private fun sharedDecisionViolations(): List<String> {
        val src = code(liveRel)
        if (src.isEmpty()) return listOf("$liveRel is missing")
        val out = mutableListOf<String>()
        if (!src.contains("a11yStatusOf(context)")) {
            out += "the reachable screen does not derive its state from a11yStatusOf(context)"
        }
        if (!src.contains("A11yStatus.READY")) {
            out += "the reachable screen never compares against A11yStatus.READY"
        }
        return out
    }

    /** Arm B: both self-rescue cards are rendered where the user can reach them. */
    private fun rescueCardViolations(): List<String> {
        val src = code(liveRel)
        if (src.isEmpty()) return listOf("$liveRel is missing")
        val out = mutableListOf<String>()
        if (!src.contains("A11yRevokedRepairSection(")) {
            out += "the reachable screen does not render the force-stop repair card"
        }
        if (!src.contains("A11yOemKeepAliveSection(")) {
            out += "the reachable screen does not render the OEM keep-alive card"
        }
        return out
    }

    /** Arm C: the ever-granted gate that keeps a fresh install clean. */
    private fun everGrantedViolations(): List<String> {
        val shared = code(sharedRel)
        val status = code(statusRel)
        val out = mutableListOf<String>()
        if (shared.isEmpty()) return listOf("$sharedRel is missing")
        if (status.isEmpty()) return listOf("$statusRel is missing")
        if (!shared.contains("AccessibilityRecoveryManager.hasEverBeenGranted(context)")) {
            out += "the shared observation no longer passes hasEverBeenGranted(context)"
        }
        if (!status.contains("everGranted -> A11yStatus.REVOKED")) {
            out += "REVOKED is no longer gated on everGranted (a fresh install would be offered a repair)"
        }
        return out
    }

    /** Arm D: the unreachable screen was de-forked, not replaced. */
    private fun deadScreenViolations(): List<String> {
        val src = code(deadRel)
        if (src.isEmpty()) return listOf("$deadRel is missing")
        val out = mutableListOf<String>()
        if (!src.contains("fun SystemPermissionsScreen(")) {
            out += "SystemPermissionsScreen is gone (its route declaration must stay)"
        }
        if (!src.contains("a11yStatusOf(context)")) {
            out += "the second screen still computes its a11y state locally"
        }
        if (src.contains("a11yDegraded = inSettings && !connected")) {
            out += "the forked inline `inSettings && !connected` decision is back"
        }
        return out
    }

    /** Arm E: one definition of each shared piece — sharing, not copying. */
    private fun singleDefinitionViolations(): List<String> {
        val all = listOf(liveRel, deadRel, sharedRel)
        val out = mutableListOf<String>()
        val link = countAll("fun openAccessibilitySettings(", all)
        if (link.size != 1 || link[0] != sharedRel) {
            out += "the settings deeplink is not defined exactly once in $sharedRel (found in $link)"
        }
        if (!code(sharedRel).contains("internal fun openAccessibilitySettings(")) {
            out += "the shared deeplink is not internal, so it cannot serve both screens"
        }
        for (card in listOf("fun A11yRevokedRepairSection(", "fun A11yOemKeepAliveSection(")) {
            val where = countAll(card, all)
            if (where.size != 1 || where[0] != sharedRel) {
                out += "$card must be defined exactly once in $sharedRel (found in $where)"
            }
        }
        return out
    }

    /** Arm F: the reachable screen consumes both decision sources. */
    private fun decisionSourceViolations(): List<String> {
        val src = code(liveRel)
        if (src.isEmpty()) return listOf("$liveRel is missing")
        val out = mutableListOf<String>()
        if (!src.contains("revoked = a11yRevoked")) {
            out += "the repair card is not fed the revoked decision"
        }
        if (!src.contains("degraded = a11yDegraded")) {
            out += "the keep-alive card is not fed the degraded decision"
        }
        return out
    }

    /**
     * Arm G: one classification assigns all three flags, so the degraded state
     * cannot be reported as ready — the defect was precisely a second, more
     * optimistic derivation living beside the first.
     */
    private fun singleFlagSourceViolations(): List<String> {
        val src = code(liveRel)
        if (src.isEmpty()) return listOf("$liveRel is missing")
        val out = mutableListOf<String>()
        if (!src.contains("a11yEnabled = status == A11yStatus.READY")) {
            out += "readiness is not assigned from the classification (degraded could read as ready)"
        }
        if (!src.contains("a11yDegraded = status == A11yStatus.DEGRADED")) {
            out += "the degraded flag is not assigned from the classification"
        }
        if (!src.contains("a11yRevoked = status == A11yStatus.REVOKED")) {
            out += "the revoked flag is not assigned from the classification"
        }
        return out
    }

    private fun assertHolds(label: String, violations: List<String>) {
        assertTrue("[$label] the shape must hold, got: $violations", violations.isEmpty())
    }

    // ── the live source ──────────────────────────────────────────────────────

    @Test
    fun `arm A1 - the reachable screen no longer misreports a listed-but-killed service`() {
        assertHolds("A1", misreportViolations())
    }

    @Test
    fun `arm A2 - readiness comes from the shared classification`() {
        assertHolds("A2", sharedDecisionViolations())
    }

    @Test
    fun `arm B - both self-rescue cards are reachable`() {
        assertHolds("B", rescueCardViolations())
    }

    @Test
    fun `arm C - the ever-granted gate survives`() {
        assertHolds("C", everGrantedViolations())
    }

    @Test
    fun `arm D - the unreachable screen is de-forked, its route kept`() {
        assertHolds("D", deadScreenViolations())
    }

    @Test
    fun `arm E - each shared piece has exactly one definition`() {
        assertHolds("E", singleDefinitionViolations())
    }

    @Test
    fun `arm F - the reachable screen consumes both decision sources`() {
        assertHolds("F", decisionSourceViolations())
    }

    @Test
    fun `arm G - one classification assigns all three flags`() {
        assertHolds("G", singleFlagSourceViolations())
    }
}
