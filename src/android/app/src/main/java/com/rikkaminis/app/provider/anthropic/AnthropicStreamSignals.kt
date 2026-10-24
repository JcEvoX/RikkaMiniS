package com.rikkaminis.app.provider.anthropic

/**
 * [dv29 A-01] SINGLE definition of "this SSE event represents tool-call
 * progress" for the Anthropic Messages protocol.
 *
 * `AnthropicProvider.streamChat` keeps one turn-level activity counter
 * (`toolChars`) that the EOF truncation guard at the end of the loop compares
 * against zero. The guard exists because a stream can die (network EOF, relay
 * cut) before the terminal `message_delta`: without a positive counter the
 * provider emits no `Finished(truncated = true)`, the downstream turn flag
 * stays false, `TruncatedToolCallPolicy` never fires, and a tool call whose
 * arguments were still in flight reaches the executor with `{}` or with a
 * prefix object.
 *
 * The counter used to be fed from ONE of the protocol's three tool lifecycle
 * points — the argument delta. A pure-tool turn (no text/thinking, so the
 * content counter stays 0) cut between the `tool_use` block announcement and
 * the first non-empty delta therefore looked like a completely empty stream:
 * the orphan-flush below it still emitted `ToolCallComplete(id, name, {})`,
 * so `failOnSilentEmptyCompletion` stayed quiet too, and an argument-less call
 * was executed for real.
 *
 * All three lifecycle points now report through these functions, so the
 * mapping has one home instead of three inline expressions:
 *
 *  - [toolUseStartActivityChars] — `content_block_start` with
 *    `type == "tool_use"`. This is the FIRST proof the turn produced a tool
 *    call; it arrives before any argument byte, so a server that closes right
 *    after it must still count as progress. The `maxOf(..., 1)` floor is the
 *    point: a zero-argument tool carries no payload, yet its announcement is
 *    exactly the event a cut-after-start stream leaves behind.
 *  - [toolInputDeltaActivityChars] — `content_block_delta` with
 *    `type == "input_json_delta"`. Empty deltas are keep-alives on some
 *    relays; they carry no progress.
 *  - [toolUseStopActivityChars] — `content_block_stop` of a tool block. By
 *    then the delta feed has normally already counted the arguments; this is
 *    defence in depth for a relay that completes a zero-argument block
 *    without ever sending a delta, and for a future refactor that moves the
 *    start feed.
 *
 * Deliberately NOT included: `content_block_start` for `text` / `thinking`
 * block headers. Those announce a block, not content — counting them would
 * turn "server opened a text block then died" into a truncation retry and
 * steal the fully-empty case from `failOnSilentEmptyCompletion`, which owns
 * it (see the guard's KDoc in the provider).
 *
 * Pure and Android-free on purpose: the SSE loop cannot run without the
 * Android runtime, so the decision table is only testable when it lives
 * outside the loop (see `AnthropicStreamSignalsTest`).
 */
internal fun toolUseStartActivityChars(toolId: String, toolName: String): Int =
    // The call exists even when it carries no arguments and no name — the
    // floor is what makes "announced, then cut" observable at all.
    maxOf(toolId.length, toolName.length, 1)

/**
 * Weight of one streamed argument fragment.
 *
 * @return the fragment length, or 0 for the empty `partial_json` some relays
 *   send as a keep-alive (an empty fragment is not progress).
 */
internal fun toolInputDeltaActivityChars(partialJson: String): Int =
    if (partialJson.isEmpty()) 0 else partialJson.length

/**
 * Weight of a completed tool block (`content_block_stop`, and the orphan-flush
 * that stands in for a missing one). Same floor as
 * [toolUseStartActivityChars]: a completed zero-argument call is still a call
 * that a cut-before-`message_delta` stream must be flagged on.
 */
internal fun toolUseStopActivityChars(toolId: String, toolName: String): Int =
    maxOf(toolId.length, toolName.length, 1)
