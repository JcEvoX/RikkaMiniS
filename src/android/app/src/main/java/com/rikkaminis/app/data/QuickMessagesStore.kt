package com.rikkaminis.app.data

import android.content.Context
import com.rikkaminis.app.logging.AppLogger
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File
import java.util.UUID

/**
 * [feat/quick-messages] 玄星二开「快捷消息」的 MiniS 适配版。
 *
 * 原版存在 `Settings.quickMessages` 里，并按 `assistant.quickMessageIds` 做多助手订阅。
 * MiniS 是单人格架构（没有 assistants 概念），所以这里收敛为一份全局模板列表：
 * 每个会话的输入框都能直接取用，不再有"按助手订阅"这一步。
 *
 * 持久化：`filesDir/minis-config/quick-messages.json`（与 mounted-folders.json 同级，
 * 刻意放在 `minis-global/` 之外，避免随 DocumentsProvider 暴露的文件树泄漏）。
 */
@Serializable
data class QuickMessage(
    val id: String = UUID.randomUUID().toString(),
    val title: String,
    val content: String,
    val createdAt: Long = System.currentTimeMillis(),
)

class QuickMessagesStore(private val context: Context) {

    private val storeFile: File by lazy {
        File(context.filesDir, "minis-config/quick-messages.json").apply {
            parentFile?.mkdirs()
        }
    }

    private val mutex = Mutex()
    private val _messages = MutableStateFlow<List<QuickMessage>>(emptyList())
    val messages: StateFlow<List<QuickMessage>> = _messages.asStateFlow()

    init {
        loadFromDisk()
    }

    /** 追加一条。标题/正文去空白后为空则拒绝，返回 null。 */
    suspend fun add(title: String, content: String): QuickMessage? = mutex.withLock {
        val t = title.trim()
        val c = content.trim()
        if (t.isEmpty() || c.isEmpty()) return@withLock null
        val entry = QuickMessage(title = t.take(MAX_TITLE_LENGTH), content = c)
        _messages.value = _messages.value + entry
        saveToDisk(_messages.value)
        entry
    }

    /** 按 id 覆盖标题与正文。id 不存在返回 false。 */
    suspend fun update(id: String, title: String, content: String): Boolean = mutex.withLock {
        val t = title.trim()
        val c = content.trim()
        if (t.isEmpty() || c.isEmpty()) return@withLock false
        var changed = false
        _messages.value = _messages.value.map { m ->
            if (m.id == id) {
                changed = true
                m.copy(title = t.take(MAX_TITLE_LENGTH), content = c)
            } else {
                m
            }
        }
        if (changed) saveToDisk(_messages.value)
        changed
    }

    suspend fun remove(id: String): Boolean = mutex.withLock {
        val before = _messages.value
        val after = before.filterNot { it.id == id }
        if (after.size == before.size) return@withLock false
        _messages.value = after
        saveToDisk(after)
        true
    }

    private fun loadFromDisk() {
        if (!storeFile.isFile) return
        runCatching {
            val parsed = JSON.decodeFromString<List<QuickMessage>>(storeFile.readText())
            _messages.value = parsed
        }.onFailure {
            AppLogger.warning(TAG, "loadFromDisk failed: ${it.message}")
        }
    }

    private suspend fun saveToDisk(list: List<QuickMessage>) = withContext(Dispatchers.IO) {
        runCatching {
            storeFile.writeText(JSON.encodeToString(list))
        }.onFailure {
            AppLogger.warning(TAG, "saveToDisk failed: ${it.message}")
        }
    }

    companion object {
        /** 与列表行的单行展示对齐；超长标题截断，正文不设上限。 */
        const val MAX_TITLE_LENGTH = 80
        private const val TAG = "QuickMessages"
        private val JSON = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    }
}