// T283 — native crash → file (NDK signal handler).
//
// Registered at app startup from MinisApp.onCreate via JNI. Catches
// fatal signals raised inside JNI / proot / pty_bridge / any other
// native code, writes a one-shot text report to the configured logs
// dir, then restores the previous handler and re-raises so the system
// tombstone is also generated and the app exits like normal.
//
// Strict async-signal-safety: only signal-safe libc calls inside the
// handler (open/write/close/snprintf are safe; printf/malloc are not).

#include <jni.h>
#include <signal.h>
#include <unistd.h>
#include <fcntl.h>
#include <cstring>
#include <cstdio>
#include <ctime>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <android/log.h>
#include "crash_report_fields.h"

#define LOG_TAG "MinisCrashHandler"

// Plenty of headroom for "<logs_dir>/native-crash-YYYY-MM-DD_HH-MM-SS.log".
static char g_log_dir[512] = {0};

// Reentrancy guard. If the handler crashes itself, we want the second
// signal to skip straight to the previous handler rather than recursing.
static volatile sig_atomic_t g_in_handler = 0;

// [fix/sweep1014-crash-cpp] Previous handlers, saved at install time.
// On Android the pre-installed handler is debuggerd's — it is what
// produces the tombstone and the `logcat -b crash` entry. Installing
// SIG_DFL directly (the old behavior) silently defeated it, so we save
// each signal's previous handler and restore it before re-raising.
#define MINIS_MAX_SIGNAL 32
static struct sigaction g_old_sa[MINIS_MAX_SIGNAL];
static volatile sig_atomic_t g_old_saved[MINIS_MAX_SIGNAL];

// ---- async-signal-safe helpers for richer crash context ----
//
// Everything here avoids malloc/printf/fopen: only open/read/write/close
// (async-signal-safe per POSIX) plus manual integer formatting.

// ---- async-signal-safe /proc readers ----
//
// One read of /proc/self/status now feeds every field we report (the
// previous per-key reader opened the file once per field). The parsing
// itself lives in crash_report_fields.h so the same source lines are
// host-testable (scripts/native/crash_fields_host_test.sh).

// Reads up to bufsize-1 bytes of a text file into buf (NUL-terminated).
// Returns the number of bytes read (0 on failure). open/read/close only.
static size_t read_proc_text(const char* path, char* buf, size_t bufsize) {
    if (buf == nullptr || bufsize == 0) return 0;
    buf[0] = '\0';
    int fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    size_t total = 0;
    while (total + 1 < bufsize) {
        ssize_t r = read(fd, buf + total, bufsize - 1 - total);
        if (r <= 0) break;
        total += (size_t)r;
    }
    close(fd);
    buf[total] = '\0';
    return total;
}

// Counts lines in a /proc file without buffering it (/proc/self/maps can run
// to hundreds of KB; the handler must not allocate). -1 on failure.
static long count_proc_lines(const char* path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    char chunk[1024];
    long lines = 0;
    bool trailing_nl = true;  // empty file => 0 lines
    ssize_t r;
    while ((r = read(fd, chunk, sizeof(chunk))) > 0) {
        for (ssize_t i = 0; i < r; i++) {
            if (chunk[i] == '\n') lines++;
        }
        trailing_nl = (chunk[r - 1] == '\n');
    }
    close(fd);
    if (!trailing_nl) lines++;
    return lines;
}

// Reads /proc/self/cmdline (NUL-separated argv) and joins with spaces.
// Returns true on success (even an empty cmdline is "success").
static bool read_cmdline(char* outbuf, size_t outsize) {
    int fd = open("/proc/self/cmdline", O_RDONLY);
    if (fd < 0) return false;
    ssize_t r = read(fd, outbuf, outsize - 1);
    close(fd);
    if (r < 0) return false;
    outbuf[r] = '\0';
    for (ssize_t i = 0; i < r; i++) {
        if (outbuf[i] == '\0') outbuf[i] = ' ';
    }
    return true;
}

// Signal name lookup — strsignal() is NOT async-signal-safe on all
// libc implementations, so use a hardcoded table.
static const char* signal_name(int sig) {
    switch (sig) {
        case SIGSEGV: return "SIGSEGV";
        case SIGABRT: return "SIGABRT";
        case SIGBUS:  return "SIGBUS";
        case SIGFPE:  return "SIGFPE";
        case SIGILL:  return "SIGILL";
        case SIGSYS:  return "SIGSYS";
        case SIGTRAP: return "SIGTRAP";
        default:      return "UNKNOWN";
    }
}

// [fix/sweep1014-crash-cpp] Signal-safe UTC time formatting. gmtime_r and
// localtime_r both take the tz locks (and localtime_r may additionally read
// files/env), so neither may run in a signal handler — a crash while another
// thread held the tz/malloc lock would deadlock the handler itself, and the
// very crashes this handler exists to capture would be lost. Compute the
// civil date from time_t with pure arithmetic instead (proleptic Gregorian,
// Howard Hinnant's civil_from_days algorithm; no locks, no env, no I/O).
static long long epoch_seconds_now() {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);  // async-signal-safe per POSIX
    return (long long)ts.tv_sec;
}

// days since 1970-01-01 -> y/m/d, pure integer arithmetic.
static void civil_from_days(long z, int* y, int* m, int* d) {
    z += 719468;
    long era = (z >= 0 ? z : z - 146096) / 146097;
    unsigned doe = (unsigned long)(z - era * 146097);               // [0, 146096]
    unsigned yoe = (doe - doe/1460 + doe/36524 - doe/146096) / 365; // [0, 399]
    long yy = (long)yoe + era * 400;
    unsigned doy = doe - (365*yoe + yoe/4 - yoe/100);               // [0, 365]
    unsigned mp = (5*doy + 2)/153;                                  // [0, 11]
    unsigned dd = doy - (153*mp+2)/5 + 1;                           // [1, 31]
    unsigned mm = mp < 10 ? mp+3 : mp-9;                            // [1, 12]
    *y = (int)(yy + (mm <= 2 ? 1 : 0));
    *m = (int)mm;
    *d = (int)dd;
}

// [fix/sweep1014-crash-cpp] Restore the saved previous handler (typically
// debuggerd's) and re-raise, so the tombstone chain stays intact. Falls
// back to SIG_DFL if the previous handler was never saved. Used for every
// re-raise path (early returns included — a tombstone helps there too).
static void restore_and_reraise(int sig) {
    struct sigaction sa{};
    if (sig >= 0 && sig < MINIS_MAX_SIGNAL && g_old_saved[sig]) {
        sa = g_old_sa[sig];
    } else {
        sa.sa_handler = SIG_DFL;
        sigemptyset(&sa.sa_mask);
    }
    sigaction(sig, &sa, nullptr);
    raise(sig);
}

// [fix/sweep1014-crash-cpp] Install `sa` for `sig`, capturing the previous
// handler exactly once (first install wins, so a repeated nativeInstall
// never loses the debuggerd handler it captured the first time).
static void install_and_save(int sig, const struct sigaction& sa) {
    struct sigaction captured{};
    if (!g_old_saved[sig] && sigaction(sig, &sa, &captured) == 0) {
        g_old_sa[sig] = captured;
        g_old_saved[sig] = 1;
    } else {
        sigaction(sig, &sa, nullptr);
    }
}

static void crash_signal_handler(int sig, siginfo_t* info, void* ctx) {
    // Reentrancy: if we're already in the handler, just restore default
    // and re-raise. Avoids infinite loop when the handler itself faults.
    if (g_in_handler) {
        restore_and_reraise(sig);
        return;
    }
    g_in_handler = 1;

    if (g_log_dir[0] == 0) {
        restore_and_reraise(sig);
        return;
    }

    // Build the per-crash filename. We timestamp in UTC and mark it "Z"
    // so the filename always matches wall-clock / file mtime regardless
    // of device timezone. [fix/sweep1014-crash-cpp] the body prints UTC
    // only: gmtime_r/localtime_r take the tz locks and are not
    // signal-safe, so the civil date is computed by pure arithmetic.
    long long now = epoch_seconds_now();
    long secs_of_day = (long)(now % 86400);
    int y, mo, d;
    civil_from_days((long)(now / 86400), &y, &mo, &d);
    int hh = (int)(secs_of_day / 3600);
    int mi = (int)((secs_of_day % 3600) / 60);
    int ss = (int)(secs_of_day % 60);

    char path[640];
    snprintf(path, sizeof(path),
        "%s/native-crash-%04d-%02d-%02dT%02d-%02d-%02dZ.log",
        g_log_dir, y, mo, d, hh, mi, ss);

    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        restore_and_reraise(sig);
        return;
    }

    // [fix/voice-crash-observability] Sentinel write BEFORE gathering any
    // context. If the process is SIGKILL'd mid-handler (lmkd / kernel OOM
    // killer — uncatchable, no tombstone, no dialog), the file is at least
    // non-empty: the sentinel proves "signal reached the handler, then the
    // process was externally killed before we could write the report". A
    // 0-byte file means the handler never even ran its write path — which
    // by itself is a decisive signal we could not capture before.
    static const char SENTINEL[] = "SIGNAL-REACHED\n";
    ssize_t sentinel_written = write(fd, SENTINEL, sizeof(SENTINEL) - 1);
    (void)sentinel_written; // best-effort; ignore partial write

    // Gather process identity + memory context (async-signal-safe reads).
    char cmdline[256] = {0};
    read_cmdline(cmdline, sizeof(cmdline));

    // One status read feeds every field below. RssAnon/RssFile/RssShmem are
    // what make the death state readable: on 2026-09-13 the process died at
    // VmRSS 6.0GB, but a same-day probe run showed the split matters — file
    // pages sit at a constant ~150MB regardless of pressure, so VmRSS alone
    // hides how much of the report is actually private anon memory (the part
    // the memory gate now thresholds on, and the part the kernel cannot
    // reclaim). static buffers: never grow the crashing thread's stack.
    static char status_text[4096];
    read_proc_text("/proc/self/status", status_text, sizeof(status_text));

    char vm_rss[32] = {0};
    char vm_peak[32] = {0};
    char vm_size[32] = {0};
    char threads[32] = {0};
    char rss_anon[32] = {0};
    char rss_file[32] = {0};
    char rss_shmem[32] = {0};
    minis_crash::extractField(status_text, "VmRSS", vm_rss, sizeof(vm_rss));
    minis_crash::extractField(status_text, "VmPeak", vm_peak, sizeof(vm_peak));
    minis_crash::extractField(status_text, "VmSize", vm_size, sizeof(vm_size));
    minis_crash::extractField(status_text, "Threads", threads, sizeof(threads));
    minis_crash::extractField(status_text, "RssAnon", rss_anon, sizeof(rss_anon));
    minis_crash::extractField(status_text, "RssFile", rss_file, sizeof(rss_file));
    minis_crash::extractField(status_text, "RssShmem", rss_shmem, sizeof(rss_shmem));

    // System-wide headroom at the moment of death: was the device tight too,
    // or did the process die while the phone still had GBs free?
    static char meminfo_text[2048];
    char mem_available[32] = {0};
    if (read_proc_text("/proc/meminfo", meminfo_text, sizeof(meminfo_text)) > 0) {
        minis_crash::extractField(meminfo_text, "MemAvailable", mem_available,
                                  sizeof(mem_available));
    }

    // Mapping count: VMAs are invisible in RSS, yet the same crash had
    // VmPeak 16.8GB against VmRSS 6.0GB.
    long map_lines = count_proc_lines("/proc/self/maps");

    char buf[2048];
    int off = 0;
    off += snprintf(buf + off, sizeof(buf) - (size_t)off,
        "=== Minis Native Crash ===\n"
        "Signal: %d (%s)\n"
        "si_code: %d\n"
        "Fault addr: %p\n"
        "PID: %d  TID: %d\n"
        "UTC: %04d-%02d-%02dT%02d:%02d:%02dZ\n",
        sig, signal_name(sig),
        info ? info->si_code : -1,
        info ? info->si_addr : nullptr,
        getpid(), (int)syscall(SYS_gettid),
        y, mo, d, hh, mi, ss);

    // [fix/sweep1014-crash-cpp] Local-time line removed: localtime_r is not
    // async-signal-safe (tz locks + may read files/env). UTC above is the
    // canonical timestamp; local timezone display is intentionally dropped.

    if (cmdline[0] != '\0') {
        off += snprintf(buf + off, sizeof(buf) - (size_t)off,
            "Process: %s\n", cmdline);
    }
    if (vm_rss[0] != '\0') {
        off += snprintf(buf + off, sizeof(buf) - (size_t)off,
            "VmRSS: %s kB\n", vm_rss);
    }
    if (vm_peak[0] != '\0') {
        off += snprintf(buf + off, sizeof(buf) - (size_t)off,
            "VmPeak: %s kB\n", vm_peak);
    }
    if (threads[0] != '\0') {
        off += snprintf(buf + off, sizeof(buf) - (size_t)off,
            "Threads: %s\n", threads);
    }
    // Private-anon split + address-space pressure. RssAnon is the metric the
    // memory gate now thresholds on (soft 450MB / hard 1200MB anon); the 1s
    // probe curve that led to this death lives in
    // files/logs/memspike-<date>.log (columns anon/resid/native/java/peak).
    if (rss_anon[0] != '\0') {
        off += snprintf(buf + off, sizeof(buf) - (size_t)off,
            "RssAnon: %s kB\n", rss_anon);
    }
    if (rss_file[0] != '\0') {
        off += snprintf(buf + off, sizeof(buf) - (size_t)off,
            "RssFile: %s kB\n", rss_file);
    }
    if (rss_shmem[0] != '\0') {
        off += snprintf(buf + off, sizeof(buf) - (size_t)off,
            "RssShmem: %s kB\n", rss_shmem);
    }
    if (vm_size[0] != '\0') {
        off += snprintf(buf + off, sizeof(buf) - (size_t)off,
            "VmSize: %s kB\n", vm_size);
    }
    if (mem_available[0] != '\0') {
        off += snprintf(buf + off, sizeof(buf) - (size_t)off,
            "MemAvailable: %s kB\n", mem_available);
    }
    if (map_lines >= 0) {
        off += snprintf(buf + off, sizeof(buf) - (size_t)off,
            "Maps: %ld entries\n", map_lines);
    }
    off += snprintf(buf + off, sizeof(buf) - (size_t)off,
        "\n"
        "(Full backtrace + abort message: run `logcat -b crash -d` and "
        "find this PID/TID.)\n");

    if (off > 0) {
        ssize_t written = 0;
        while (written < off) {
            ssize_t w = write(fd, buf + written, (size_t)off - (size_t)written);
            if (w <= 0) break;
            written += w;
        }
    }
    close(fd);

    // Re-raise after restoring the saved previous handler (typically
    // debuggerd's) so Android still produces a tombstone and
    // ActivityManager handles process-death the normal way.
    // [fix/sweep1014-crash-cpp] previously SIG_DFL was installed directly,
    // which silently defeated debuggerd: no tombstone, no `logcat -b crash`.
    restore_and_reraise(sig);
}

extern "C" JNIEXPORT void JNICALL
Java_com_rikkaminis_app_crash_NativeCrashHandler_nativeInstall(
        JNIEnv* env, jobject /*thiz*/, jstring jLogDir) {
    if (jLogDir == nullptr) return;
    const char* dir = env->GetStringUTFChars(jLogDir, nullptr);
    if (dir == nullptr) return;
    strncpy(g_log_dir, dir, sizeof(g_log_dir) - 1);
    g_log_dir[sizeof(g_log_dir) - 1] = 0;
    env->ReleaseStringUTFChars(jLogDir, dir);

    // mkdir is fine here — we're on the JVM thread, not in a signal.
    mkdir(g_log_dir, 0755);

    struct sigaction sa{};
    sa.sa_sigaction = crash_signal_handler;
    sa.sa_flags = SA_SIGINFO;
    sigemptyset(&sa.sa_mask);

    // Register for the signals that map to JNI/native bugs we actually
    // want to capture. SIGABRT covers __android_log_assert / abort()
    // from libc; SIGSEGV/BUS/ILL cover most JNI memory bugs; SIGFPE
    // covers integer div-by-zero. SIGSYS catches seccomp violations
    // (proot occasionally trips these on new kernels).
    // [fix/sweep1014-crash-cpp] the third argument now saves the previous
    // handler per signal (debuggerd's on Android) so the handler can
    // restore it before re-raising — without this, the "Android still
    // produces a tombstone" intent is defeated by our own SIG_DFL
    // overwrite. First save wins: a second install never clobbers the
    // original handler we captured.
    install_and_save(SIGSEGV, sa);
    install_and_save(SIGABRT, sa);
    install_and_save(SIGBUS,  sa);
    install_and_save(SIGFPE,  sa);
    install_and_save(SIGILL,  sa);
    install_and_save(SIGSYS,  sa);

    __android_log_print(ANDROID_LOG_INFO, LOG_TAG,
        "installed: dir=%s", g_log_dir);
}
