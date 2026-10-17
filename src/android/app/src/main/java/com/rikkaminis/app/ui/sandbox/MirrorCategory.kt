package com.rikkaminis.app.ui.sandbox

/**
 * Mirror-source categories ("alpine" / "pip" / "npm").
 *
 * [da1021-C] Split out of MirrorSettingsScreen.kt — that file drags Compose
 * and material-icons deps, which made the category model untestable from the
 * JVM test source set. This file is deliberately dependency-free so the
 * route-key parsing below compiles (and is unit-tested) standalone.
 */
enum class MirrorCategory(
    val key: String,
    val displayName: String,
    val configPath: String,
) {
    ALPINE("alpine", "Alpine APK", "etc/apk/repositories"),
    PIP("pip", "Python pip", "etc/pip/pip.conf"),
    NPM("npm", "Node.js npm", "root/.npmrc");

    companion object {
        /**
         * [da1021-C] Resolve a route parameter back to its category. Keys are
         * the route tokens emitted by [com.rikkaminis.app.ui.navigation.Routes.mirrorCategoryDetail];
         * an unknown key returns null (fail-closed) so the navigation layer
         * backs out instead of crashing on a hand-typed deep link.
         */
        fun fromKey(key: String): MirrorCategory? =
            entries.firstOrNull { it.key == key }
    }
}
