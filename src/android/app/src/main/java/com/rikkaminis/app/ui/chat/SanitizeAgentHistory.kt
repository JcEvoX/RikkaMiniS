package com.rikkaminis.app.ui.chat

import com.rikkaminis.app.data.model.AgentContentPart
import com.rikkaminis.app.data.model.LLMMessage

/**
 * Neutral placeholder injected when a history slice carries an assistant
 * tool_use whose tool_result never landed (interrupted turn / compact slice
 * boundary). Wording deliberately does NOT tell the model to re-run the call —
 * the tool may or may not have completed.
 */
internal const val SANITIZE_PLACEHOLDER_RESULT_CONTENT =
    "Tool execution was interrupted; the tool may or may not have completed. Do not blindly re-issue the same tool call — first check the conversation and any prior results."

/**
 * [T-compact-slice-tool-pairing] Repairs the tool_use / tool_result pairing
 * on any message list (full history OR compacted slice) so no outgoing
 * request carries a dangling tool message.
 *
 * Pure + JVM-testable (no ViewModel/Android dependencies). The optional
 * [log] sink keeps the pure function Android-free (default no-op); the
 * production shell in ChatViewModel injects Log.w so the sanitize action is
 * observable in logcat under the ChatViewModel tag.
 */
internal fun sanitizeAgentHistoryMessagesImpl(
    messages: MutableList<LLMMessage>,
    log: (String) -> Unit = {},
) {
    // Walk through history sequentially, checking each assistant message.
    // For each assistant message with tool_use blocks, verify the NEXT message
    // is a user message with matching tool_result blocks. If not, inject them.
    var i = 0
    while (i < messages.size) {
        val msg = messages[i]
        if (msg.role != LLMMessage.Role.ASSISTANT) { i++; continue }

        val toolUses = msg.contentParts.filterIsInstance<AgentContentPart.ToolUse>()
        if (toolUses.isEmpty()) { i++; continue }

        val toolUseIds = toolUses.map { it.id }.toSet()

        // Check next message for matching tool_results
        val next = messages.getOrNull(i + 1)
        val nextResultIds = next?.contentParts
            ?.filterIsInstance<AgentContentPart.ToolResult>()
            ?.map { it.id }?.toSet() ?: emptySet()

        val missingIds = toolUseIds - nextResultIds
        if (missingIds.isEmpty()) { i++; continue }

        // Some tool_uses have no matching tool_result in the next message.
        // If next message is a user message, add the missing results to it.
        // Otherwise, inject a new user message with placeholder results.
        val placeholders = toolUses.filter { it.id in missingIds }.map { use ->
            AgentContentPart.ToolResult(
                id = use.id, name = use.name,
                content = SANITIZE_PLACEHOLDER_RESULT_CONTENT,
                isError = true,
            )
        }
        log("sanitize: injecting ${placeholders.size} placeholder tool_result(s) after history[$i]")

        // [fix/audit-0917-b9] Merging used to require next to ALREADY carry a
        // ToolResult; a plain-text USER neighbour (interrupted assistant turn)
        // fell through to the insert branch, producing
        // [assistant(tool_use), user(placeholders), user(original text)] —
        // two consecutive USER messages (Anthropic hard 400, OpenAI silent
        // merge). Any USER message can legally carry a tool_result block, so
        // every USER neighbour merges; only ASSISTANT neighbours insert.
        if (next != null && next.role == LLMMessage.Role.USER) {
            // [fix/audit-1015-arch03] Anthropic requires tool_result blocks
            // to precede text blocks within the same user message — prepend
            // placeholders instead of appending. Serialization emits parts
            // in order (Anthropic:727 walks contentParts verbatim); OpenAI
            // extracts tool_results by type and emits role:"tool" messages
            // first regardless of part order, so the reorder is safe there.
            // If `next` carries only top-level `content` (no parts), lift it
            // into a trailing Text part first — otherwise serialization
            // (which ignores `content` when parts are non-empty) would drop
            // the original text entirely.
            val liftedContent = next.contentParts.isEmpty() && next.content.isNotEmpty()
            val existingParts = if (liftedContent) {
                listOf<AgentContentPart>(AgentContentPart.Text(next.content))
            } else {
                next.contentParts
            }
            messages[i + 1] = next.copy(
                content = if (liftedContent) "" else next.content,
                contentParts = placeholders + existingParts
            )
        } else {
            // Insert a new user message with just the placeholder results
            messages.add(i + 1, LLMMessage(
                role = LLMMessage.Role.USER, content = "",
                contentParts = placeholders,
            ))
        }
        i++
    }

    // Remove orphaned tool_results (result IDs not found in any tool_use)
    val allToolUseIds = messages.flatMap { it.contentParts }
        .filterIsInstance<AgentContentPart.ToolUse>().map { it.id }.toSet()
    val iter = messages.listIterator()
    while (iter.hasNext()) {
        val msg = iter.next()
        if (msg.role != LLMMessage.Role.USER) continue
        val cleaned = msg.contentParts.filter { part ->
            part !is AgentContentPart.ToolResult || part.id in allToolUseIds
        }
        if (cleaned.isEmpty() && msg.content.isBlank()) {
            iter.remove()
        } else if (cleaned.size < msg.contentParts.size) {
            iter.set(msg.copy(contentParts = cleaned))
        }
    }
}

/**
 * [T-consecutive-user-bridge] Enforce "roles must alternate" just before a
 * fresh user message is appended to history from an entry point that is NOT
 * inside the agent-loop tool-result cycle ([sendMessage] /
 * [drainQueuedPrompts]).
 *
 * Pure + JVM-testable (no ViewModel dependencies).
 */
internal fun ensureRoleAlternationBeforeUserAppend(
    history: MutableList<LLMMessage>,
    bridgeText: String = "(Interrupted mid-task by a new user message. Decide based on the new message and overall context whether the prior task should continue — do not forget or abandon it unless the user explicitly says to stop, or the new message makes clear it is no longer needed.)",
) {
    if (history.lastOrNull()?.role == LLMMessage.Role.USER) {
        history.add(
            LLMMessage(
                role = LLMMessage.Role.ASSISTANT,
                content = "",
                contentParts = listOf(AgentContentPart.Text(bridgeText)),
            ),
        )
    }
}

/**
 * [audit-1015 BUG-03B/3C] Whether [msg] is a BLANK assistant shell: a message
 * the agent loop appends as a turn placeholder that carries nothing usable —
 * empty content, no content parts, AND no reasoning content. Both provider
 * request builders filter such messages out
 * (`.filter { contentParts.isNotEmpty() || content.isNotEmpty() }`), so a
 * blank shell is invisible to the model — but it still breaks role
 * alternation inside the in-memory history.
 *
 * The predicate is deliberately precise to THREE fields: a shell carrying
 * reasoningContent is NOT blank (the reasoning round-trips on the next
 * request for thinking-enabled models — DeepSeek V4 rejects assistant turns
 * lacking reasoning_content), so it must never be dropped.
 *
 * Pure + JVM-testable (no ViewModel/Android dependencies).
 */
internal fun isBlankAssistantShell(msg: LLMMessage): Boolean =
    msg.role == LLMMessage.Role.ASSISTANT &&
        msg.content.isEmpty() &&
        msg.contentParts.isEmpty() &&
        msg.reasoningContent.isNullOrEmpty()

/**
 * [audit-1015 BUG-03B/3C] Drop the trailing history entry when it is a blank
 * assistant shell ([isBlankAssistantShell]). Called before (a) the empty
 * length-wall retry `continue` (BUG-03B — the just-added empty turn must not
 * ride into the retry request) and (b) the nudge paths' role-alternation
 * bridge (BUG-03C — an undropped blank shell is filtered out of the request
 * by the provider, turning [..., shell, USER] into [..., USER, USER]).
 *
 * Pure + JVM-testable (no ViewModel/Android dependencies).
 */
internal fun dropTrailingBlankAssistantShell(history: MutableList<LLMMessage>) {
    if (history.isNotEmpty() && isBlankAssistantShell(history.last())) {
        history.removeAt(history.size - 1)
    }
}