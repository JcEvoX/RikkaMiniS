package com.rikkaminis.app.tools

import android.content.Context
import android.graphics.Bitmap
import com.rikkaminis.app.data.model.AgentToolDefinition
import com.rikkaminis.app.data.model.AgentToolParam
import com.rikkaminis.app.sandbox.PRootKernel
import com.rikkaminis.app.ui.chat.decodeUriPath
import com.rikkaminis.app.util.ImageDecoding
import org.json.JSONObject
import java.io.ByteArrayOutputStream

object ReadImageTool {
    const val NAME = "read_image"

    fun definition(): AgentToolDefinition = AgentToolDefinition(
        name = NAME,
        description = "Read an image file from the Linux filesystem and return it for visual analysis. Supports PNG, JPEG, GIF, WEBP, and other common image formats. Use this to inspect generated charts, downloaded images, screenshots, or any visual output. The image is returned directly for your analysis along with metadata (dimensions, file size).",
        parameters = mapOf(
            "tool_title" to AgentToolParam("string", "A concise 5-10 word summary of what this tool call does, shown to the user (e.g. 'View generated bar chart', 'Inspect downloaded screenshot'). Use the same language as the user."),
            "path" to AgentToolParam("string", "Linux path (e.g. /var/minis/attachments/chart.png) or minis:// URL (e.g. minis://attachments/chart.png)"),
        ),
        required = listOf("tool_title", "path"),
        propertyOrdering = listOf("tool_title", "path"),
    )

    /**
     * T178: when the caller knows the owning session, prefer
     * [PRootKernel.resolveSessionHostPath] so per-session subdirs
     * (`/var/minis/{attachments,workspace,offloads,browser}/...`) resolve
     * directly against this session's host dir instead of consulting the
     * global, last-writer-wins `bindMounts` map. Without this, an agent
     * loop in session A that calls `read_image` after session B booted
     * its PRoot reads from session B's host dir — confirmed leak per
     * docs/parity/cross-session-isolation-audit.md.
     *
     * The legacy single-arg overload is preserved for callers that don't
     * know the session id (and falls back to the global map). Mirror iOS
     * `ReadImageTool` which carries `sessionId` through its tool-call
     * pipeline.
     */
    fun execute(argsJson: String, sessionId: String? = null, context: Context? = null): ToolExecutionResult {
        // [fix/da1022-G] Resolve the title BEFORE the try (FileWriteTool pattern):
        // a try-scoped val left the catch arm with no title to report.
        val toolTitle = resolveToolTitle(argsJson, NAME)
        return try {
            val args = JSONObject(argsJson)
            val rawPath = args.optString("path", "")

            if (rawPath.isBlank()) {
                return ToolExecutionResult("Error: 'path' is required", false, toolTitle = toolTitle)
            }

            val path = if (rawPath.startsWith("minis://")) {
                // [rv1014-B2] decodeUriPath = URI path semantics (decodes %XX only,
            // leaves `+` intact, never throws on bare `%`); the previous
            // URLDecoder.decode corrupted filenames containing `+` and threw
            // on `100%.png`-style names.
            "/var/minis/" + decodeUriPath(rawPath.removePrefix("minis://"))
            } else rawPath

            // [audit3 F-08] The session-vs-global choice lives in
            // resolvePathForSession: with a session in scope a path it rejects
            // (escape out of the session tree) is NOT re-resolved by the global
            // resolver — the null check below is now only "cannot resolve at
            // all". The legacy overload (no sessionId/context) keeps the global
            // resolver, per the KDoc above.
            val file = when {
                sessionId != null && context != null ->
                    PRootKernel.resolvePathForSession(sessionId, path, context)
                else -> PRootKernel.resolveHostPath(path)
            }
            if (file == null) {
                return ToolExecutionResult("Error: Cannot resolve path: $path", false, toolTitle = toolTitle)
            }

            if (!file.exists()) {
                return ToolExecutionResult("Error: File not found: $path", false, toolTitle = toolTitle)
            }

            // [audit2-PERF-03] Bounded two-pass decode (bounds -> inSampleSize
            // -> decode) through the single shared entry point. The old
            // `BitmapFactory.decodeFile(path)` with no Options allocated the
            // full-resolution bitmap first: a 12 Mpx photo is ~48 MB (8000x6000
            // ~192 MB) of transient ARGB_8888 against a 256-512 MB heap, and the
            // createScaledBitmap copy landed on top of it.
            val maxEdge = 2000
            val decoded = ImageDecoding.decodeFileBounded(file.absolutePath, maxEdge)
                ?: return ToolExecutionResult("Error: Cannot decode image: $path", false, toolTitle = toolTitle)

            // Source dimensions come from the bounds probe, so nothing here can
            // repeat the [fix/audit-s6h1] failure (reading width/height off a
            // recycled bitmap threw "Can't call getWidth() on a recycled
            // bitmap"). Recycle the one bitmap the decoder handed us.
            val width = decoded.sourceWidth
            val height = decoded.sourceHeight
            val imageBytes: ByteArray
            try {
                val out = ByteArrayOutputStream()
                decoded.bitmap.compress(Bitmap.CompressFormat.JPEG, 85, out)
                imageBytes = out.toByteArray()
            } finally {
                // [audit-0917] Recycle on the error path too: a throw between the
                // decode and the old explicit recycles used to leak the bitmap
                // until GC (native memory). recycle() is idempotent.
                decoded.bitmap.recycle()
            }
            val metadata = "[$path | ${width}x${height} | ${file.length()} bytes]"
            ToolExecutionResult(
                output = metadata,
                success = true,
                imageData = imageBytes,
                imageMimeType = "image/jpeg",
                toolTitle = toolTitle,
                // Surface the source file for inline preview in the tool result UI
                // (mirrors iOS ToolLiveSheet.readImageTool case).
                imageFilePath = file.absolutePath,
            )
        } catch (e: Exception) {
            ToolExecutionResult("Error reading image: ${e.message}", false, toolTitle = toolTitle)
        }
    }
}
