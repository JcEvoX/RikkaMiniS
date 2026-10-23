package com.rikkaminis.app.util

/**
 * [deep-audit-1047 B-01] The one success decision behind every "write a bitmap
 * to a stream and then tell the user it worked" site (gallery save, clipboard
 * copy, share sheet, table-image copy, SAF save-as).
 *
 * The defect this exists for: BOTH halves of the write can fail **without
 * throwing**, and each call site read as if success were guaranteed.
 *
 *  * `ContentResolver.openOutputStream(uri)` returns **null** when a provider
 *    dies or refuses the write — it does not throw. `stream?.use { … }` then
 *    skips the whole write and execution falls through to "publish the row" /
 *    "hand out the FileProvider URI" / "Toast: saved".
 *  * `Bitmap.compress(...)` returns **false** when the encoder could not write
 *    the pixels at all. Dropped on the floor, it leaves a 0-byte file that is
 *    then wrapped in a content:// URI and pasted / shared / published as a
 *    gallery entry.
 *
 * The properties the call sites actually need are asymmetric, so they are
 * spelled out here instead of being re-invented per site:
 *
 *  * [saveOutcome] answers "did the bytes land?" — both halves must have
 *    happened. A site that already threw on a null stream passes
 *    `streamOpened = true` and gets the encode half checked.
 *
 * Deliberately Android-free (`java.io` never appears here either): the decision
 * is what the JVM arms pin, while the resolver / File plumbing stays at the
 * call site which owns the Context.
 */
internal fun saveOutcome(streamOpened: Boolean, compressed: Boolean): Boolean =
    streamOpened && compressed
