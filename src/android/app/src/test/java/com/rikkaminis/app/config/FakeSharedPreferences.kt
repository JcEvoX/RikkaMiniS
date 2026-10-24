package com.rikkaminis.app.config

import android.content.SharedPreferences

/**
 * Minimal in-memory [SharedPreferences] for JVM tests ([dv29-A4]).
 *
 * Deliberately dumb: `apply()`/`commit()` fold into the map immediately and the
 * listener set is only book-kept (nothing here fires callbacks). Tests that need
 * "the listener does not reach me" model it explicitly with two instances rather
 * than relying on this fake to be quiet.
 */
internal class FakeSharedPreferences(initial: Map<String, Any?> = emptyMap()) : SharedPreferences {

    val map = HashMap<String, Any?>(initial)

    /** Copy this handle's keys into a second, independent handle (same file, another instance). */
    fun cloneHandle(): FakeSharedPreferences = FakeSharedPreferences(map)

    private val listeners = HashSet<SharedPreferences.OnSharedPreferenceChangeListener>()

    override fun getAll(): Map<String, *> = HashMap(map)
    override fun getString(key: String, defValue: String?): String? = map[key] as? String ?: defValue
    override fun getInt(key: String, defValue: Int): Int = map[key] as? Int ?: defValue
    override fun getLong(key: String, defValue: Long): Long = map[key] as? Long ?: defValue
    override fun getFloat(key: String, defValue: Float): Float = map[key] as? Float ?: defValue
    override fun getBoolean(key: String, defValue: Boolean): Boolean =
        map[key] as? Boolean ?: defValue

    override fun contains(key: String): Boolean = key in map
    override fun edit(): SharedPreferences.Editor = FakeEditor(this)
    override fun getStringSet(key: String, defValues: Set<String>?): Set<String>? = null

    override fun registerOnSharedPreferenceChangeListener(
        listener: SharedPreferences.OnSharedPreferenceChangeListener,
    ) {
        listeners.add(listener)
    }

    override fun unregisterOnSharedPreferenceChangeListener(
        listener: SharedPreferences.OnSharedPreferenceChangeListener,
    ) {
        listeners.remove(listener)
    }

    private class FakeEditor(private val prefs: FakeSharedPreferences) : SharedPreferences.Editor {
        private val pending = HashMap<String, Any?>()
        private val removed = HashSet<String>()

        override fun putString(key: String, value: String?) = apply { set(key, value) }
        override fun putInt(key: String, value: Int) = apply { set(key, value) }
        override fun putLong(key: String, value: Long) = apply { set(key, value) }
        override fun putFloat(key: String, value: Float) = apply { set(key, value) }
        override fun putBoolean(key: String, value: Boolean) = apply { set(key, value) }

        override fun putStringSet(
            key: String,
            values: Set<String>?,
        ): SharedPreferences.Editor = throw UnsupportedOperationException("not used by these tests")

        override fun remove(key: String) = apply {
            removed.add(key)
            pending.remove(key)
        }

        override fun clear() = apply {
            pending.clear()
            removed.addAll(prefs.map.keys)
        }

        private fun set(key: String, value: Any?) {
            pending[key] = value
            removed.remove(key)
        }

        override fun commit(): Boolean {
            apply()
            return true
        }

        override fun apply() {
            for (k in removed) prefs.map.remove(k)
            for ((k, v) in pending) prefs.map[k] = v
            pending.clear()
            removed.clear()
        }
    }
}
