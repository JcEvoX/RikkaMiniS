package com.rikkaminis.app.ui.settings

import com.rikkaminis.app.R
import com.rikkaminis.app.browser.SafeWebViewClient
import com.rikkaminis.app.ui.components.MinisTextButton

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Error
import androidx.compose.material.icons.filled.Info
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.res.stringResource
import com.rikkaminis.app.data.repository.SkillRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL
import com.rikkaminis.app.ui.theme.ChatColors

private enum class HudState {
    HIDDEN, IMPORTING, SUCCESS, ERROR, HINT
}

@SuppressLint("SetJavaScriptEnabled")
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MinisSkillsBrowserScreen(
    skillRepository: SkillRepository,
    onBack: () -> Unit,
) {
    val context = LocalContext.current
    var currentUrl by remember { mutableStateOf("https://github.com/OpenMinis/MinisSkills") }
    var hudState by remember { mutableStateOf(HudState.HIDDEN) }
    var hudMessage by remember { mutableStateOf("") }
    var webViewRef by remember { mutableStateOf<WebView?>(null) }
    // [GH#341] Bumped when the renderer dies so `key` discards the dead WebView
    // and the factory builds a fresh one. The screen is a thin browser around
    // the page, so a full reload is the correct recovery (unlike the pool /
    // holder cases, where rebuilding costs real work).
    var rendererEpoch by remember { mutableStateOf(0) }
    val scope = rememberCoroutineScope()

    // System back / gesture: walk WebView history first, leave the screen
    // only when the WebView has nothing left to pop. The TopAppBar back
    // button uses the same handler so both entry points behave identically.
    val handleBack = {
        val wv = webViewRef
        if (wv != null && wv.canGoBack()) {
            wv.goBack()
        } else {
            onBack()
        }
    }
    BackHandler(onBack = handleBack)

    Scaffold(
        topBar = {
            // Manual Scaffold (not SettingsScaffold) because the body is a
            // fullscreen WebView and SettingsScaffold's verticalScroll Column
            // would fight the WebView measure pass. Match the SettingsScaffold
            // bar styling (background colour + SemiBold title) so the visual
            // signature stays consistent with the rest of the settings stack.
            TopAppBar(
                title = { Text(stringResource(R.string.skills_browser_title), fontWeight = FontWeight.SemiBold) },
                navigationIcon = {
                    IconButton(onClick = handleBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = stringResource(R.string.common_back))
                    }
                },
                actions = {
                    MinisTextButton(
                        onClick = {
                            scope.launch {
                                importSkillFromCurrentUrl(
                                    context = context,
                                    url = currentUrl,
                                    skillRepository = skillRepository,
                                    onStateChange = { state, msg ->
                                        hudState = state
                                        hudMessage = msg
                                    },
                                )
                            }
                        },
                        enabled = hudState != HudState.IMPORTING,
                    ) {
                        Text(stringResource(R.string.skills_browser_import_button), style = MaterialTheme.typography.titleMedium)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                ),
            )
        },
        containerColor = MaterialTheme.colorScheme.background,
    ) { padding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding),
        ) {
            // WebView
            // [GH#341] `key` so a dead renderer is actually replaced: bumping
            // rendererEpoch below discards this AndroidView and re-runs the
            // factory with a fresh WebView.
            key(rendererEpoch) {
            // [FIX-R22 D12] Hoist the client out of the factory so the
            // `onRelease` below can reuse the shared
            // [SafeWebViewClient.destroyDeadWebView] teardown instead of
            // duplicating it.
            val skillWebClient = object : SafeWebViewClient() {
                /**
                 * [GH#341] Renderer died. Rebuild the WebView and
                 * reload — this screen has no unsaved state worth
                 * preserving, so a fresh page load is the whole
                 * recovery. Clearing the ref first keeps
                 * [handleBack] from calling into a dead instance.
                 * [FIX-R22 D12] The epoch bump below discards this
                 * AndroidView, whose `onRelease` now also runs the
                 * teardown — destroy here anyway so the dead instance
                 * gives up its renderer handle slot immediately, not
                 * on the next recomposition.
                 */
                override fun onRendererGone(view: WebView?) {
                    if (webViewRef === view) webViewRef = null
                    destroyDeadWebView(view)
                    rendererEpoch += 1
                }

                override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                    url?.let { currentUrl = it }
                }

                // GitHub SPA uses pushState — capture URL changes here too
                override fun doUpdateVisitedHistory(view: WebView?, url: String?, isReload: Boolean) {
                    url?.let { currentUrl = it }
                }

                override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                    val ctx = view?.context ?: return false
                    val urlStr = request?.url?.toString()
                    // T234: Google permanently disallows WebView
                    // for sign-in / OAuth. Hand auth-domain nav
                    // to Chrome Custom Tab.
                    if (com.rikkaminis.app.browser.GoogleAuthRouter.shouldRouteExternally(urlStr)) {
                        if (urlStr != null) {
                            com.rikkaminis.app.browser.GoogleAuthRouter.openInCustomTab(ctx, urlStr)
                        }
                        return true
                    }
                    // T134: hand off intent:// / market:// /
                    // tel: / mailto: links to the system so the
                    // user doesn't see ERR_UNKNOWN_URL_SCHEME.
                    return com.rikkaminis.app.ui.browser
                        .BrowserExternalSchemeHandler
                        .handle(ctx, request?.url)
                }

                /**
                 * [FIX-R22 D12] Public bridge: the AndroidView `onRelease`
                 * below sits outside this class and cannot reach the
                 * protected [destroyDeadWebView] directly.
                 */
                fun releaseDeadInstance(view: WebView?) = destroyDeadWebView(view)
            }
            AndroidView(
                factory = { context ->
                    WebView(context).apply {
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        settings.userAgentString = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Mobile Safari/537.36"

                        webViewClient = skillWebClient

                        webChromeClient = WebChromeClient()

                        loadUrl("https://github.com/OpenMinis/MinisSkills")
                    }.also { webViewRef = it }
                },
                onRelease = { wv ->
                    // [FIX-R22 D12] Normal screen exit used to leak the whole
                    // WebView — only the crash path (onRendererGone) tore
                    // anything down. Clear the ref first (keeps [handleBack]
                    // from calling into a dead instance), then run the same
                    // detach + destroy sequence.
                    if (webViewRef === wv) webViewRef = null
                    skillWebClient.releaseDeadInstance(wv)
                },
                modifier = Modifier.fillMaxSize(),
            )
            }

            // HUD overlay at bottom
            AnimatedVisibility(
                visible = hudState != HudState.HIDDEN,
                enter = slideInVertically(initialOffsetY = { it }) + fadeIn(),
                exit = slideOutVertically(targetOffsetY = { it }) + fadeOut(),
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .padding(bottom = 60.dp),
            ) {
                Row(
                    modifier = Modifier
                        .background(
                            Color.Black.copy(alpha = 0.75f),
                            RoundedCornerShape(50),
                        )
                        .padding(horizontal = 16.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    when (hudState) {
                        HudState.IMPORTING -> {
                            CircularProgressIndicator(
                                modifier = Modifier.size(16.dp),
                                color = Color.White,
                                strokeWidth = 2.dp,
                            )
                            Spacer(Modifier.width(10.dp))
                            Text(stringResource(R.string.skills_browser_hud_importing), color = Color.White, style = MaterialTheme.typography.labelLarge)
                        }
                        HudState.SUCCESS -> {
                            Icon(Icons.Filled.CheckCircle, contentDescription = null, tint = ChatColors.success, modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(10.dp))
                            Text(hudMessage, color = Color.White, style = MaterialTheme.typography.labelLarge, maxLines = 1)
                        }
                        HudState.ERROR -> {
                            Icon(Icons.Filled.Error, contentDescription = null, tint = ChatColors.error, modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(10.dp))
                            Text(hudMessage, color = Color.White, style = MaterialTheme.typography.labelLarge, maxLines = 2)
                        }
                        HudState.HINT -> {
                            Icon(Icons.Filled.Info, contentDescription = null, tint = ChatColors.link, modifier = Modifier.size(16.dp))
                            Spacer(Modifier.width(10.dp))
                            Text(hudMessage, color = Color.White, style = MaterialTheme.typography.labelLarge, maxLines = 2)
                        }
                        HudState.HIDDEN -> {}
                    }
                }
            }
        }
    }
}

private suspend fun importSkillFromCurrentUrl(
    context: Context,
    url: String,
    skillRepository: SkillRepository,
    onStateChange: (HudState, String) -> Unit,
) {
    // Check if URL points to a specific skill directory (not repo root)
    if (!isSkillDirectoryUrl(url)) {
        onStateChange(HudState.HINT, context.getString(R.string.skills_browser_error_not_folder))
        delay(3000)
        onStateChange(HudState.HIDDEN, "")
        return
    }

    onStateChange(HudState.IMPORTING, "")

    try {
        // Hand the GitHub directory URL to the repository's full-fat
        // importer. importFromGitHub fetches SKILL.md, imports it, then
        // recurses through the GitHub Contents API to pull every sibling
        // file (scripts/, references/, sub-directories) into the skill's
        // local directory. The previous flow only fetched SKILL.md via
        // toRawGitHubUrl + importFromContent, which left bundled
        // scripts on the server and the skill non-runnable on first use.
        val skill = skillRepository.importFromGitHub(url)
        if (skill != null) {
            onStateChange(HudState.SUCCESS, context.getString(R.string.skills_browser_success_imported, skill.name))
            delay(3000)
            onStateChange(HudState.HIDDEN, "")
            return
        }

        // importFromGitHub returned null — the SKILL.md was either
        // missing or duplicate. Fall back to the prior "fetch + try
        // updating existing" path so users can refresh in place.
        val rawUrl = toRawGitHubUrl(url)
        val content = withContext(Dispatchers.IO) {
            val conn = URL(rawUrl).openConnection() as HttpURLConnection
            conn.connectTimeout = 15_000
            conn.readTimeout = 15_000
            try {
                if (conn.responseCode == 200) {
                    conn.inputStream.bufferedReader().readText()
                } else {
                    null
                }
            } finally {
                conn.disconnect()
            }
        }

        if (content == null) {
            onStateChange(HudState.HINT, context.getString(R.string.skills_browser_error_no_skillmd))
            delay(4000)
            onStateChange(HudState.HIDDEN, "")
            return
        }

        val parsed = skillRepository.parseSkillMdPublic(content)
        if (parsed != null) {
            // [FIX-R22 B32b] The old call used `slugify(parsed)` as the id and
            // passed body only: skills whose local id differs from the name
            // slug (renamed or imported with a custom id) never matched, and
            // description/version were dropped. Resolve the real record id by
            // name OR slug (pure helper below, JVM-covered), pass the full
            // ParsedSkill, and run the repository write on IO — db.execSQL +
            // writeSkillMd are disk IO and used to run on the Main
            // dispatcher. update() has no frontmatter parameter; its
            // writeSkillMd preserves the existing frontmatter block, so no
            // managed key is lost (boundary registered in the report).
            val targetId = resolveUpdateTargetId(
                skillRepository.skills.value.map { it.id to it.name },
                slug = slugify(parsed.name),
                incomingName = parsed.name,
            ) ?: slugify(parsed.name)
            val updated = withContext(Dispatchers.IO) {
                skillRepository.update(
                    id = targetId,
                    name = parsed.name,
                    description = parsed.description,
                    body = parsed.body,
                )
            }
            if (updated) {
                onStateChange(HudState.SUCCESS, context.getString(R.string.skills_browser_success_updated, parsed.name))
                delay(3000)
                onStateChange(HudState.HIDDEN, "")
            } else {
                onStateChange(HudState.ERROR, context.getString(R.string.skills_browser_error_invalid))
                delay(4000)
                onStateChange(HudState.HIDDEN, "")
            }
        } else {
            onStateChange(HudState.ERROR, context.getString(R.string.skills_browser_error_format))
            delay(4000)
            onStateChange(HudState.HIDDEN, "")
        }
    } catch (e: Exception) {
        onStateChange(HudState.ERROR, e.message ?: "Import failed")
        delay(4000)
        onStateChange(HudState.HIDDEN, "")
    }
}

/**
 * Check if the URL points to a specific skill directory (not repo root or non-skill pages).
 * e.g. github.com/OpenMinis/MinisSkills/tree/main/exa-search → true
 *      github.com/OpenMinis/MinisSkills → false
 *      github.com/OpenMinis/MinisSkills/issues → false
 */
private fun isSkillDirectoryUrl(url: String): Boolean {
    if (!url.contains("github.com")) return true // Non-GitHub URL, let it try
    if (url.endsWith("SKILL.md")) return true

    // Must contain /tree/ or /blob/ with a path after the branch name
    // e.g. /tree/main/skill-name or /blob/main/skill-name/SKILL.md
    val treePattern = Regex("github\\.com/[^/]+/[^/]+/(tree|blob)/[^/]+/.+")
    if (treePattern.containsMatchIn(url)) return true

    // Reject known non-skill pages
    val nonSkillPages = listOf("/issues", "/pulls", "/pull/", "/actions", "/settings", "/wiki")
    if (nonSkillPages.any { url.contains(it) }) return false

    return false
}

private fun toRawGitHubUrl(url: String): String {
    // Handle github.com/user/repo/tree/branch/path → find SKILL.md
    if (url.contains("github.com") && url.contains("/blob/")) {
        return url.replace("github.com", "raw.githubusercontent.com")
            .replace("/blob/", "/")
    }
    if (url.contains("github.com") && !url.contains("raw.githubusercontent.com")) {
        val withFile = if (!url.endsWith("SKILL.md")) {
            // Try tree → add SKILL.md
            val cleaned = url.replace("/tree/", "/").trimEnd('/')
            "$cleaned/SKILL.md"
        } else {
            url
        }
        return withFile.replace("github.com", "raw.githubusercontent.com")
            .replace("/blob/", "/")
            .replace("/tree/", "/")
    }
    return url
}

/**
 * [FIX-R22 B32b] Resolve the id of the existing skill a browser refresh
 * targets: prefer the record whose id equals the name slug, else the record
 * whose display name equals the incoming name (custom id case), else null —
 * the caller falls back to the slug and the repository reports "not found".
 * Pure so JVM tests cover the mapping (SkillRepository needs Android's
 * Context/SQLite and can't be unit-tested directly).
 */
internal fun resolveUpdateTargetId(
    skills: List<Pair<String, String>>,
    slug: String,
    incomingName: String,
): String? =
    skills.firstOrNull { (id, name) -> id == slug || name == incomingName }?.first

private fun slugify(name: String): String =
    name.lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-')
