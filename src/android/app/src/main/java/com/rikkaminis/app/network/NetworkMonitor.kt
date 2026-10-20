package com.rikkaminis.app.network

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.util.Log
import com.rikkaminis.app.sandbox.RootfsManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient

/**
 * Monitors network connectivity changes using ConnectivityManager.NetworkCallback.
 * Evicts the OkHttp connection pool on network transitions to prevent stale connections.
 */
class NetworkMonitor {

    enum class NetworkStatus {
        CONNECTED,
        DISCONNECTED
    }

    companion object {
        private const val TAG = "NetworkMonitor"

        /**
         * [T-android-stale-conn-retry-hang] App-wide ConnectionPool shared by
         * every long-lived LLM provider OkHttpClient (OpenAI / Anthropic /
         * Gemini) — OkHttp explicitly supports sharing one pool across
         * clients. Routing them all through this instance is what lets
         * [evictConnectionPool] actually reach provider connections:
         * previously eviction only covered the single client registered via
         * [start], and MinisApp registers none, so eviction was a no-op.
         * Through a local VPN/proxy (e.g. clash at 127.0.0.1:7890) the TCP
         * socket to localhost survives network flaps, so the pool kept
         * handing the dead h2 tunnel to every retry — requests wrote into it
         * and hung forever waiting for response headers.
         */
        val sharedLLMConnectionPool = okhttp3.ConnectionPool(
            5, 5, java.util.concurrent.TimeUnit.MINUTES,
        )

        /**
         * [absorb-network-pack: P0-2-offline-retry-hold] The last started
         * monitor, for context-free connectivity probes (AgentLoopEngine's
         * offline retry hold — AgentLoopHost cannot grow a probe member
         * without touching every implementor). Null when monitoring was
         * never started — probes fail-open (treated as online) so a missing
         * monitor can never wedge a retry loop.
         */
        @Volatile
        var activeMonitor: NetworkMonitor? = null
    }

    private val _status = MutableStateFlow(NetworkStatus.DISCONNECTED)
    val status: StateFlow<NetworkStatus> = _status.asStateFlow()

    private var connectivityManager: ConnectivityManager? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private var okHttpClient: OkHttpClient? = null
    private var appContext: Context? = null

    /**
     * Background scope for DNS-refresh side effects. Kept off the callback
     * thread so ConnectivityManager isn't held up by file I/O.
     */
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /**
     * [dv15-A4] Pure transition table behind the three callbacks — the verdict
     * ("is anything usable?") and the pool decisions are computed in one place
     * instead of three per-callback special cases. See the class doc at the
     * bottom of this file for the transition table and why each rule exists.
     */
    private val networkState = NetworkStateMachine<Network>()

    /**
     * Registers a network callback to observe connectivity changes.
     * Optionally accepts a shared OkHttpClient whose connection pool will be
     * evicted on network transitions.
     *
     * @param context Application or activity context.
     * @param client Optional shared OkHttpClient for connection pool eviction.
     */
    fun start(context: Context, client: OkHttpClient? = null) {
        okHttpClient = client
        appContext = context.applicationContext
        connectivityManager = context.getSystemService(Context.CONNECTIVITY_SERVICE)
                as? ConnectivityManager

        val cm = connectivityManager ?: run {
            Log.e(TAG, "ConnectivityManager not available")
            return
        }

        // Set initial state
        val activeNetwork = cm.activeNetwork
        val capabilities = activeNetwork?.let { cm.getNetworkCapabilities(it) }
        val initiallyOnline =
            capabilities?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) == true
        _status.value = if (initiallyOnline) NetworkStatus.CONNECTED else NetworkStatus.DISCONNECTED
        // [dv15-A4] Seed the machine with the state the pools are built in, so
        // the first callback for this same network is a no-op instead of a
        // bogus "the interface moved" eviction.
        networkState.seed(activeNetwork.takeIf { initiallyOnline }, initiallyOnline)
        Log.d(TAG, "Initial network status: ${_status.value}")

        // Mirror iOS NetworkMonitor.swift:23-26 — do an immediate DNS write so
        // resolv.conf is populated before the first NetworkCallback fires. The
        // callback is async and can lag by hundreds of ms on cold start.
        refreshSandboxDns("initial")

        val request = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()

        activeMonitor = this

        val callback = object : ConnectivityManager.NetworkCallback() {

            override fun onAvailable(network: Network) {
                applyDecision(
                    networkState.onAvailable(network, currentDefaultNetwork()),
                    "onAvailable",
                )
                // Always refresh sandbox DNS on availability — an interface
                // swap (Wi-Fi → cellular) can fire onAvailable without a
                // prior onLost, and the new interface carries new DNS servers.
                refreshSandboxDns("onAvailable")
            }

            override fun onLost(network: Network) {
                applyDecision(
                    networkState.onLost(network, currentDefaultNetwork()),
                    "onLost",
                )
                // Rewrite resolv.conf even when disconnected so it falls back
                // to 8.8.8.8 / 8.8.4.4 instead of sitting stale with a DNS
                // server that's no longer reachable.
                refreshSandboxDns("onLost")
            }

            override fun onCapabilitiesChanged(
                network: Network,
                networkCapabilities: NetworkCapabilities
            ) {
                val hasInternet = networkCapabilities.hasCapability(
                    NetworkCapabilities.NET_CAPABILITY_INTERNET
                )
                val decision = networkState.onCapabilitiesChanged(
                    network,
                    hasInternet,
                    currentDefaultNetwork(),
                )
                applyDecision(decision, "onCapabilitiesChanged")
                if (decision.changed) refreshSandboxDns("onCapabilitiesChanged")
            }
        }

        networkCallback = callback
        cm.registerNetworkCallback(request, callback)
        Log.d(TAG, "Network monitoring started")
    }

    /**
     * Unregisters the network callback. Should be called during cleanup.
     */
    fun stop() {
        networkCallback?.let { callback ->
            try {
                connectivityManager?.unregisterNetworkCallback(callback)
                Log.d(TAG, "Network monitoring stopped")
            } catch (e: IllegalArgumentException) {
                Log.w(TAG, "Callback was not registered: ${e.message}")
            }
        }
        networkCallback = null
        connectivityManager = null
        okHttpClient = null
        appContext = null
        // [absorb-network-pack] Context-free probes must fail-open once the
        // monitor is gone (see the companion doc).
        activeMonitor = null
    }

    /** The interface traffic currently rides on — the pools' binding target. */
    private fun currentDefaultNetwork(): Network? = connectivityManager?.activeNetwork

    /**
     * [dv15-A4] Apply one [NetworkDecision]: publish the verdict, evict the
     * pools when their sockets can no longer be used, re-warm after a swap or
     * a recovery. [reason] names the callback and stays in the log line so a
     * flap can still be traced back to the event that caused it.
     */
    private fun applyDecision(decision: NetworkDecision, reason: String) {
        val newStatus = if (decision.online) NetworkStatus.CONNECTED else NetworkStatus.DISCONNECTED
        if (decision.changed) {
            Log.d(TAG, "Network transition: ${_status.value} -> $newStatus ($reason)")
        }
        if (decision.evictPool) {
            evictConnectionPool()
        }
        _status.value = newStatus
        if (decision.warmPool) {
            // [absorb-network-pack: OPT7-warm-reconnect] The pool was just
            // evicted — clear stale warmup debounce stamps and re-warm the
            // most recent origins so the recovery path re-arms the pool before
            // the user's next send. Without this, the first request after a
            // swap pays the full cold-connect cost (DNS+TCP+TLS, 1-3s via a
            // proxy).
            ConnectionWarmer.onNetworkChanged()
        }
    }

    /**
     * Evicts all idle connections from the OkHttp connection pools
     * to prevent stale connection reuse after a network change.
     * Always evicts [sharedLLMConnectionPool] (all LLM provider clients),
     * plus the optional client registered via [start].
     */
    private fun evictConnectionPool() {
        sharedLLMConnectionPool.evictAll()
        okHttpClient?.connectionPool?.evictAll()
        Log.d(TAG, "OkHttp connection pools evicted (shared LLM pool + registered client)")
    }

    /**
     * Refresh the sandbox rootfs' /etc/resolv.conf from the current system
     * DNS configuration. Runs on IO so we don't block the ConnectivityManager
     * callback thread with file I/O. Safe to call before the rootfs has been
     * extracted — [RootfsManager.refreshDns] no-ops when the rootfs is missing.
     *
     * Mirrors iOS NetworkMonitor.swift:26,60 which calls refreshDns() on every
     * NWPath update so already-running shells pick up the new nameservers the
     * next time they resolve a hostname (musl's getaddrinfo re-reads
     * resolv.conf on each lookup — no cache to invalidate).
     */
    private fun refreshSandboxDns(reason: String) {
        val ctx = appContext ?: return
        scope.launch {
            try {
                RootfsManager.getInstance(ctx).refreshDns()
                Log.d(TAG, "[DNS] sandbox resolv.conf refreshed ($reason)")
            } catch (t: Throwable) {
                Log.w(TAG, "[DNS] refresh failed ($reason): ${t.message}")
            }
        }
    }
}

/**
 * Outcome of one connectivity event as decided by [NetworkStateMachine].
 *
 * @param online the app has at least one usable network right now.
 * @param changed this event flipped the verdict (`online` differs from before).
 * @param evictPool the shared connection pools hold sockets that may be bound
 *   to an interface which is no longer the traffic-carrying one.
 * @param warmPool re-arm the warmup debounce and re-warm the recent origins —
 *   only set together with [evictPool] or a recovery into CONNECTED.
 */
internal data class NetworkDecision(
    val online: Boolean,
    val changed: Boolean,
    val evictPool: Boolean,
    val warmPool: Boolean,
)

/**
 * Pure connectivity state machine behind [NetworkMonitor]'s callbacks
 * ([dv15-A4] deep-audit-1036 A-06).
 *
 * The callbacks used to decide on their own, and the two decisions were
 * asymmetric, each with its own failure mode:
 *
 *  - `onAvailable` evicted the pools + re-warmed ONLY when the previous verdict
 *    was DISCONNECTED. In a Make-Before-Break swap (the new interface comes up
 *    while the old one is still up, then the old one dies) the verdict is
 *    already CONNECTED when the new network arrives, so the whole block was
 *    skipped: the pools kept sockets bound to an interface that was gone (via
 *    a local proxy the stale h2 tunnel survives and every request hangs).
 *  - `onLost` wrote DISCONNECTED unconditionally, so losing ANY interface read
 *    as going offline even while another one was still carrying traffic — the
 *    offline retry hold then waited out its full window on a live device.
 *
 * This machine tracks the set of live networks, so both symptoms have a single
 * explanation:
 *
 *  - DISCONNECTED <=> the live set is empty (multi-network safe);
 *  - the pools are evicted exactly when the sockets they hold can no longer be
 *    used: the default (traffic-carrying) interface moved, the last live
 *    network went away, or the interface the pools were built on left the live
 *    set. A redundant callback for a network already counted, or a flap on a
 *    SECONDARY interface, changes no verdict and evicts nothing — otherwise a
 *    dual-network wobble would thrash the pools on every callback.
 *
 * Deliberately dependency-free (no ConnectivityManager, no android.* types):
 * the caller supplies the keys (`android.net.Network`) plus the current
 * default, which keeps the transition table unit-testable on the JVM.
 */
internal class NetworkStateMachine<K : Any> {

    private val liveNetworks = LinkedHashSet<K>()

    /** Interface the pools' sockets were last known to be built on. */
    private var poolDefault: K? = null

    /** How many networks are currently considered usable. */
    @Synchronized
    fun liveCount(): Int = liveNetworks.size

    /**
     * Seed from the startup probe ([NetworkMonitor.start] reads
     * `activeNetwork` + its capabilities before registering the callback).
     * Emits no transition and evicts nothing — this is the state the pools were
     * built in.
     */
    @Synchronized
    fun seed(defaultKey: K?, online: Boolean): NetworkDecision {
        liveNetworks.clear()
        poolDefault = if (online) defaultKey else null
        if (online && defaultKey != null) liveNetworks.add(defaultKey)
        return NetworkDecision(online = online, changed = false, evictPool = false, warmPool = false)
    }

    /** `NetworkCallback.onAvailable` — [key] joined the usable set. */
    @Synchronized
    fun onAvailable(key: K, defaultKey: K?): NetworkDecision = apply(key, add = true, defaultKey)

    /** `NetworkCallback.onLost` — [key] left the usable set. */
    @Synchronized
    fun onLost(key: K, defaultKey: K?): NetworkDecision = apply(key, add = false, defaultKey)

    /**
     * `NetworkCallback.onCapabilitiesChanged` — the network either gained or
     * lost `NET_CAPABILITY_INTERNET`. Losing it (captive portal / failed
     * validation) takes the network out of the usable set exactly like a loss,
     * without the interface disappearing.
     */
    @Synchronized
    fun onCapabilitiesChanged(key: K, hasInternet: Boolean, defaultKey: K?): NetworkDecision =
        apply(key, add = hasInternet, defaultKey)

    private fun apply(key: K, add: Boolean, defaultKey: K?): NetworkDecision {
        val wasOnline = liveNetworks.isNotEmpty()
        if (add) liveNetworks.add(key) else liveNetworks.remove(key)
        val online = liveNetworks.isNotEmpty()
        val changed = online != wasOnline
        val goingOffline = wasOnline && !online
        // Read the OLD pool binding: if the interface the pools were built on
        // is the one leaving the usable set, its sockets are dead even when
        // another network keeps the verdict online.
        val lostPoolInterface = !add && poolDefault == key
        val defaultMoved = defaultKey != poolDefault
        poolDefault = defaultKey
        val evict = defaultMoved || goingOffline || lostPoolInterface
        return NetworkDecision(
            online = online,
            changed = changed,
            evictPool = evict,
            warmPool = online && (evict || changed),
        )
    }
}
