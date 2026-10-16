package com.rikkaminis.app.browser

/**
 * Single-slot registry for in-flight async JS bridge requests
 * (`executeJS` / `awaitPromiseJs` wait on exactly one deferred per
 * [BrowserUseManager] at a time — the WebView drives one page, so one slot
 * matches the runtime shape).
 *
 * [deep-audit-1016c ISSUE-03B] Replaces the two bare fields
 * (`asyncJsDeferred` / `asyncJsActiveToken`) it used to be spread across:
 *
 *  - **Visibility** — the two fields had no `@Volatile`; writers ran on the
 *    calling coroutine's thread while readers ([BrowserUseManager.jsBridge]
 *    resolve/reject) run on the WebView's JavaBridge thread, so a bridge
 *    callback could legally observe a stale token/deferred and drop a resolve.
 *    The single `@Volatile` entry below makes token+value swap atomically and
 *    gives every reader the write it needs to see.
 *
 *  - **Unconditional finish** — the old `finishAsyncJsRequest()` took no
 *    token and cleared the slot unconditionally, so a timed-out / cancelled /
 *    failed request could clear a slot a NEWER request already owns (the
 *    newer request's resolve then lands on a null slot and its waiter burns
 *    the full 30s/60s timeout). [finish] only clears when [token] owns it.
 *
 *  - **Silent eviction** — the old `beginAsyncJsRequest()` overwrote any
 *    in-flight slot, orphaning the previous deferred; [begin] now returns the
 *    evicted value so the caller can complete it with a structured error
 *    instead of leaving its waiter to time out.
 *
 * Pure JVM (no Android / no coroutines types) so the state machine is unit
 * testable directly.
 */
internal class AsyncJsSlot<T : Any> {

    private class Entry<V>(val token: String, val value: V)

    /**
     * One volatile reference holds both the owning token and its value, so a
     * reader never observes a (token, value) pair from two different
     * requests.
     */
    @Volatile
    private var entry: Entry<T>? = null

    /** The value iff [token] currently owns the slot, else null. */
    fun ifOwner(token: String): T? = entry?.takeIf { it.token == token }?.value

    /**
     * Register [value] under a fresh [token] and return the value that was
     * evicted (caller completes it with a structured error), or null when the
     * slot was free.
     */
    fun begin(token: String, value: T): T? {
        val previous = entry
        entry = Entry(token, value)
        return previous?.value
    }

    /**
     * Clear the slot only when [token] owns it. Returns true when cleared;
     * false means the slot was free or owned by a different (newer) request —
     * the caller's own slot is already gone in that case and must stay that
     * way.
     */
    fun finish(token: String): Boolean {
        val current = entry ?: return false
        if (current.token != token) return false
        entry = null
        return true
    }
}
