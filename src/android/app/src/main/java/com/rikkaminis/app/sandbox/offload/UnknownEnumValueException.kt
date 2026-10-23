package com.rikkaminis.app.sandbox.offload

/**
 * [T-model-exec-strict-enum] Thrown when the worker receives an enum value
 * it doesn't recognize — typically because the main process serialized a
 * NEWER enum case that this (older) worker build doesn't know about. The
 * outer catch in executeRun / executeStreamingRun writes an error result /
 * stream error line; the main process's dispatch() retry then spawns a
 * fresh worker that reads the correct enum on its first-ever prefs load.
 *
 * Replaces the old silent-fallback-to-default behavior which could route
 * an Anthropic request through the OpenAI protocol (or vice versa) and
 * produce a confusing 400 / parse failure that looked like a provider bug.
 *
 * [dv27-A02] Lives in its own file so the wire-level enum parsers
 * (ThinkingLevelWire.kt) can be compiled and unit-tested on a plain JVM
 * without dragging the whole Android Service in.
 */
internal class UnknownEnumValueException(
    val enumClass: String,
    val unknownValue: String,
) : RuntimeException("unknown $enumClass value: $unknownValue")
