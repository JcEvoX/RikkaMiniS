// [T-android-text-segmenter] JNI bridge to CppJieba for TextSegmenter's Chinese
// path. Loads the dictionary trie + HMM model once from a directory the Kotlin
// side extracted out of Assets (JNI can't read Assets by path), then serves
// exact-mode (`Cut`) and search-mode (`CutForSearch`) segmentation.
//
// We deliberately build DictTrie + HMMModel directly and share them between a
// MixSegment (exact) and a QuerySegment (search) instead of using the full
// cppjieba::Jieba facade — Jieba pulls in KeywordExtractor, which needs
// idf.utf8 + stop_words.utf8 (~6MB we don't ship). Segmentation only needs the
// dict + HMM model, so this keeps the shipped asset down to ~5.4MB.
//
// Thread-safety: a single global instance guarded by a std::mutex. All C++ is
// wrapped in try/catch so a load failure or a malformed input returns false /
// an empty array to Kotlin rather than crashing the app.

#include <jni.h>
#include <android/log.h>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "cppjieba/DictTrie.hpp"
#include "cppjieba/HMMModel.hpp"
#include "cppjieba/MixSegment.hpp"
#include "cppjieba/PosTagger.hpp"
#include "cppjieba/QuerySegment.hpp"

#define LOG_TAG "JiebaJNI"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

namespace {

// Owns the loaded dictionaries and both segmenters. Constructed once, under the
// global mutex, on the first successful nativeInit call.
struct JiebaState {
    std::unique_ptr<cppjieba::DictTrie> dict;
    std::unique_ptr<cppjieba::HMMModel> hmm;
    std::unique_ptr<cppjieba::MixSegment> mixSeg;      // exact mode (Cut, HMM on)
    std::unique_ptr<cppjieba::QuerySegment> querySeg;  // search-engine mode
};

std::mutex g_mutex;
std::unique_ptr<JiebaState> g_state;

// Join two path components with a single '/'.
std::string joinPath(const std::string& dir, const std::string& file) {
    if (dir.empty()) return file;
    if (dir.back() == '/') return dir + file;
    return dir + "/" + file;
}

// Forward declarations (definitions follow toStdString; the conversion pair
// is kept adjacent to where it is applied).
static std::string mutf8ToUtf8(const std::string& in);
static std::string utf8ToMutf8(const std::string& in);

// Convert a jstring to a std::string (UTF-8). Returns "" on null.
//
// [fix/audit-1015 BUG-06] JNI's GetStringUTFChars/NewStringUTF exchange
// *Modified UTF-8* (MUTF-8), not standard UTF-8: code points above U+FFFF —
// emoji, CJK Extension B+ — travel as CESU-8-style surrogate halves, i.e.
// TWO 3-byte sequences (ED A0..AF 8x 8x + ED B0..BF 8x 8x) instead of one
// 4-byte sequence. cppjieba's DecodeUTF8ToRune reads standard UTF-8, so
// without folding each half became its own rune: 😀 was split into two
// garbage tokens by the segmenter, and words handed back to NewStringUTF
// carried lone surrogates (invalid Java String content). toStdString now
// folds MUTF-8 into standard UTF-8 on the way in and toStringArray unfolds
// on the way out (see utf8ToMutf8 below) — segmentation sees real code
// points and every jstring we build is well-formed again. ASCII (paths,
// plain Chinese) passes through untouched in both directions.
std::string toStdString(JNIEnv* env, jstring s) {
    if (s == nullptr) return std::string();
    const char* chars = env->GetStringUTFChars(s, nullptr);
    if (chars == nullptr) return std::string();
    std::string out = mutf8ToUtf8(std::string(chars));
    env->ReleaseStringUTFChars(s, chars);
    return out;
}

// MUTF-8 -> standard UTF-8. Surrogate pairs (high ED A0..AF .. followed
// immediately by low ED B0..BF ..) collapse into one 4-byte sequence; every
// other byte passes through, so unpaired halves survive verbatim and plain
// ASCII/CJK is a no-op. Pure function over bytes — host-testable.
static std::string mutf8ToUtf8(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    const size_t n = in.size();
    size_t i = 0;
    while (i < n) {
        const unsigned char c0 = (unsigned char)in[i];
        if (c0 == 0xED && i + 5 < n) {
            const unsigned char c1 = (unsigned char)in[i + 1];
            const unsigned char c2 = (unsigned char)in[i + 2];
            const unsigned char c3 = (unsigned char)in[i + 3];
            const unsigned char c4 = (unsigned char)in[i + 4];
            const unsigned char c5 = (unsigned char)in[i + 5];
            if (c1 >= 0xA0 && c1 <= 0xAF && (c2 & 0xC0) == 0x80 &&
                c3 == 0xED && c4 >= 0xB0 && c4 <= 0xBF && (c5 & 0xC0) == 0x80) {
                const unsigned int hi = 0xD800u +
                    (((unsigned int)(c1 & 0x0F) << 6) | (unsigned int)(c2 & 0x3F));
                const unsigned int lo = 0xDC00u +
                    (((unsigned int)(c4 & 0x0F) << 6) | (unsigned int)(c5 & 0x3F));
                const unsigned int cp = 0x10000u +
                    ((hi - 0xD800u) << 10) + (lo - 0xDC00u);
                out += (char)(0xF0 | (cp >> 18));
                out += (char)(0x80 | ((cp >> 12) & 0x3F));
                out += (char)(0x80 | ((cp >> 6) & 0x3F));
                out += (char)(0x80 | (cp & 0x3F));
                i += 6;
                continue;
            }
        }
        out += in[i];
        ++i;
    }
    return out;
}

// standard UTF-8 -> MUTF-8 (exact inverse): each 4-byte sequence (code point
// >= U+10000) expands back into its surrogate pair. Pure function, host-
// testable; mutf8ToUtf8 then utf8ToMutf8 is the identity on both MUTF-8 and
// plain-UTF-8-with-no-above-BMP inputs.
static std::string utf8ToMutf8(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    const size_t n = in.size();
    size_t i = 0;
    while (i < n) {
        const unsigned char c0 = (unsigned char)in[i];
        if (c0 >= 0xF0 && c0 <= 0xF4 && i + 3 < n &&
            ((unsigned char)in[i + 1] & 0xC0) == 0x80 &&
            ((unsigned char)in[i + 2] & 0xC0) == 0x80 &&
            ((unsigned char)in[i + 3] & 0xC0) == 0x80) {
            const unsigned int cp = ((unsigned int)(c0 & 0x07) << 18) |
                (((unsigned char)in[i + 1] & 0x3Fu) << 12) |
                (((unsigned char)in[i + 2] & 0x3Fu) << 6) |
                ((unsigned char)in[i + 3] & 0x3Fu);
            if (cp >= 0x10000u) {
                const unsigned int v = cp - 0x10000u;
                const unsigned int hi = 0xD800u + (v >> 10);
                const unsigned int lo = 0xDC00u + (v & 0x3FFu);
                out += (char)(0xE0 | (hi >> 12));
                out += (char)(0x80 | ((hi >> 6) & 0x3F));
                out += (char)(0x80 | (hi & 0x3F));
                out += (char)(0xE0 | (lo >> 12));
                out += (char)(0x80 | ((lo >> 6) & 0x3F));
                out += (char)(0x80 | (lo & 0x3F));
                i += 4;
                continue;
            }
        }
        out += in[i];
        ++i;
    }
    return out;
}

// Build a Java String[] from a vector of UTF-8 words.
//
// [fix/audit-1015 BUG-06] NewStringUTF expects MUTF-8, so words are unfolded
// back (4-byte sequence -> surrogate pair) before crossing the boundary; a
// word that contains an above-BMP character (😀 / 𠀀) is otherwise handed to
// the JVM as a lone surrogate, which is not a well-formed Java String.
jobjectArray toStringArray(JNIEnv* env, const std::vector<std::string>& words) {
    jclass stringClass = env->FindClass("java/lang/String");
    jobjectArray arr = env->NewObjectArray(static_cast<jsize>(words.size()), stringClass, nullptr);
    if (arr == nullptr) return nullptr;
    for (jsize i = 0; i < static_cast<jsize>(words.size()); ++i) {
        jstring w = env->NewStringUTF(utf8ToMutf8(words[i]).c_str());
        env->SetObjectArrayElement(arr, i, w);
        env->DeleteLocalRef(w);
    }
    return arr;
}

// Empty String[0] — the failure / empty-input return value.
jobjectArray emptyStringArray(JNIEnv* env) {
    jclass stringClass = env->FindClass("java/lang/String");
    return env->NewObjectArray(0, stringClass, nullptr);
}

}  // namespace

extern "C" {

JNIEXPORT jboolean JNICALL
Java_com_rikkaminis_app_shared_JiebaEngine_nativeInit(
    JNIEnv* env, jobject /* thiz */, jstring dictDir) {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_state) return JNI_TRUE;  // already initialized

    const std::string dir = toStdString(env, dictDir);
    if (dir.empty()) {
        LOGE("nativeInit: empty dict dir");
        return JNI_FALSE;
    }

    try {
        auto state = std::make_unique<JiebaState>();
        const std::string dictPath = joinPath(dir, "jieba.dict.utf8");
        const std::string hmmPath = joinPath(dir, "hmm_model.utf8");

        state->dict = std::make_unique<cppjieba::DictTrie>(dictPath);
        state->hmm = std::make_unique<cppjieba::HMMModel>(hmmPath);
        // Both segmenters borrow the same dict/HMM (non-owning pointers).
        state->mixSeg = std::make_unique<cppjieba::MixSegment>(state->dict.get(), state->hmm.get());
        state->querySeg = std::make_unique<cppjieba::QuerySegment>(state->dict.get(), state->hmm.get());

        g_state = std::move(state);
        LOGI("nativeInit: loaded dictionaries from %s", dir.c_str());
        return JNI_TRUE;
    } catch (const std::exception& e) {
        LOGE("nativeInit failed: %s", e.what());
        g_state.reset();
        return JNI_FALSE;
    } catch (...) {
        LOGE("nativeInit failed: unknown error");
        g_state.reset();
        return JNI_FALSE;
    }
}

JNIEXPORT jobjectArray JNICALL
Java_com_rikkaminis_app_shared_JiebaEngine_nativeSegment(
    JNIEnv* env, jobject /* thiz */, jstring text) {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_state) {
        LOGE("nativeSegment before init");
        return emptyStringArray(env);
    }
    try {
        const std::string sentence = toStdString(env, text);
        std::vector<std::string> words;
        // Exact mode with HMM enabled (recovers OOV words).
        g_state->mixSeg->Cut(sentence, words, /* hmm = */ true);
        return toStringArray(env, words);
    } catch (const std::exception& e) {
        LOGE("nativeSegment failed: %s", e.what());
        return emptyStringArray(env);
    } catch (...) {
        LOGE("nativeSegment failed: unknown error");
        return emptyStringArray(env);
    }
}

JNIEXPORT jobjectArray JNICALL
Java_com_rikkaminis_app_shared_JiebaEngine_nativeSegmentForSearch(
    JNIEnv* env, jobject /* thiz */, jstring text) {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_state) {
        LOGE("nativeSegmentForSearch before init");
        return emptyStringArray(env);
    }
    try {
        const std::string sentence = toStdString(env, text);
        std::vector<std::string> words;
        g_state->querySeg->Cut(sentence, words, /* hmm = */ true);
        return toStringArray(env, words);
    } catch (const std::exception& e) {
        LOGE("nativeSegmentForSearch failed: %s", e.what());
        return emptyStringArray(env);
    } catch (...) {
        LOGE("nativeSegmentForSearch failed: unknown error");
        return emptyStringArray(env);
    }
}

// [T-android-voice-correction] Part-of-speech tagging, needed by
// VocabularyFilter's admission scoring (proper-noun tags nr/ns/nz/nt earn +2).
//
// Returns a FLAT String[] of alternating token, tag, token, tag, … rather than
// an array of pairs: building a Pair[] here would mean FindClass +
// GetMethodID + NewObject per token across the JNI boundary, where the flat
// form is one NewStringUTF per element and a trivial pairwise walk in Kotlin.
//
// PosTagger is stateless and cheap to construct (its ctor is empty; it borrows
// the DictTrie from the SegmentTagged we hand it), so unlike KeywordExtractor
// it needs no extra dictionary files and is built per call rather than kept in
// JiebaState.
JNIEXPORT jobjectArray JNICALL
Java_com_rikkaminis_app_shared_JiebaEngine_nativePosTag(
    JNIEnv* env, jobject /* thiz */, jstring text) {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_state) {
        LOGE("nativePosTag before init");
        return emptyStringArray(env);
    }
    try {
        const std::string sentence = toStdString(env, text);
        std::vector<std::pair<std::string, std::string> > tagged;
        cppjieba::PosTagger tagger;
        // MixSegment is a SegmentTagged, so it supplies both the cut and the
        // DictTrie the tag lookup needs.
        tagger.Tag(sentence, tagged, *g_state->mixSeg);
        std::vector<std::string> flat;
        flat.reserve(tagged.size() * 2);
        for (size_t i = 0; i < tagged.size(); ++i) {
            flat.push_back(tagged[i].first);
            flat.push_back(tagged[i].second);
        }
        return toStringArray(env, flat);
    } catch (const std::exception& e) {
        LOGE("nativePosTag failed: %s", e.what());
        return emptyStringArray(env);
    } catch (...) {
        LOGE("nativePosTag failed: unknown error");
        return emptyStringArray(env);
    }
}

}  // extern "C"
