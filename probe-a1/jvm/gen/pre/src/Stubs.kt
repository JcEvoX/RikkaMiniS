package probe

object AppLogger {
    fun warning(category: String, message: String) {
        println("[WARN] " + category + ": " + message)
    }
}

object SystemVoiceIds {
    const val BUILTIN_PROVIDER_ID = "__builtin_system_speech__"
}

object ArtifactBackupScope {
    data class SelectedFile(val relPath: String)
}
