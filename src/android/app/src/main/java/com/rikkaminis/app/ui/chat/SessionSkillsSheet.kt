package com.rikkaminis.app.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Description
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.rikkaminis.app.R
import com.rikkaminis.app.data.repository.SkillRepository
import com.rikkaminis.app.ui.components.DialogTextField
import com.rikkaminis.app.ui.settings.SettingsSection
import com.rikkaminis.app.ui.settings.SkillRowItem
import com.rikkaminis.app.ui.components.MinisTextButton

/**
 * Bottom sheet showing all skills with per-session enable/disable toggles.
 * Mirrors iOS SessionSkillsView. Uses the [StandardChatSheet] shell plus the
 * shared [SettingsSection] / [SkillRowItem] primitives so each skill row
 * looks identical to the Settings → Skills screen.
 *
 * The "Enable All / Disable All" controls live to the right of the section
 * header (mirroring iOS section-actions), keeping the sheet's standard top
 * header reserved for the title + close button.
 */
@Composable
fun SessionSkillsSheet(
    sessionId: String,
    skillRepository: SkillRepository,
    onDismiss: () -> Unit,
) {
    val skills by skillRepository.skills.collectAsState()

    // [T-android-session-skill-override-init-timing] The seed must re-derive
    // once the live skills list arrives: a keyless `remember {}` seeds from
    // the possibly-empty first-composition list and then never re-runs, so
    // every row's switch is stuck at the global default (the same stale-
    // override race already fixed in SessionMcpsSheet — keeping the two
    // sheets symmetric). Mirrors iOS ed861471 (T-ios-session-skill-override-
    // init-timing) for the initial-state half of that fix; the per-draft id
    // binding half is already covered on Android by the `__new__<uuid>` route
    // arg (every draft has its own stable session id), unlike iOS which
    // previously coerced nil to "" and bled overrides across drafts.
    // [da1022-b] Key the map identity on `sessionId`: when the draft session
    // is promoted (first send), the caller re-composes with the real session
    // id, so a FRESH map must be built and re-seeded from the real id's
    // stored overrides — otherwise the sheet keeps showing (and the toggles
    // keep writing against) the pre-promotion `__new__…` key. Same reasoning
    // as the sessionId key added to both sheets.
    // [da1034-b3] Seed by MERGING into that live map (mirrors SessionMcpsSheet's
    // [audit-0917] merge) instead of rebuilding it inside
    // `remember(skills, sessionId)`: the rebuild ran one `isEnabledForSession`
    // SQLite read per skill on EVERY skills StateFlow emission — inside
    // composition — and threw away any toggle the user had already flipped in
    // the open sheet (the row snapped back to the stored value until the
    // write landed, and an emission arriving between flip and write reverted
    // the switch outright). The merge only fills in ids that are missing, so
    // an open sheet keeps in-flight toggles, and rows for skills that were
    // removed are dropped so a later re-add re-seeds from storage.
    val overrides = remember(sessionId) {
        mutableStateMapOf<String, Boolean>()
    }
    LaunchedEffect(skills, sessionId) {
        mergeSessionOverrides(
            liveIds = skills.map { it.id },
            overrides = overrides,
            lookup = { skillRepository.isEnabledForSession(it, sessionId) },
        )
    }

    var searchQuery by remember { mutableStateOf("") }
    val filteredSkills by remember(skills) {
        derivedStateOf {
            val q = searchQuery.trim().lowercase()
            if (q.isEmpty()) skills
            else skills.filter {
                it.name.lowercase().contains(q) || it.description.lowercase().contains(q)
            }
        }
    }

    StandardChatSheet(
        title = stringResource(R.string.session_skills_title),
        onDismiss = onDismiss,
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState()),
        ) {
            if (skills.isEmpty()) {
                EmptySkillsCard()
            } else {
                // Search field — filters by skill name + description.
                DialogTextField(
                    value = searchQuery,
                    onValueChange = { searchQuery = it },
                    placeholder = stringResource(R.string.skills_search_placeholder),
                    singleLine = true,
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp)
                        .padding(top = 12.dp),
                )
                // Header row: small-caps "INSTALLED" label on the left,
                // Enable-All / Disable-All TextButtons on the right. Echoes
                // the SettingsSection header exactly so visual rhythm carries
                // over from Settings.
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(top = 16.dp)
                        .padding(start = 32.dp, end = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        text = stringResource(R.string.session_skills_header_installed),
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        fontWeight = FontWeight.Medium,
                        letterSpacing = 0.5.sp,
                    )
                    Spacer(modifier = Modifier.weight(1f))
                    // Enable/Disable All operate on the FILTERED subset so a
                    // user can bulk-toggle the result of a search.
                    MinisTextButton(
                        onClick = {
                            for (skill in filteredSkills) {
                                overrides[skill.id] = true
                                skillRepository.setSessionOverride(sessionId, skill.id, true)
                            }
                        },
                        contentPadding = androidx.compose.foundation.layout.PaddingValues(
                            horizontal = 8.dp, vertical = 0.dp,
                        ),
                    ) {
                        Text(stringResource(R.string.session_skills_enable_all), fontSize = 12.sp)
                    }
                    MinisTextButton(
                        onClick = {
                            for (skill in filteredSkills) {
                                overrides[skill.id] = false
                                skillRepository.setSessionOverride(sessionId, skill.id, false)
                            }
                        },
                        contentPadding = androidx.compose.foundation.layout.PaddingValues(
                            horizontal = 8.dp, vertical = 0.dp,
                        ),
                    ) {
                        Text(stringResource(R.string.session_skills_disable_all), fontSize = 12.sp)
                    }
                }

                if (filteredSkills.isEmpty()) {
                    Text(
                        text = stringResource(R.string.skills_search_no_match, searchQuery),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 32.dp, vertical = 24.dp),
                    )
                } else {
                    SettingsSection(
                        footer = stringResource(R.string.session_skills_footer),
                    ) {
                        filteredSkills.forEachIndexed { index, skill ->
                            SkillRowItem(
                                name = skill.name,
                                description = skill.description,
                                importSource = skill.importSource,
                                isEnabled = overrides[skill.id] ?: skill.isEnabled,
                                onToggle = { enabled ->
                                    overrides[skill.id] = enabled
                                    skillRepository.setSessionOverride(sessionId, skill.id, enabled)
                                },
                                showDivider = index < filteredSkills.size - 1,
                            )
                        }
                    }
                }

                Spacer(modifier = Modifier.padding(bottom = 24.dp))
            }
        }
    }
}

/**
 * [da1034-b3] Pure seeding merge for [SessionSkillsSheet]'s per-session
 * override map. Extracted to a top-level function so the merge semantics can
 * be unit-tested on the JVM without a Compose/Android runtime (see
 * `SessionSkillOverrideMergeTest`).
 *
 * Contract — deliberately identical to `SessionMcpsSheet`'s seeding effect:
 *  - an id already present in [overrides] is never overwritten, so a toggle
 *    the user flipped in the still-open sheet survives the live list
 *    re-emitting (the write that persists it may not have landed yet);
 *  - ids that are missing get seeded exactly once, through [lookup] (which
 *    reads the stored per-session value);
 *  - ids that vanished from [liveIds] are dropped, so re-adding the same
 *    skill later re-seeds from storage instead of resurrecting a stale
 *    toggle.
 */
internal fun mergeSessionOverrides(
    liveIds: List<String>,
    overrides: MutableMap<String, Boolean>,
    lookup: (String) -> Boolean,
) {
    for (id in liveIds) {
        if (id !in overrides) {
            overrides[id] = lookup(id)
        }
    }
    val live = liveIds.toHashSet()
    overrides.keys.retainAll(live)
}

@Composable
private fun EmptySkillsCard() {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .padding(top = 20.dp)
            .padding(horizontal = 16.dp),
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 32.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Icon(
                Icons.Outlined.Description,
                contentDescription = null,
                modifier = Modifier.size(36.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.3f),
            )
            Text(
                stringResource(R.string.session_skills_empty_title),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                stringResource(R.string.session_skills_empty_subtitle),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.6f),
            )
        }
    }
}
