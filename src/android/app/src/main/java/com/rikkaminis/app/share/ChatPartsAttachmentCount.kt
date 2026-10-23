package com.rikkaminis.app.share

import org.json.JSONArray
import org.json.JSONObject

/**
 * Best-effort `(images, videos)` count over a persisted parts_json array.
 *
 * [deep-audit-1047 A-04] `ChatExporter.countAttachments` used to switch on the
 * part NAME alone, comparing it against the pre-mediaRef names (`"image"` /
 * `"video"`). Every row written today uses the `@SerialName` values declared
 * in [com.rikkaminis.app.data.model.ContentPart]:
 *
 *  - `"mediaRef"`, whose `value` is a `MediaRef` (`id` / `relativePath` /
 *    `mimeType` / `originalFileName` / `linuxPath`), written by
 *    `ChatMessageJson.buildMediaRefPartJson`;
 *  - `"toolResult"`, whose `value` carries an optional `mediaRef` object —
 *    the field `com.rikkaminis.app.data.model.ToolResult.mediaRef` declares,
 *    and the shape `SessionForkManager.remapForkedPartsJson` exists to remap
 *    (relevant again for any tool that returns media, such as a screenshot
 *    action).
 *
 * Neither name was matched, so `Summary.imageAttachments` / `videoAttachments`
 * and the exported `session.json` counters stayed `0` for every session — the
 * counters existed, were summed per batch, and were written out, but nothing
 * ever incremented them.
 *
 * The image/video split follows the sibling classifications
 * (`ChatRepository.extractTextPreview` / `extractTextForOffload`): anything
 * with a `video/` MIME is a video, everything else — including an absent or
 * unrecognised MIME — is an image. A name-only count cannot make that call,
 * and defaulting a mixed basket to images is what those readers already do.
 *
 * The legacy names are still counted: rows persisted before the mediaRef
 * migration carry them and their counts must survive this change.
 *
 * Top level (not a member of [ChatExporter]) and Android-free on purpose: a
 * plain JVM test can then pin the classification with a real org.json.
 */
internal fun countPartsAttachments(arr: JSONArray): Pair<Int, Int> {
    var img = 0
    var vid = 0
    for (i in 0 until arr.length()) {
        val obj = arr.optJSONObject(i) ?: continue
        val value = obj.optJSONObject("value")
        when (obj.optString("type")) {
            "mediaRef" -> {
                if (isVideoMediaRef(value)) vid += 1 else img += 1
            }
            // A tool result declares its own optional MediaRef (see
            // ToolResult.mediaRef) — counting it is what makes tool-produced
            // media land in the export count.
            "toolResult" -> {
                val media = value?.optJSONObject("mediaRef")
                if (media != null) {
                    if (isVideoMediaRef(media)) vid += 1 else img += 1
                }
            }
            "image", "image_url" -> img += 1
            "video", "video_url" -> vid += 1
        }
    }
    return img to vid
}

/**
 * Does [media] carry a video MIME?
 *
 * Only an explicit `video/` MIME prefix counts as video; anything else — including a
 * missing `mimeType` — is treated as an image, matching how every sibling
 * reader sizes up an unclassifiable MediaRef.
 */
internal fun isVideoMediaRef(media: JSONObject?): Boolean {
    if (media == null) return false
    val mime = media.optString("mimeType")
    return mime.startsWith("video/", ignoreCase = true)
}
