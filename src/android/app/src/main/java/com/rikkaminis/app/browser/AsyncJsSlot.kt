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

    /**
     * [deep-audit-1019 ISSUE-08] Take (and clear) whatever the slot holds,
     * whoever owns it. The teardown paths — `BrowserUseManager.destroy()` and
     * its `onRendererGone` — use this to complete the in-flight waiter with a
     * structured error instead of leaving it to burn the full 30s/60s timeout
     * on bridge callbacks that can never arrive again (the JS interface dies
     * with the renderer, and a destroyed WebView never re-registers one).
     *
     * Deliberately NOT token-guarded, unlike [finish]: teardown does not know
     * (or care) which request is in flight, and after teardown no request may
     * wait on this WebView at all. Returns null when the slot is free, so a
     * teardown racing a request that already finished is a no-op — and a
     * request that [begin]s *after* the take keeps its own slot (take never
     * stores a null entry).
     *
     * Read-then-clear has the same best-effort concurrency shape as
     * [begin]/[finish]: the single volatile reference keeps each token/value
     * pair intact, it does not make the swap atomic. A [begin] concurrent with
     * a take therefore lands either before it (its deferred is handed to the
     * teardown and fails — correct, that request has no completion path) or
     * after it (its deferred survives, and the bridge that would resolve it is
     * already gone — it times out, which is the pre-fix behaviour and no
     * worse).
     */
    fun takeCurrent(): T? {
        val current = entry ?: return null
        entry = null
        return current.value
    }
}
