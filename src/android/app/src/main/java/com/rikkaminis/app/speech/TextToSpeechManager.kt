package com.rikkaminis.app.speech

import android.content.Context
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.util.Locale
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

/**
 * Manages Android TextToSpeech engine for reading assistant responses aloud.
 * Supports en-US and zh-CN with automatic language detection.
 */
class TextToSpeechManager : TextToSpeech.OnInitListener {

    companion object {
        private const val TAG = "TextToSpeech"
        private val HAN_REGEX = Regex("[\\u4e00-\\u9fff\\u3400-\\u4dbf]")
        // [T-android-tts-intranumber-guard] The sentence-boundary set moved to
        // SpeechSentenceSplitter.SENTENCE_ENDERS — a single source of truth
        // shared by every TTS path, so they can't drift.
    }

    private var tts: TextToSpeech? = null
    var isInitialized = false
        private set

    private val _isSpeaking = MutableStateFlow(false)
    val isSpeaking: StateFlow<Boolean> = _isSpeaking.asStateFlow()

    // [fix/audit-b20 / T8-L5] Queue-drain bookkeeping. The pause/resume layer
    // (isPaused / togglePause / pausedAtIndex) had no caller anywhere — Android
    // TTS has no native pause and no UI ever exposed one — so it is gone. The
    // drain tracker is what keeps `isSpeaking` accurate: utterances handed to
    // the engine, and utterances that finished (done or error).
    // [sweep1014-B20b] Session epoch: speak()/stop() start a new epoch and
    // every utterance id carries its epoch prefix, so callbacks from a flushed
    // session can never corrupt the current one. The two plain Int counters
    // also moved into the tracker as atomics — they were written on the main
    // thread (speak) and read/written on the Binder thread (onDone/onError),
    // with no visibility guarantee.
    private val drain = TtsDrainTracker()

    /**
     * Rolling buffer of partial-streaming text that hasn't hit a sentence
     * boundary yet. Cleared by [flush] or [stop]. See [appendText].
     */
    private val sentenceBuffer = StringBuilder()

    var speechRate: Float = 1.0f
        set(value) {
            field = value.coerceIn(0.1f, 3.0f)
            tts?.setSpeechRate(field)
        }

    var speechPitch: Float = 1.0f
        set(value) {
            field = value.coerceIn(0.5f, 2.0f)
            tts?.setPitch(field)
        }

    var speechVolume: Float = 1.0f
        set(value) {
            field = value.coerceIn(0f, 1f)
        }

    /**
     * Initializes the TTS engine. Must be called before speaking.
     *
     * [T-android-tts-init-leak] Re-init is defensive: shutdown the previous
     * engine first so a repeated [init] doesn't leak the prior [TextToSpeech]
     * instance (which holds native audio resources until shutdown).
     */
    fun init(context: Context) {
        tts?.let { old -> old.shutdown() }
        tts = null
        isInitialized = false
        tts = TextToSpeech(context.applicationContext, this)
    }

    override fun onInit(status: Int) {
        if (status == TextToSpeech.SUCCESS) {
            isInitialized = true
            tts?.apply {
                setSpeechRate(speechRate)
                setPitch(speechPitch)
                setLanguage(Locale.US)
                setOnUtteranceProgressListener(createProgressListener())
            }
            Log.d(TAG, "TTS initialized successfully")
        } else {
            Log.e(TAG, "TTS initialization failed with status: $status")
        }
    }

    /**
     * Speaks text immediately, interrupting any ongoing speech (QUEUE_FLUSH).
     * Automatically detects language from text content.
     */
    fun speak(text: String) {
        if (!isInitialized || text.isBlank()) return

        // [sweep1014-B20b] Bump the epoch BEFORE enqueueing: the utterance the
        // engine flushes still delivers onDone/onError asynchronously — with a
        // new epoch those stale callbacks are ignored instead of corrupting
        // this session's fresh counters.
        val sessionEpoch = drain.newSession()
        sentenceBuffer.setLength(0)

        autoDetectAndSetLanguage(text)

        val params = buildSpeechParams()
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, params, drain.utteranceId(sessionEpoch))
        _isSpeaking.value = true
    }

    /**
     * Adds text to the speech queue without interrupting (QUEUE_ADD).
     */
    fun speakQueued(text: String) {
        if (!isInitialized || text.isBlank()) return

        val utteranceId = drain.utteranceId(drain.enqueue())
        autoDetectAndSetLanguage(text)

        val params = buildSpeechParams()
        tts?.speak(text, TextToSpeech.QUEUE_ADD, params, utteranceId)
        _isSpeaking.value = true
    }

    /**
     * Feed a chunk of streaming AI output. Any complete sentences the chunk
     * produces (boundary characters: `。！？.!?\n` — same set as iOS
     * `extractNewSentences` in AIChatViewModel) are immediately enqueued for
     * TTS via [speakQueued], so the first sentence starts speaking the moment
     * its terminator arrives instead of waiting for the full response.
     *
     * Incomplete tail text stays in [sentenceBuffer] until the next call, or
     * until [flush] runs at stream end.
     *
     * Safe to call before [init]/onInit — chunks are still split; the TTS
     * engine simply drops them if not yet ready (speakQueued guards).
     */
    fun appendText(chunk: String) {
        if (chunk.isEmpty()) return
        sentenceBuffer.append(chunk)
        val sentences = extractCompleteSentences(sentenceBuffer)
        if (sentences.isEmpty()) return
        for (sentence in sentences) {
            speakQueued(sentence)
        }
    }

    /**
     * Emit the remaining buffered tail (a sentence fragment with no trailing
     * punctuation) as a final utterance. Call at the end of a streaming
     * response. Mirrors iOS's post-stream "flush remaining speech" logic.
     */
    fun flush() {
        if (sentenceBuffer.isEmpty()) return
        val tail = sentenceBuffer.toString().trim()
        sentenceBuffer.setLength(0)
        if (tail.isNotEmpty()) {
            speakQueued(tail)
        }
    }

    /**
     * [T-android-tts-intranumber-guard] Delegates to the shared
     * [SpeechSentenceSplitter] so this path and the read-aloud UI path apply the
     * same terminator set AND the same intra-number guards ("3.14" is never cut
     * into "3." / "14").
     * "3." + "14").
     */
    private fun extractCompleteSentences(buffer: StringBuilder): List<String> =
        SpeechSentenceSplitter.extractCompleteSentences(buffer)

    /**
     * Stops all speech immediately.
     */
    fun stop() {
        tts?.stop()
        // [sweep1014-B20b] Invalidate every in-flight callback: utterances the
        // engine flushes still deliver onDone/onError asynchronously — without
        // the epoch bump a stale callback would push the next session's
        // counters (isSpeaking dropped to false the moment a fresh speak()
        // started).
        drain.invalidate()
        sentenceBuffer.setLength(0)
        _isSpeaking.value = false
    }

    /**
     * [fix/audit-b20 / T8-L5] Advances the drain counter and clears the
     * speaking flag once the queue empties. Called from both onDone and the
     * error overloads so a failed utterance still counts as finished — without
     * that, `isSpeaking` stayed true after an error.
     *
     * [sweep1014-B20b] Only callbacks belonging to the CURRENT session count:
     * a flushed utterance's late onDone/onError must not advance the new
     * session's counters (it extinguished a fresh speak() the moment it
     * started). Stale/unknown ids are side-effect free no-ops.
     */
    private fun onUtteranceFinished(utteranceId: String?) {
        if (drain.onFinished(utteranceId)) {
            _isSpeaking.value = false
        }
    }

    /**
     * Sets the TTS language explicitly.
     */
    fun setLanguage(locale: Locale) {
        if (!isInitialized) return
        val result = tts?.setLanguage(locale)
        if (result == TextToSpeech.LANG_MISSING_DATA || result == TextToSpeech.LANG_NOT_SUPPORTED) {
            Log.w(TAG, "Language not supported: ${locale.toLanguageTag()}")
        } else {
            Log.d(TAG, "Language set to: ${locale.toLanguageTag()}")
        }
    }

    /**
     * Cleans up the TTS engine. Must be called when no longer needed.
     */
    fun shutdown() {
        stop()
        tts?.shutdown()
        tts = null
        isInitialized = false
        Log.d(TAG, "TTS shut down")
    }

    /**
     * Detects if text contains Han characters and sets language accordingly.
     */
    private fun autoDetectAndSetLanguage(text: String) {
        val locale = if (HAN_REGEX.containsMatchIn(text)) {
            Locale.SIMPLIFIED_CHINESE
        } else {
            Locale.US
        }
        tts?.setLanguage(locale)
    }

    private fun buildSpeechParams(): android.os.Bundle {
        return android.os.Bundle().apply {
            putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, speechVolume)
        }
    }

    private fun createProgressListener(): UtteranceProgressListener =
        object : UtteranceProgressListener() {

            override fun onStart(utteranceId: String?) {
                // [sweep1014-B20b] A stale onStart from a flushed session must
                // not re-light the flag after stop().
                if (drain.isCurrent(utteranceId)) {
                    _isSpeaking.value = true
                }
            }

            override fun onDone(utteranceId: String?) {
                onUtteranceFinished(utteranceId)
            }

            @Deprecated("Deprecated in API level 21")
            override fun onError(utteranceId: String?) {
                Log.e(TAG, "TTS error for utterance: $utteranceId")
                onUtteranceFinished(utteranceId)
            }

            override fun onError(utteranceId: String?, errorCode: Int) {
                Log.e(TAG, "TTS error for utterance $utteranceId, code: $errorCode")
                onUtteranceFinished(utteranceId)
            }
        }
}

/**
 * [sweep1014-B20b] Session-scoped drain tracker for the TTS queue. speak()
 * (QUEUE_FLUSH) and stop() start a new epoch; every utterance id carries its
 * epoch prefix, so progress callbacks from a flushed session are recognized
 * as stale and ignored — they used to push the fresh session's counters and
 * extinguish `isSpeaking` the moment a new speak() started.
 *
 * Pure JVM-testable: no Android dependencies.
 */
internal class TtsDrainTracker {

    private val epoch = AtomicLong(0)
    private val queued = AtomicInteger(0)
    private val completed = AtomicInteger(0)

    /**
     * Starts a new session (speak / QUEUE_FLUSH): resets the drain counters
     * for the fresh queue and returns the session's epoch.
     */
    fun newSession(): Long {
        val e = epoch.incrementAndGet()
        queued.set(1)
        completed.set(0)
        return e
    }

    /** Adds one utterance to the CURRENT session (speakQueued / QUEUE_ADD). */
    fun enqueue(): Long {
        queued.incrementAndGet()
        return epoch.get()
    }

    /** Invalidates every in-flight callback (stop / shutdown). */
    fun invalidate() {
        epoch.incrementAndGet()
        queued.set(0)
        completed.set(0)
    }

    fun utteranceId(sessionEpoch: Long): String = "$PREFIX$sessionEpoch-${UUID.randomUUID()}"

    /** True when [utteranceId] belongs to the current session. */
    fun isCurrent(utteranceId: String?): Boolean =
        sessionEpochOf(utteranceId) == epoch.get()

    /**
     * Counts a finished (done or error) utterance. Returns true only when the
     * callback belongs to the CURRENT session and the queue has drained —
     * stale callbacks from a flushed session return false without touching
     * the counters.
     */
    fun onFinished(utteranceId: String?): Boolean {
        if (sessionEpochOf(utteranceId) != epoch.get()) return false
        return completed.incrementAndGet() >= queued.get()
    }

    companion object {
        private const val PREFIX = "tts-"

        fun sessionEpochOf(utteranceId: String?): Long? =
            utteranceId
                ?.takeIf { it.startsWith(PREFIX) }
                ?.removePrefix(PREFIX)
                ?.substringBefore('-')
                ?.toLongOrNull()
    }
}
