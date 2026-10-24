package com.rikkaminis.app.sandbox.offload

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.ContactsContract
import androidx.core.content.ContextCompat
import com.rikkaminis.app.logging.AppLogger
import com.rikkaminis.app.offload.OffloadPermissionManager
import com.rikkaminis.app.sandbox.NativeOffloadHandler
import com.rikkaminis.app.sandbox.NativeOffloadRequest
import com.rikkaminis.app.sandbox.NativeOffloadResult
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject

/**
 * Host-side implementation of `android-contacts` — reads + writes the device
 * contacts via the ContactsContract ContentProvider.
 *
 * Usage:
 *   android-contacts list [--max N]
 *   android-contacts search <query> [--max N]
 *   android-contacts get <id>
 *   android-contacts delete <id>
 *   android-contacts --help
 */
class ContactsOffloadHandler(private val context: Context) : NativeOffloadHandler {

    override fun handle(request: NativeOffloadRequest): NativeOffloadResult {
        val rawArgs = request.argv.drop(1)
        // T54 / [da1019-J]: parse once, then dispatch off the PARSED positional
        // list — never off the raw argv. OffloadArgs has already stripped the
        // shared output-shaping flags (--compact / -q / --quiet) and consumed
        // named values (`--max 10`), so an option placed before the subcommand
        // no longer takes the subcommand or the <id> slot.
        // [sweep1014-B34] fixed only the search query in this file; dispatch,
        // needsWrite and get/delete kept reading `rawArgs[0]`, so
        // `android-contacts --compact list` died with "unknown subcommand
        // '--compact'" and `android-contacts get --compact 42` read "--compact"
        // as the id (exit 2) — both are 100%-reproducible agent CLI combos.
        val cmd = parseContactsCommand(rawArgs)
        val parsedArgs = cmd.args

        // Help + empty-invocation handling moves to the parsed flags too, the
        // same shape as every other android-* handler
        // (`args.hasFlag("h", "help")`). It needs the help/h booleanFlags
        // declaration ([sweep1014-B26a]): without it `--help list` greedily
        // binds "list" as the *value* of --help and the flag disappears.
        // Behaviour delta vs. the old first-token check: `--help` in any
        // position now prints help (was: only as argv[1]).
        if (rawArgs.isEmpty() || parsedArgs.hasFlag("h", "help")) {
            return NativeOffloadResult(0, HELP_TEXT)
        }

        // T330: tri-state agent gate before any work or system permission ask.
        OffloadGate.enforce("contacts", "android-contacts", parsedArgs, request)?.let { return it }

        // delete needs WRITE_CONTACTS in addition to READ; the read-only path
        // is fine with READ_CONTACTS alone.
        val needsWrite = cmd.sub == "delete"
        ensurePermission(needsWrite = needsWrite, args = parsedArgs)?.let { return it }

        return try {
            when (val sub = cmd.sub) {
                "list" -> doList(parsedArgs)
                "search" -> doSearch(parsedArgs)
                "get" -> doGet(cmd.id, parsedArgs)
                "delete" -> doDelete(cmd.id, parsedArgs)
                // sub == null ⇒ the argv held only options / named values
                // (a bare `--compact`, `--max 10`): there is no subcommand to
                // name, so say that instead of quoting the literal "null".
                null -> NativeOffloadResult(
                    2,
                    "android-contacts: missing subcommand — expected one of list / search / get / delete\n$HELP_TEXT",
                )
                else -> NativeOffloadResult(2, "android-contacts: unknown subcommand '$sub'\n$HELP_TEXT")
            }
        } catch (e: SecurityException) {
            AppLogger.warning(TAG, "SecurityException from ContactsProvider2: ${e.message}")
            val body = org.json.JSONObject()
                .put("error", "contacts_blocked_by_oem")
                .put("message", "READ_CONTACTS is granted but the contacts provider refused the query. On MIUI/HyperOS this usually means the user needs to enable Contacts access in the per-app 'Other Permissions' / 'Privacy' panel; on ColorOS/OxygenOS check Phone Manager → Privacy Permissions. Underlying: ${e.message}")
                .toString()
            NativeOffloadResult(77, OffloadOutput.formatBody(body, parsedArgs) + "\n")
        } catch (e: Throwable) {
            AppLogger.warning(TAG, "uncaught: ${e.message}")
            val body = org.json.JSONObject()
                .put("error", "contacts_provider_error")
                .put("message", e.message ?: "unknown contacts provider error")
                .toString()
            NativeOffloadResult(1, OffloadOutput.formatBody(body, parsedArgs) + "\n")
        }
    }

    private fun hasPermission(needsWrite: Boolean = false): Boolean {
        val read = ContextCompat.checkSelfPermission(context, Manifest.permission.READ_CONTACTS) ==
            PackageManager.PERMISSION_GRANTED
        if (!needsWrite) return read
        val write = ContextCompat.checkSelfPermission(context, Manifest.permission.WRITE_CONTACTS) ==
            PackageManager.PERMISSION_GRANTED
        return read && write
    }

    /**
     * Drive the same system-dialog → settings-gate flow as location/calendar.
     * Returns null if permission ends up granted, otherwise a ready-to-return
     * error result describing why we gave up. When [needsWrite] is true the
     * permission set additionally includes WRITE_CONTACTS — required by the
     * `delete` subcommand.
     */
    private fun ensurePermission(needsWrite: Boolean = false, args: OffloadArgs): NativeOffloadResult? {
        if (hasPermission(needsWrite)) return null
        val perms = if (needsWrite) {
            listOf(Manifest.permission.READ_CONTACTS, Manifest.permission.WRITE_CONTACTS)
        } else {
            listOf(Manifest.permission.READ_CONTACTS)
        }
        AppLogger.warning(TAG, "${perms.joinToString("+")} not granted — routing through permission flow")
        val result = runBlocking {
            var r = OffloadPermissionManager.requestAndroidPermission(perms)
            if (r == OffloadPermissionManager.AndroidPermissionResult.DENIED &&
                OffloadPermissionManager.pollForPermissionGrant({ hasPermission(needsWrite) })
            ) {
                AppLogger.info(TAG, "Contacts permission granted during post-DENY poll")
                r = OffloadPermissionManager.AndroidPermissionResult.GRANTED
            }
            if (r == OffloadPermissionManager.AndroidPermissionResult.DENIED) {
                r = OffloadPermissionManager.requestSettingsGate(
                    OffloadPermissionManager.SettingsGateRequest(
                        id = if (needsWrite) "CONTACTS_RW" else Manifest.permission.READ_CONTACTS,
                        title = "Contacts permission needed",
                        message = if (needsWrite) {
                            "RikkaMinis needs read + write contacts permission to delete entries. Open Settings to allow it."
                        } else {
                            "RikkaMinis needs contacts permission to read your address book. Open Settings to allow it."
                        },
                        settingsAction = android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        requiresPackageUri = true,
                        positiveLabel = "Open Settings",
                    ),
                    check = { hasPermission(needsWrite) },
                )
            }
            r
        }
        return when (result) {
            OffloadPermissionManager.AndroidPermissionResult.GRANTED -> null
            OffloadPermissionManager.AndroidPermissionResult.DENIED -> NativeOffloadResult(
                77,
                OffloadOutput.formatBody(
                    JSONObject().put("error", "permission_denied")
                        .put("message", "The user declined the contacts permission.")
                        .toString(),
                    args,
                ) + "\n",
            )
            OffloadPermissionManager.AndroidPermissionResult.TIMEOUT -> NativeOffloadResult(
                77,
                OffloadOutput.formatBody(
                    JSONObject().put("error", "timeout")
                        .put("message", "Timed out waiting for the user to grant the contacts permission.")
                        .toString(),
                    args,
                ) + "\n",
            )
        }
    }

    /**
     * [da1033-D ISSUE-04] Structured replacement for the three bare
     * "android-contacts: failed to query …" sentences. A null cursor out of
     * ContactsProvider is a JSON error object like every other failure branch
     * in this handler, so `jq` / `json.loads` keeps working when the provider
     * misbehaves. Exit code stays 1 at each call site (it was 1 before too).
     */
    private fun queryFailedBody(message: String): String =
        JSONObject().put("error", "query_failed").put("message", message).toString()

    private fun doList(parsedArgs: OffloadArgs): NativeOffloadResult {
        // [dv28-A6 A-06] A bad page size is a usage error, never a silent
        // default and never an empty page: `--max 0` used to hand 0 to
        // collectContactPage, whose `rows.size < max` loop answered `[]` at
        // exit 0 (and `--max abc` fell back to the default at exit 0).
        val max = when (val size = resolvePageSize(parsedArgs)) {
            is PageSize.Ok -> size.value
            is PageSize.Invalid -> return pageSizeRefusal("list", size.message)
        }
        val projection = arrayOf(
            ContactsContract.Contacts._ID,
            ContactsContract.Contacts.DISPLAY_NAME_PRIMARY,
            ContactsContract.Contacts.HAS_PHONE_NUMBER,
        )
        val sort = "${ContactsContract.Contacts.DISPLAY_NAME_PRIMARY} ASC"
        val cursor = context.contentResolver.query(
            ContactsContract.Contacts.CONTENT_URI, projection, null, null, sort,
        ) ?: return NativeOffloadResult(
            1,
            OffloadOutput.formatBody(queryFailedBody("Failed to query contacts."), parsedArgs) + "\n",
        )

        // [sweep1014-P10] Collect the page first, then batch-load phones +
        // emails for all ids in one query per data kind — the old per-row
        // readPhones/readEmails cost 2 extra Binder queries per contact
        // (50 contacts = 101 contentResolver round-trips).
        val arr = JSONArray()
        cursor.use {
            attachContactData(collectContactPage(it, max), arr)
        }
        return NativeOffloadResult(0, OffloadOutput.formatBody(arr.toString(2), parsedArgs) + "\n")
    }

    private fun doSearch(parsedArgs: OffloadArgs): NativeOffloadResult {
        // [dv14-A1 ISSUE-01] The `-n` short form is bound to `--max` by
        // [bindLegacyShortMax] before the parse, so `getInt("max", "n")` — the
        // old spelling here — could never have read it: OffloadArgs puts a
        // single-dash token in the flag set and NEVER in `values`, so the "n"
        // alias was dead and the value landed in the positional list, where
        // `search -n 10 Alice` searched for the name "10" and dropped "Alice".
        // [resolvePageSize] is the ONE resolver (same as doList); 20 is
        // search's documented default (HELP_TEXT: "default 20" — list's is 50).
        //
        // [dv28-A6 A-06] Resolved BEFORE the query on purpose: a page size the
        // caller spelled but the handler cannot honour is a usage error even in
        // the invocation whose query slot is ambiguous (`search -n abc Alice`
        // put "abc" there and dropped "Alice").
        val max = when (val size = resolvePageSize(parsedArgs, 20)) {
            is PageSize.Ok -> size.value
            is PageSize.Invalid -> return pageSizeRefusal("search", size.message)
        }
        val query = searchQueryOf(parsedArgs.positional)
            ?: return NativeOffloadResult(2, "android-contacts search: missing <query>\n")
        val projection = arrayOf(
            ContactsContract.Contacts._ID,
            ContactsContract.Contacts.DISPLAY_NAME_PRIMARY,
            ContactsContract.Contacts.HAS_PHONE_NUMBER,
        )
        val selection = "${ContactsContract.Contacts.DISPLAY_NAME_PRIMARY} LIKE ?"
        val selectionArgs = arrayOf("%$query%")
        val sort = "${ContactsContract.Contacts.DISPLAY_NAME_PRIMARY} ASC"
        val cursor = context.contentResolver.query(
            ContactsContract.Contacts.CONTENT_URI, projection, selection, selectionArgs, sort,
        ) ?: return NativeOffloadResult(
            1,
            OffloadOutput.formatBody(queryFailedBody("Failed to query contacts."), parsedArgs) + "\n",
        )

        val arr = JSONArray()
        cursor.use {
            attachContactData(collectContactPage(it, max), arr)
        }
        // [da1033-D ISSUE-04] No-match is a normal result, not a message: this
        // branch used to emit the bare sentence "No contacts found matching
        // '<q>'." with exit 0, so `... | jq` broke on the most common empty
        // path. `arr.toString(2)` is `[]` here — the same JSONArray shape
        // doList returns for an empty address book.
        return NativeOffloadResult(0, OffloadOutput.formatBody(arr.toString(2), parsedArgs) + "\n")
    }

    private fun doGet(contactId: Long?, parsedArgs: OffloadArgs): NativeOffloadResult {
        val id = contactId
            ?: return NativeOffloadResult(2, "android-contacts get: missing or invalid <id>\n")

        val projection = arrayOf(
            ContactsContract.Contacts._ID,
            ContactsContract.Contacts.DISPLAY_NAME_PRIMARY,
            ContactsContract.Contacts.HAS_PHONE_NUMBER,
        )
        val cursor = context.contentResolver.query(
            ContactsContract.Contacts.CONTENT_URI,
            projection,
            "${ContactsContract.Contacts._ID} = ?",
            arrayOf(id.toString()),
            null,
        ) ?: return NativeOffloadResult(
            1,
            OffloadOutput.formatBody(queryFailedBody("Failed to query contact id=$id."), parsedArgs) + "\n",
        )

        cursor.use {
            if (!it.moveToFirst()) {
                // [da1033-D ISSUE-04] Same envelope as doDelete below
                // (error / id / message, exit 1). A missing id is an error
                // object, not the bare sentence "android-contacts: no contact
                // with id=$id".
                val body = JSONObject().put("error", "not_found")
                    .put("id", id)
                    .put("message", "No contact with id=$id.")
                    .toString()
                return NativeOffloadResult(1, OffloadOutput.formatBody(body, parsedArgs) + "\n")
            }
            val obj = readContactRow(it)
            return NativeOffloadResult(0, OffloadOutput.formatBody(obj.toString(2), parsedArgs) + "\n")
        }
    }

    /**
     * Delete a single contact by aggregated `Contacts._ID`. We first try the
     * aggregated `Contacts.CONTENT_URI` path which cascades raw rows on most
     * accounts; when that returns 0 we fall back to deleting every
     * `RawContacts` row whose `CONTACT_ID` matches — this catches stub rows
     * that the aggregated URI silently refuses (commonly orphaned raw rows
     * left by sync adapters or test inserts).
     */
    private fun doDelete(contactId: Long?, parsedArgs: OffloadArgs): NativeOffloadResult {
        val id = contactId
            ?: return NativeOffloadResult(2, "android-contacts delete: missing or invalid <id>\n")

        var deleted = context.contentResolver.delete(
            ContactsContract.Contacts.CONTENT_URI,
            "${ContactsContract.Contacts._ID} = ?",
            arrayOf(id.toString()),
        )
        if (deleted <= 0) {
            // Fallback: delete raw_contacts rows linked to this aggregated id.
            // We don't have the account credentials needed for the
            // CALLER_IS_SYNCADAPTER hard-delete dance, so this leaves a
            // tombstone that real sync adapters will eventually purge — fine
            // for our use case (the contact disappears from Contacts immediately).
            deleted = context.contentResolver.delete(
                ContactsContract.RawContacts.CONTENT_URI,
                "${ContactsContract.RawContacts.CONTACT_ID} = ?",
                arrayOf(id.toString()),
            )
        }
        if (deleted <= 0) {
            val body = JSONObject().put("error", "not_found")
                .put("id", id)
                .put("message", "No contact with id=$id (already deleted, or the OS hides it).")
                .toString()
            return NativeOffloadResult(1, OffloadOutput.formatBody(body, parsedArgs) + "\n")
        }
        val body = JSONObject().put("id", id).put("deleted", deleted).toString(2)
        return NativeOffloadResult(0, OffloadOutput.formatBody(body, parsedArgs) + "\n")
    }

    private fun readContactRow(c: android.database.Cursor): JSONObject {
        val id = c.getLong(0)
        val name = c.getString(1) ?: ""
        val hasPhone = c.getInt(2) > 0
        val obj = JSONObject()
        obj.put("id", id)
        obj.put("name", name)
        if (hasPhone) {
            val phones = readPhones(id)
            if (phones.length() > 0) obj.put("phones", phones)
        }
        val emails = readEmails(id)
        if (emails.length() > 0) obj.put("emails", emails)
        return obj
    }

    private fun readPhones(contactId: Long): JSONArray {
        val arr = JSONArray()
        context.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            arrayOf(ContactsContract.CommonDataKinds.Phone.NUMBER),
            "${ContactsContract.CommonDataKinds.Phone.CONTACT_ID} = ?",
            arrayOf(contactId.toString()),
            null,
        )?.use {
            while (it.moveToNext()) arr.put(it.getString(0) ?: "")
        }
        return arr
    }

    private fun readEmails(contactId: Long): JSONArray {
        val arr = JSONArray()
        context.contentResolver.query(
            ContactsContract.CommonDataKinds.Email.CONTENT_URI,
            arrayOf(ContactsContract.CommonDataKinds.Email.ADDRESS),
            "${ContactsContract.CommonDataKinds.Email.CONTACT_ID} = ?",
            arrayOf(contactId.toString()),
            null,
        )?.use {
            while (it.moveToNext()) arr.put(it.getString(0) ?: "")
        }
        return arr
    }

    /**
     * One row of the contacts cursor — id, display name and whether it may
     * have phone numbers. Pure data holder so page collection and the batch
     * aggregation stay testable without an Android device.
     */
    private data class ContactRow(val id: Long, val name: String, val hasPhone: Boolean)

    private fun collectContactPage(c: android.database.Cursor, max: Int): List<ContactRow> {
        val rows = mutableListOf<ContactRow>()
        while (c.moveToNext() && rows.size < max) {
            rows.add(ContactRow(c.getLong(0), c.getString(1) ?: "", c.getInt(2) > 0))
        }
        return rows
    }

    /**
     * [sweep1014-P10] Batch-load phones + emails for a whole page of contacts:
     * one IN (…) query per data kind per chunk instead of two Binder
     * round-trips per contact. Output shape is unchanged — each contact still
     * carries phones[]/emails[] exactly as the per-row implementation emitted
     * them (phones only when HAS_PHONE_NUMBER, values in provider order).
     */
    private fun attachContactData(page: List<ContactRow>, arr: JSONArray) {
        if (page.isEmpty()) return
        val phones = batchQueryValues(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            ContactsContract.CommonDataKinds.Phone.CONTACT_ID,
            ContactsContract.CommonDataKinds.Phone.NUMBER,
            page.map { it.id },
        )
        val emails = batchQueryValues(
            ContactsContract.CommonDataKinds.Email.CONTENT_URI,
            ContactsContract.CommonDataKinds.Email.CONTACT_ID,
            ContactsContract.CommonDataKinds.Email.ADDRESS,
            page.map { it.id },
        )
        for (row in page) {
            val obj = JSONObject()
            obj.put("id", row.id)
            obj.put("name", row.name)
            if (row.hasPhone) {
                val p = phones[row.id].orEmpty()
                if (p.isNotEmpty()) obj.put("phones", JSONArray(p))
            }
            val e = emails[row.id].orEmpty()
            if (e.isNotEmpty()) obj.put("emails", JSONArray(e))
            arr.put(obj)
        }
    }

    private fun batchQueryValues(
        uri: Uri,
        idColumn: String,
        valueColumn: String,
        ids: List<Long>,
    ): Map<Long, List<String>> {
        if (ids.isEmpty()) return emptyMap()
        val pairs = mutableListOf<Pair<Long, String>>()
        for (chunk in ids.chunked(BATCH_CHUNK)) {
            context.contentResolver.query(
                uri,
                arrayOf(idColumn, valueColumn),
                "$idColumn IN (${inPlaceholders(chunk.size)})",
                chunk.map { it.toString() }.toTypedArray(),
                "$idColumn ASC",
            )?.use { c ->
                while (c.moveToNext()) pairs.add(c.getLong(0) to (c.getString(1) ?: ""))
            }
        }
        return aggregateValues(pairs)
    }

    /**
     * [dv28-A6 A-06] Outcome of resolving one page-size spelling.
     *
     * A plain `Int` cannot express the difference the three defects
     * depended on: `--max abc` and "no `--max` at all" both arrive as
     * `null` out of `getInt`, and a non-positive value was handed straight
     * to [collectContactPage], whose `rows.size < max` loop then answered
     * an empty page with exit 0.
     */
    internal sealed class PageSize {
        /** A usable page size, always within 1..[MAX_PAGE_SIZE]. */
        data class Ok(val value: Int) : PageSize()

        /** The caller spelled a page size that cannot be honoured. */
        data class Invalid(val message: String) : PageSize()
    }

    companion object {
        private const val TAG = "ContactsOffload"
        // [sweep1014-P10] SQLite's default host-parameter ceiling is 999;
        // chunking keeps every IN (...) list well under it for any --max.
        private const val BATCH_CHUNK = 500
        /**
         * [sweep1014-B34] Search query resolution: positional[0] is the
         * subcommand itself, positional[1] is the query. Pure so the
         * --max-swallowed-query regression stays testable off-device.
         */
        private fun searchQueryOf(positional: List<String>): String? = positional.getOrNull(1)

        /** Default page size for `list` when no `--max` is given. */
        private const val DEFAULT_LIST_MAX = 50

        /**
         * [dv28-A6 A-06] Upper bound for `--max` / `-n`. A page size is a
         * provider + Binder cost, not a preference: every page is two batch
         * queries per data kind (see [BATCH_CHUNK]), and an agent that wants
         * more rows can loop. Out of range is a usage error (exit 2) rather
         * than a clamp — [OffloadArgs.getBool] documents the same
         * refuse-instead-of-guess posture, and a clamped page would report
         * success for a request the handler silently did not honour.
         */
        internal const val MAX_PAGE_SIZE = 500

        /**
         * [da1019-J] The parsed shape of one `android-contacts` invocation
         * (argv with the program name already dropped). Pure value — no
         * Context, no filesystem — so subcommand / id resolution stays
         * testable off-device.
         */
        internal data class ContactsCommand(
            /** positional[0] — the subcommand, or null when every token was an option. */
            val sub: String?,
            /** positional[1] parsed as a contact id — consumed by `get` / `delete`. */
            val id: Long?,
            val args: OffloadArgs,
        )

        /**
         * [da1019-J] Parse the raw argv into [ContactsCommand]. Dispatch,
         * `needsWrite` and the id lookup all key off
         * [OffloadArgs.positional] instead of the raw list, so output-shaping
         * flags (`--compact` / `-q` / `--quiet`) and named values
         * (`--max 10`) no longer shift the subcommand / id slots.
         *
         * `help` / `h` must be declared as boolean flags: otherwise
         * `--help list` greedily binds "list" as the *value* of --help and
         * both the flag and the subcommand vanish ([sweep1014-B26a]).
         */
        internal fun parseContactsCommand(argv: List<String>): ContactsCommand {
            val args = OffloadArgs(bindLegacyShortMax(argv), booleanFlags = setOf("help", "h"))
            return ContactsCommand(
                sub = args.positional.getOrNull(0),
                id = args.positional.getOrNull(1)?.toLongOrNull(),
                args = args,
            )
        }

        /**
         * [dv14-A1] ISSUE-01: `-n <N>` is the legacy shorthand for `--max <N>`
         * that the handler used to recover from the positional list — the shared
         * parser binds no value to a single-dash token (`-n` becomes a bare
         * flag and `10` lands in the positional list), so `search -n 10 Alice`
         * took the `10` as the QUERY slot and dropped "Alice".
         *
         * Rewriting the alias to the value-carrying long spelling here keeps
         * [OffloadArgs] (shared by every offload handler) untouched and makes
         * the alias mean the same thing in every position (`-n 10` before or
         * after the query). Only a numeric value is rewritten, so `search -n
         * Alice` binds no page size at all — [resolvePageSize] then refuses the
         * stray `-n` as a usage error instead of letting "Alice" survive in the
         * query slot as if the alias had been honoured; only the two
         * subcommands that HAVE a page size are rewritten, so `get` / `delete`
         * keep their id slots bit-for-bit (`get -n 10` still read id 10).
         *
         * [dv19-A1 A-01] The rewrite runs BEFORE the subcommand probe, and the
         * probe reads the rewritten list. Deciding on the raw list made the
         * promise above false in the `before` position: `-n 10` is still an
         * unbound single-dash token at that point, so "10" landed in
         * `positional[0]`, `sub` came out `"10"` (neither `list` nor `search`),
         * the function returned the ORIGINAL argv and the caller refused with
         * `unknown subcommand '10'` (exit 2) — while `list -n 10` worked. The
         * probe now sees the same value-carrying spelling the caller gets, so
         * both positions agree (the alias is position-independent, as KDoc'd).
         *
         * Pure — no Context, no I/O — so both arms stay testable off-device.
         */
        internal fun bindLegacyShortMax(argv: List<String>): List<String> {
            if ("-n" !in argv) return argv
            val rewritten = ArrayList<String>(argv.size)
            var i = 0
            while (i < argv.size) {
                val token = argv[i]
                val value = argv.getOrNull(i + 1)
                if (token == "-n" && value != null && value.toIntOrNull() != null) {
                    rewritten.add("--max")
                    rewritten.add(value)
                    i += 2
                    continue
                }
                rewritten.add(token)
                i++
            }
            // The subcommand may sit behind an option (`--compact list -n 10`),
            // so ask the parser rather than indexing the raw argv — and ask it
            // about `rewritten`: on the raw list `-n 10` shifts every slot it
            // reads ([dv19-A1 A-01]).
            val sub = OffloadArgs(rewritten, booleanFlags = setOf("help", "h")).positional.getOrNull(0)
            if (sub != "list" && sub != "search") return argv
            return rewritten
        }

        /**
         * [dv28-A6 A-06] The ONE page-size resolver — `list` takes the default
         * 50, `search` passes its documented 20. Three outcomes, one per
         * spelling:
         *
         *  - `--max N` / `--max=N`: the named value [OffloadArgs] consumed,
         *    read as the RAW string. `getInt("max")` is what collapsed
         *    "spelled but unreadable" into "not spelled": `--max abc` came out
         *    `null` and the handler answered the default at exit 0;
         *  - `-n N`: rewritten to `--max N` by [bindLegacyShortMax] BEFORE the
         *    parse, so a `-n` that survives the rewrite carries no number at
         *    all (it trails the argv, or the token after it is not numeric).
         *    That token used to stay in [OffloadArgs.positional] and take the
         *    QUERY slot — `search -n abc Alice` searched for "abc" and dropped
         *    "Alice". Refused here instead;
         *  - nothing spelled: [default], the only path that may answer a
         *    default.
         *
         * Zero / negative / over-[MAX_PAGE_SIZE] is refused, not clamped: the
         * empty page a zero produced was indistinguishable from "no contacts".
         * Pure — no Context, no I/O — so every arm stays testable off-device.
         */
        internal fun resolvePageSize(parsedArgs: OffloadArgs, default: Int = DEFAULT_LIST_MAX): PageSize {
            parsedArgs.get("max")?.let { raw -> return pageSizeOf(raw, "--max") }
            if (parsedArgs.hasFlag("max")) {
                // `--max` with no follower: OffloadArgs files the key as a
                // boolean flag, so "spelled" is still visible here.
                return PageSize.Invalid("--max expects an integer between 1 and $MAX_PAGE_SIZE")
            }
            if (parsedArgs.hasFlag("n")) {
                return PageSize.Invalid(
                    "-n expects an integer between 1 and $MAX_PAGE_SIZE (short form of --max)",
                )
            }
            return PageSize.Ok(default)
        }

        /** Parse + range-check one spelled page size. Pure. */
        private fun pageSizeOf(raw: String, spelled: String): PageSize {
            val value = raw.toIntOrNull()
                ?: return PageSize.Invalid(
                    "$spelled expects an integer between 1 and $MAX_PAGE_SIZE, got '$raw'",
                )
            if (value < 1 || value > MAX_PAGE_SIZE) {
                return PageSize.Invalid(
                    "$spelled expects an integer between 1 and $MAX_PAGE_SIZE, got $value",
                )
            }
            return PageSize.Ok(value)
        }

        /**
         * [dv28-A6 A-06] Usage-error result (exit 2, the code the other
         * offload handlers use for a bad invocation) carrying the accepted
         * range and the usage block, so an agent reads "I spelled it wrong"
         * instead of an empty page.
         */
        private fun pageSizeRefusal(sub: String, message: String): NativeOffloadResult =
            NativeOffloadResult(2, "android-contacts $sub: $message\n$HELP_TEXT")

        /** "?,?,…" placeholder list for an IN (…) selection. Pure. */
        private fun inPlaceholders(count: Int): String = List(count) { "?" }.joinToString(",")

        /**
         * [sweep1014-P10] Aggregate (contactId, value) rows into
         * contactId → values, preserving encounter order within each id.
         * Pure so the batch aggregation stays testable off-device.
         */
        private fun aggregateValues(pairs: List<Pair<Long, String>>): Map<Long, List<String>> {
            val out = LinkedHashMap<Long, MutableList<String>>()
            for ((id, v) in pairs) out.getOrPut(id) { mutableListOf() }.add(v)
            return out
        }

        private const val HELP_TEXT = """android-contacts — read + delete device contacts (requires READ_CONTACTS, plus WRITE_CONTACTS for delete)

Usage:
  android-contacts list [--max N]             List contacts (default 50, range 1..500)
  android-contacts search <query> [--max N]   Search by name (default 20, range 1..500)
  android-contacts get <id>                   Get a single contact by id
  android-contacts delete <id>                Delete a contact by id (PERMANENT — cascades all raw rows)
  android-contacts --help                     Show this help

`-n N` is the legacy short form of `--max N`. A page size that is not an integer,
or that is outside 1..500, is a usage error (exit 2): it is never replaced by the
default and never answered as an empty page.

Output is JSON. Each contact has: id, name, phones[], emails[].
"""
    }
}
