"""
store.py - SQLite persistence for SMS threads/messages and the call log.

One DB per manager at $MDD_DATA/mdd-sim-gateway.sqlite. Messages carry the instance id so a
multi-SIM setup keeps separate conversations. New rows are broadcast to the WebSocket
layer by the caller (main.py).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time

from . import contacts as contacts_format
from . import mms_media, sms_pdu

DATA_DIR = os.environ.get("MDD_DATA", os.path.join(os.getcwd(), "data"))

# Rows that belong to somebody rather than to the gateway -- the address book, and anything
# else that is one person's view of shared data -- carry an owner. This gateway has a single
# administrator, so that owner is the same everywhere; naming it in the
# data, and in every query, keeps the ownership explicit rather than leaving it as an
# assumption spread across the callers.
ADMIN_OWNER = 1
DB_PATH = os.path.join(DATA_DIR, "mdd-sim-gateway.sqlite")
PREVIOUS_DB_PATH = os.path.join(DATA_DIR, "vowifi.sqlite")
_lock = threading.Lock()

# Connectivity timeline. Line state is sampled every few seconds, so it is stored as merged
# segments instead of one row per sample: two days of history stays a handful of rows.
LINE_STATES = ("up", "down", "off")
# The longest silence still treated as one continuous observation. Anything longer is a hole
# in the record (control plane restarted / host powered off) and must stay visible as one
# instead of being interpolated into a healthy stretch.
LINE_STATE_CONTINUITY_SECONDS = 90
LINE_STATE_RETENTION_SECONDS = 3 * 24 * 3600
# A create reply normally arrives within 30 seconds and the scanner runs every five seconds.
# Keep a wider recovery window for a service restart, but do not let an old timed-out send hide
# an unrelated, manually-created SMS with the same recipient and body indefinitely.
LOCAL_MODEM_SMS_CLAIM_SECONDS = 30 * 60


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def schema_version() -> int | None:
    """The history database's schema version before init() touches it; None if it does not
    exist yet (a new installation, or one whose history lives only in the former file)."""
    path = DB_PATH if os.path.exists(DB_PATH) else (
        PREVIOUS_DB_PATH if os.path.isfile(PREVIOUS_DB_PATH) else None)
    if path is None:
        return None
    try:
        with sqlite3.connect(path) as c:
            return int(c.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:
        return None


class MigrationBackupError(RuntimeError):
    """The history database could not be backed up, so it was not migrated."""


def backup_dir() -> str:
    return os.path.join(DATA_DIR, "backups")


def _backup_before_migration() -> str | None:
    """Copy the history database aside before any pending schema step touches it.

    Transactions keep a step from being half applied, but several steps delete rows by
    design -- folded duplicates, retired tables, filed payloads turned into MMS -- and a
    deduplication that is wrong for some installation cannot be undone from inside the
    database. The copy is taken with SQLite's online backup API, checked (integrity, schema
    version, message count) and only then moved into place; any failure raises, and init()
    stops before changing anything. A verified copy for the same version transition is reused
    after a failed migration, so a service restart loop cannot fill the disk with backups.
    Nothing is copied for a current or new database, and copies are never removed automatically.
    """
    if not os.path.exists(DB_PATH):
        return None
    with sqlite3.connect(DB_PATH) as source:
        version = int(source.execute("PRAGMA user_version").fetchone()[0])
        has_history = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                     "AND name='messages'").fetchone() is not None
    if version >= len(_MIGRATIONS) or not has_history:
        return None
    prefix = f"mdd-sim-gateway.v{version}-before-v{len(_MIGRATIONS)}."
    existing = sorted(glob.glob(os.path.join(backup_dir(), f"{prefix}*.sqlite")))
    if existing:
        # A failed migration makes the service manager restart the control plane. Reuse the
        # first verified pre-migration copy instead of writing the whole database every few
        # seconds until the disk fills. Never replace an existing but damaged backup: that is
        # evidence requiring operator attention, not permission to discard the recovery point.
        candidate = existing[0]
        try:
            _verify_backup(candidate, version)
        except Exception as exc:
            raise MigrationBackupError(
                f"existing migration backup {candidate} could not be verified; refusing to "
                f"overwrite it or migrate the database: {exc}") from exc
        return candidate
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = os.path.join(backup_dir(), f"{prefix}{stamp}.sqlite")
    partial = target + ".partial"
    mms_target = target[:-len(".sqlite")] + ".mms"
    mms_partial = mms_target + ".partial"
    try:
        os.makedirs(backup_dir(), mode=0o700, exist_ok=True)
        source = sqlite3.connect(DB_PATH)
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
            expected = source.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            copy.close()
            source.close()
        _verify_backup(partial, version, expected)
        # The attachments the copy refers to go alongside it: the database alone cannot
        # restore an MMS. Hard links cost no space, and part files are never rewritten.
        shutil.rmtree(mms_partial, ignore_errors=True)
        snapshot_mms_files(partial, mms_dir(), mms_partial)
        os.chmod(partial, 0o600)
        os.replace(mms_partial, mms_target)
        os.replace(partial, target)
    except Exception as exc:
        try:
            os.remove(partial)
        except OSError:
            pass
        shutil.rmtree(mms_partial, ignore_errors=True)
        raise MigrationBackupError(
            f"could not back up the history database before upgrading it from schema "
            f"version {version}; nothing was migrated: {exc}") from exc
    return target


def _link_or_copy(source: str, target: str) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def snapshot_mms_files(database: str, source_root: str, target_root: str) -> dict:
    """Hard-link (or, across file systems, copy) every attachment file the history database
    at `database` refers to from `source_root` into `target_root`, laid out the same way.

    Each copy is checked against the size its row records. A file the database refers to
    but that is already gone is counted in "missing" rather than failing the snapshot, so an
    existing inconsistency cannot block a backup. ``missing_parts`` identifies those known
    pre-existing gaps so the archive verifier can distinguish them from a copy defect."""
    with sqlite3.connect(database) as check:
        has_parts = check.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                  "AND name='mms_parts'").fetchone() is not None
        rows = check.execute("SELECT message_id, path, size FROM mms_parts WHERE path!=''"
                             ).fetchall() if has_parts else []
    os.makedirs(target_root, mode=0o700, exist_ok=True)
    copied = 0
    missing_parts = []
    for message_id, name, size in rows:
        if os.path.basename(name) != name or name in ("", ".", ".."):
            continue
        source = os.path.join(source_root, str(int(message_id)), name)
        if not os.path.isfile(source):
            missing_parts.append(f"{int(message_id)}/{name}")
            continue
        directory = os.path.join(target_root, str(int(message_id)))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        target = os.path.join(directory, name)
        _link_or_copy(source, target)
        if os.path.getsize(target) != int(size or 0):
            raise OSError(f"the backup copy of MMS part {message_id}/{name} does not match "
                          "its record")
        copied += 1
    return {"parts": copied, "missing": len(missing_parts),
            "missing_parts": missing_parts}


def snapshot_history(database_target: str, mms_target: str, *, attempts: int = 3) -> dict:
    """A consistent copy of the live history database (SQLite online backup) and of every
    attachment file it refers to, for a full backup taken while the gateway runs.

    A save that commits between the two steps removes files the database copy still points
    at; when that leaves files missing, both are taken again. Returns snapshot_mms_files()'s
    counts."""
    result = {"parts": 0, "missing": 0, "missing_parts": []}
    for attempt in range(max(1, attempts)):
        for leftover in (database_target,):
            if os.path.exists(leftover):
                os.remove(leftover)
        shutil.rmtree(mms_target, ignore_errors=True)
        os.makedirs(os.path.dirname(database_target) or ".", exist_ok=True)
        source, copy = sqlite3.connect(DB_PATH), sqlite3.connect(database_target)
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        result = snapshot_mms_files(database_target, mms_dir(), mms_target)
        if not result["missing"]:
            break
    return result


def _verify_backup(path: str, version: int, messages: int | None = None) -> None:
    check = sqlite3.connect(path)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise OSError("the backup failed its integrity check")
        copied_messages = check.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if int(check.execute("PRAGMA user_version").fetchone()[0]) != version or \
                (messages is not None and copied_messages != messages):
            raise OSError("the backup does not match the database")
    finally:
        check.close()


def init():
    with _lock:
        # Preserve call/SMS history when upgrading an installation that used the former
        # database filename. Copy once and keep the source as a rollback artifact.
        if not os.path.exists(DB_PATH) and os.path.isfile(PREVIOUS_DB_PATH):
            os.makedirs(DATA_DIR, exist_ok=True)
            shutil.copy2(PREVIOUS_DB_PATH, DB_PATH)
        _backup_before_migration()
        with _conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance TEXT NOT NULL,
                    direction TEXT NOT NULL,        -- 'in' | 'out'
                    peer TEXT NOT NULL,             -- phone number / address
                    body TEXT NOT NULL,
                    status TEXT DEFAULT 'ok',       -- ok|pending|sent|delivered|unknown|failed
                    ts INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_msg_inst_peer ON messages(instance, peer, ts);
                -- The identity of every message ever stored, kept when the message itself is
                -- deleted: a text the modem still holds, or a carrier re-delivery, must not
                -- bring back what the user removed. `fingerprint` is exact (network timestamp
                -- included); `content_hash` without the timestamp lets a copy of the same text
                -- arriving over the other transport be recognised within a short window.
                CREATE TABLE IF NOT EXISTS message_identities (
                    scope TEXT NOT NULL,
                    instance TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    message_id INTEGER,
                    created_ts INTEGER NOT NULL,
                    PRIMARY KEY(scope, fingerprint)
                );

                -- Parts of a multi-part (concatenated) inbound SMS, held only until the
                -- whole message can be assembled. The SMSC delivers each part as its own
                -- SMS-DELIVER, out of order and seconds apart; the primary key absorbs the
                -- duplicates it re-pushes when an RP-ACK is missed.
                CREATE TABLE IF NOT EXISTS sms_segments (
                    instance TEXT NOT NULL,
                    peer TEXT NOT NULL,
                    concat_ref INTEGER NOT NULL,
                    total INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    created_ts INTEGER NOT NULL,
                    PRIMARY KEY(instance, peer, concat_ref, total, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_sms_segments_age ON sms_segments(created_ts);

                -- A group flushed incomplete, kept so the parts that arrive after the flush
                -- still land in the message they belong to. Carriers can be minutes late with
                -- a part (10 minutes measured between segment 1 and segment 2 of one text
                -- crossing two networks), which is far longer than a buffer meant to hold a
                -- message can wait. `parts` is the {seq: body} already merged, so the body can
                -- be rebuilt in sequence order without re-parsing the gap marks in the stored
                -- message. One row per flushed group; the row dies when the message completes
                -- or the late window passes.
                CREATE TABLE IF NOT EXISTS sms_late_groups (
                    instance TEXT NOT NULL,
                    peer TEXT NOT NULL,
                    concat_ref INTEGER NOT NULL,
                    total INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    parts TEXT NOT NULL,
                    created_ts INTEGER NOT NULL,
                    PRIMARY KEY(instance, peer, concat_ref, total)
                );
                -- Inbound SMS that were never meant to be read: 8-bit binary payloads, SIM
                -- data-download, silent service pushes. They are kept out of `messages` so
                -- they cannot reach a conversation, a notification or an export, but kept
                -- verbatim — an encrypted payload cannot be identified from a decode, only
                -- from the PDU as it arrived. tpdu_hex is the whole PDU; body_hex is the
                -- user data alone, recovered from Asterisk's byte-per-character widen.
                CREATE TABLE IF NOT EXISTS binary_sms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance TEXT NOT NULL,
                    peer TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    transport TEXT NOT NULL DEFAULT 'vowifi',
                    tp_pid INTEGER,
                    tp_dcs INTEGER,
                    concat_ref INTEGER,
                    concat_total INTEGER,
                    concat_seq INTEGER,
                    udh_hex TEXT NOT NULL DEFAULT '',
                    tpdu_hex TEXT NOT NULL DEFAULT '',
                    body_hex TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_binary_sms_ts ON binary_sms(instance, ts);
                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance TEXT NOT NULL,
                    direction TEXT NOT NULL,        -- 'in' | 'out'
                    peer TEXT NOT NULL,
                    status TEXT DEFAULT '',         -- ringing|answered|ended|missed|failed
                    start_ts INTEGER NOT NULL,
                    end_ts INTEGER
                );
                CREATE TABLE IF NOT EXISTS legacy_history_imports (
                    kind TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    imported_ts INTEGER NOT NULL,
                    PRIMARY KEY(kind, source_id)
                );
                CREATE TABLE IF NOT EXISTS line_states (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance TEXT NOT NULL,
                    state TEXT NOT NULL,        -- 'up' | 'down' | 'off'
                    start_ts INTEGER NOT NULL,
                    end_ts INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',  -- reason_code when the segment began
                    detail TEXT NOT NULL DEFAULT ''   -- evidence behind it (names, resolvers…)
                );
                CREATE INDEX IF NOT EXISTS idx_line_states ON line_states(instance, start_ts);
                CREATE TABLE IF NOT EXISTS line_allowances (
                    instance TEXT PRIMARY KEY,
                    balance TEXT NOT NULL DEFAULT '',
                    valid_until TEXT NOT NULL DEFAULT '',
                    sms_remaining TEXT NOT NULL DEFAULT '',
                    data_remaining TEXT NOT NULL DEFAULT '',
                    voice_remaining TEXT NOT NULL DEFAULT '',
                    activated_at TEXT NOT NULL DEFAULT '',
                    updated_ts INTEGER,
                    source TEXT NOT NULL DEFAULT 'manual'
                );
                CREATE TABLE IF NOT EXISTS allowance_query_rules (
                    instance TEXT PRIMARY KEY,
                    recipient TEXT NOT NULL,
                    body TEXT NOT NULL,
                    updated_ts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS allowance_queries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    body TEXT NOT NULL,
                    carrier_key TEXT NOT NULL DEFAULT '',
                    started_ts INTEGER NOT NULL,
                    transport TEXT NOT NULL DEFAULT 'auto',
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS idx_allowance_queries_inst
                    ON allowance_queries(instance, started_ts);
                CREATE TABLE IF NOT EXISTS allowance_reminders (
                    instance TEXT NOT NULL,
                    expiry_date TEXT NOT NULL,
                    days_before INTEGER NOT NULL,
                    sent_ts INTEGER NOT NULL,
                    PRIMARY KEY(instance, expiry_date, days_before)
                );
                -- Number keeping. Carriers judge a number active by CHARGEABLE events, so
                -- what is scheduled here is a real (paid) action, never a free balance
                -- lookup. Config lives here rather than in the line config because the
                -- engine has no use for it, and saving a line config restarts its container.
                CREATE TABLE IF NOT EXISTS line_keepalive (
                    instance TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    action TEXT NOT NULL DEFAULT 'sms',   -- 'sms' | 'balance_watch'
                    sms_to TEXT NOT NULL DEFAULT '',
                    sms_body TEXT NOT NULL DEFAULT '',
                    verify_charge INTEGER NOT NULL DEFAULT 1,
                    threshold TEXT NOT NULL DEFAULT '',
                    interval_days INTEGER NOT NULL DEFAULT 30,
                    -- The only persistent record of when a line last held a registration:
                    -- line_states is pruned after 3 days and hub.ok_since dies with the
                    -- process, so neither can answer "is this number still being used".
                    last_registered_ts INTEGER NOT NULL DEFAULT 0,
                    last_run_ts INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT NOT NULL DEFAULT '',
                    last_detail TEXT NOT NULL DEFAULT '',
                    next_due_ts INTEGER NOT NULL DEFAULT 0,
                    balance_low_since INTEGER NOT NULL DEFAULT 0,
                    balance_low_last_notified INTEGER NOT NULL DEFAULT 0
                );
                -- Claim ledger: an INSERT OR IGNORE here is what stops a restart mid-sweep
                -- from charging the SIM twice for one due date.
                CREATE TABLE IF NOT EXISTS keepalive_runs_claim (
                    instance TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    claimed_ts INTEGER NOT NULL,
                    PRIMARY KEY(instance, slot)
                );
                -- calls had no index at all: every per-line lookup was a full scan + sort.
                CREATE INDEX IF NOT EXISTS idx_calls_inst ON calls(instance, start_ts);
                -- Voicemail. The audio itself stays on the shared /logs volume: binary_sms
                -- stores payloads as hex in a column, which suits a 140-byte SMS and would be
                -- absurd for a two-minute recording. Only the path and metadata live here.
                CREATE TABLE IF NOT EXISTS voicemails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance TEXT NOT NULL,
                    peer TEXT NOT NULL DEFAULT '',
                    ts INTEGER NOT NULL,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    path TEXT NOT NULL,          -- relative to instances/<id>/logs/
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    listened INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_voicemails_inst ON voicemails(instance, ts);
                -- An address book belongs to whoever keeps it, not to the gateway, so every
                -- row says whose it is and every query names an owner. See ADMIN_OWNER.
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner INTEGER NOT NULL,         -- whose book it is
                    name TEXT NOT NULL,
                    company TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_contacts_owner ON contacts(owner, name);
                -- `number` is what the person typed, and what is shown back to them.
                -- `match_key` is what an arriving number is compared against on any line:
                -- contacts.number_key without a country, which is E.164 when the number is
                -- written internationally and the digits as written when it is not.
                CREATE TABLE IF NOT EXISTS contact_numbers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id INTEGER NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    number TEXT NOT NULL,
                    match_key TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_contact_numbers_contact
                    ON contact_numbers(contact_id);
                CREATE INDEX IF NOT EXISTS idx_contact_numbers_match
                    ON contact_numbers(match_key);
                -- The keys a nationally written number has in each country the gateway has a
                -- line in, where they differ from match_key (contacts.number_keys). Derived from
                -- `number` and rebuilt whenever those countries change (contacts_rekey).
                CREATE TABLE IF NOT EXISTS contact_number_keys (
                    number_id INTEGER NOT NULL,
                    region TEXT NOT NULL,
                    match_key TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_contact_number_keys_number
                    ON contact_number_keys(number_id);
                CREATE INDEX IF NOT EXISTS idx_contact_number_keys_match
                    ON contact_number_keys(region, match_key);
                """
            )
            # migration: per-message failure detail (added later)
            try:
                c.execute("ALTER TABLE messages ADD COLUMN error TEXT")
            except Exception:
                pass
            try:
                c.execute("ALTER TABLE messages ADD COLUMN transport TEXT DEFAULT 'vowifi'")
            except Exception:
                pass
            try:
                c.execute("ALTER TABLE calls ADD COLUMN transport TEXT DEFAULT 'vowifi'")
            except Exception:
                pass
            try:
                # A carrier's answer to a dialled service code arrives as signalling, not
                # audio, so it belongs on the call it answered rather than in the message log.
                c.execute("ALTER TABLE calls ADD COLUMN ussd_text TEXT DEFAULT ''")
            except Exception:
                pass
            try:
                c.execute("ALTER TABLE line_allowances "
                          "ADD COLUMN activated_at TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            try:
                # A message left after an unanswered call belongs to that call, so the log can
                # show them together instead of as two unrelated events.
                c.execute("ALTER TABLE calls ADD COLUMN voicemail_id INTEGER")
            except Exception:
                pass
            # A process exit after ModemManager accepted Create/Send leaves a pending row. On
            # startup its delivery outcome is unknowable, so preserve it and discourage retry.
            c.execute(
                "UPDATE messages SET status='unknown', "
                "error='Cellular SMS submission was interrupted; delivery is unknown.' "
                "WHERE transport='cellular' AND status='pending'")
            # migration: why a down segment began (added later)
            try:
                # The network timestamp of each buffered part (TP-SCTS), so the assembled
                # text is dated -- and identified -- by its first part.
                c.execute("ALTER TABLE sms_segments ADD COLUMN sent_ts INTEGER")
            except Exception:
                pass
            try:
                c.execute("ALTER TABLE line_states ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            try:
                c.execute("ALTER TABLE line_states ADD COLUMN detail TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            _sweep_binary_messages(c)
            _migrate(c)
            _identity_indexes(c)
            orphans = _reconcile(c)
        for mid in orphans:
            shutil.rmtree(_mms_message_dir(mid), ignore_errors=True)
    try:
        sweep_mms_orphans()
    except (OSError, sqlite3.Error):
        pass    # housekeeping; the worker sweeps again later


# Schema steps that must run exactly once, in order. `PRAGMA user_version` records the last
# one applied, so a step may rewrite data -- which the idempotent ALTER-and-ignore migrations
# above cannot safely do, since they run on every start.
def _migrate(c) -> None:
    """Apply each pending step in its own transaction, together with its version bump.

    SQLite DDL is transactional, so a step interrupted by a crash or a power cut rolls back
    whole and runs again on the next start. Steps must therefore never call executescript(),
    which commits whatever is pending before it runs; _script() executes statement by statement.
    """
    for target, step in enumerate(_MIGRATIONS, start=1):
        c.commit()
        if int(c.execute("PRAGMA user_version").fetchone()[0]) >= target:
            continue
        c.execute("BEGIN IMMEDIATE")
        try:
            step(c)
            c.execute(f"PRAGMA user_version={target}")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def _script(c, sql: str) -> None:
    """Run several statements inside the caller's transaction (unlike executescript)."""
    for statement in sql.split(";"):
        if statement.strip():
            c.execute(statement)


def _migration_message_identity(c) -> None:
    """Give every message a transport-independent identity and fold existing duplicates.

    Imports used to be keyed on a fingerprint over the ModemManager object path. Those paths
    are renumbered whenever ModemManager restarts, so a message still held by the modem was
    imported again under its new path, and nothing tied a text delivered over VoWiFi to the
    copy of the same text the modem received from the same SIM. The old markers can never
    match the new identity and are dropped; the new identity is backfilled from the stored
    rows, which is what keeps a message the modem still holds from importing a second time
    on the first poll after the upgrade.
    """
    for statement in (
            "ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'sms'",
            "ALTER TABLE messages ADD COLUMN sent_ts INTEGER",
            "ALTER TABLE messages ADD COLUMN received_ts INTEGER"):
        try:
            c.execute(statement)
        except sqlite3.OperationalError:
            pass
    c.execute("UPDATE messages SET received_ts=ts WHERE received_ts IS NULL")
    # 1.9.3 stored mmcli's placeholder for an unreadable body as the text itself. Those rows
    # are an unassembled multi-part text (imported again once complete) or an MMS
    # notification (filed from VoWiFi, retrieved as MMS from now on) -- never a message.
    c.execute("DELETE FROM messages WHERE transport='cellular' AND direction='in' "
              "AND TRIM(body)='--'")
    rows = c.execute("SELECT id,instance,direction,peer,body,ts,transport,kind FROM messages "
                     "ORDER BY id").fetchall()
    kept: dict[tuple, list[tuple[int, str]]] = {}
    for row in rows:
        content = message_content_hash(row["direction"], row["peer"], row["body"],
                                       row["kind"] or "sms")
        key = (str(row["instance"]), content)
        ts, transport = int(row["ts"] or 0), str(row["transport"] or "vowifi")
        seen = kept.setdefault(key, [])
        # Only inbound copies are folded. Two identical outgoing texts are two sends the user
        # paid for, however close together.
        duplicate = row["direction"] == "in" and any(
            abs(ts - other_ts) <= _duplicate_window(transport, other_transport)
            for other_ts, other_transport in seen)
        if duplicate:
            c.execute("DELETE FROM messages WHERE id=?", (int(row["id"]),))
            continue
        seen.append((ts, transport))
        _record_identity(c, str(row["instance"]), int(row["id"]), row["direction"],
                         row["peer"], row["body"], ts, transport, row["kind"] or "sms")
    # The old markers cannot identify a message any more, but they still record which modem
    # objects were imported -- including ones the user has since deleted. They are kept, under
    # a name no version writes to, so the scanner can recognise such an object by its old
    # fingerprint instead of importing it again (see ingest_message's legacy_fingerprint).
    exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='message_imports'").fetchone()
    if exists:
        c.execute("DROP TABLE IF EXISTS legacy_message_imports")
        c.execute("ALTER TABLE message_imports RENAME TO legacy_message_imports")


def _migration_modem_object_on_message(c) -> None:
    """Record a cellular send's ModemManager object on its own history row.

    local_modem_sms tracked the gateway's own objects in a side table with reservations, a
    daemon-generation key and a content hash. Objects are now deleted once sent, and the
    content comparison alone tells a reused path apart, so the path lives on the message.
    Bound markers are carried over so an object still listed after the upgrade stays claimed.
    """
    for statement in ("ALTER TABLE messages ADD COLUMN modem_path TEXT",
                      "ALTER TABLE messages ADD COLUMN modem_sms_path TEXT"):
        try:
            c.execute(statement)
        except sqlite3.OperationalError:
            pass
    exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='local_modem_sms'").fetchone()
    if exists:
        columns = {row[1] for row in c.execute("PRAGMA table_info(local_modem_sms)")}
        if "message_id" in columns:
            c.execute(
                "UPDATE messages SET modem_path=(SELECT l.modem_path FROM local_modem_sms l "
                "WHERE l.message_id=messages.id AND l.sms_path IS NOT NULL "
                "ORDER BY l.id DESC LIMIT 1), modem_sms_path=(SELECT l.sms_path "
                "FROM local_modem_sms l WHERE l.message_id=messages.id "
                "AND l.sms_path IS NOT NULL ORDER BY l.id DESC LIMIT 1) "
                "WHERE id IN (SELECT message_id FROM local_modem_sms "
                "WHERE sms_path IS NOT NULL)")
        c.execute("DROP TABLE local_modem_sms")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_modem_object "
              "ON messages(instance, modem_sms_path) WHERE modem_sms_path IS NOT NULL")


def _migration_mms(c) -> None:
    """MMS: one `messages` row per MMS (kind='mms') so it sits in its conversation, plus its
    retrieval/sending state and its parts. Part content lives in files under MMS_DIR; a
    database row per image would bloat every backup of the message history."""
    _script(c, """
        CREATE TABLE IF NOT EXISTS mms (
            message_id INTEGER PRIMARY KEY,
            instance TEXT NOT NULL,
            direction TEXT NOT NULL,
            state TEXT NOT NULL,
            transaction_id TEXT NOT NULL DEFAULT '',
            content_location TEXT NOT NULL DEFAULT '',
            message_ref TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            from_addr TEXT NOT NULL DEFAULT '',
            to_addrs TEXT NOT NULL DEFAULT '[]',
            cc_addrs TEXT NOT NULL DEFAULT '[]',
            size INTEGER,
            expiry_ts INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_ts INTEGER,
            last_error TEXT NOT NULL DEFAULT '',
            transport TEXT NOT NULL DEFAULT '',
            delivery TEXT NOT NULL DEFAULT '{}',
            updated_ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mms_state ON mms(state, next_attempt_ts);
        CREATE INDEX IF NOT EXISTS idx_mms_ref ON mms(instance, message_ref);
        CREATE TABLE IF NOT EXISTS mms_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            content_type TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            content_id TEXT NOT NULL DEFAULT '',
            charset TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            path TEXT NOT NULL DEFAULT '',
            text TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_mms_parts_message ON mms_parts(message_id, seq);
    """)
    try:
        # A binary multi-part payload (a long WAP Push) is reassembled from the same buffer
        # as text parts, but must never be flushed as a text message.
        c.execute("ALTER TABLE sms_segments ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'")
    except sqlite3.OperationalError:
        pass


def _migration_identity_scope(c) -> None:
    """Scope identities to the subscriber (the SIM) instead of the line id.

    A line id is only a slot: delete a line and add another SIM, and the new SIM inherits the
    old one's identities; re-add the same SIM under a new id, and the messages its modem still
    holds import again, deleted ones included. The scope is the SIM's ICCID (its IMSI where
    the modem exposes no ICCID), resolved from the line configuration; rows of lines that no
    longer exist, or that have no SIM identity, stay scoped to their line id.
    """
    columns = {row[1] for row in c.execute("PRAGMA table_info(message_identities)")}
    c.execute("ALTER TABLE message_identities RENAME TO message_identities_old")
    c.execute("DROP INDEX IF EXISTS idx_message_identities_content")
    c.execute("DROP INDEX IF EXISTS idx_message_identities_message")
    _script(c, """
        CREATE TABLE message_identities (
            scope TEXT NOT NULL,
            instance TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            transport TEXT NOT NULL,
            ts INTEGER NOT NULL,
            message_id INTEGER,
            created_ts INTEGER NOT NULL,
            PRIMARY KEY(scope, fingerprint)
        );
    """)
    rows = c.execute("SELECT * FROM message_identities_old").fetchall()
    for row in rows:
        scope = (row["scope"] if "scope" in columns and row["scope"]
                 else identity_scope(row["instance"]))
        c.execute("INSERT OR IGNORE INTO message_identities(scope,instance,fingerprint,"
                  "content_hash,transport,ts,message_id,created_ts) VALUES(?,?,?,?,?,?,?,?)",
                  (scope, row["instance"], row["fingerprint"], row["content_hash"],
                   row["transport"], row["ts"], row["message_id"], row["created_ts"]))
    c.execute("DROP TABLE message_identities_old")


def _identity_indexes(c) -> None:
    c.execute("CREATE INDEX IF NOT EXISTS idx_message_identities_content "
              "ON message_identities(scope, content_hash, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_message_identities_message "
              "ON message_identities(message_id)")


# The last step is defined further down with the MMS store it relies on, hence the lambda.
_MIGRATIONS = (_migration_message_identity, _migration_modem_object_on_message, _migration_mms,
               _migration_identity_scope, lambda c: _migration_filed_mms_pushes(c))


def _sweep_binary_messages(c) -> int:
    """Move machine payloads already sitting in `messages` into binary_sms.

    Runs on every startup rather than once behind a migration flag, for two reasons: it is
    self-limiting (a row it moves is no longer in `messages`, so the next sweep finds nothing),
    and an engine image older than the PDU-header patch keeps producing these, so a one-shot
    migration would strand everything that arrives before that image is rebuilt.

    Nothing is discarded — the payload is preserved as hex, recoverable byte for byte. Only
    inbound rows are examined; an outgoing message was composed here and is text by definition.
    """
    moved = 0
    rows = c.execute("SELECT id,instance,peer,body,ts,transport FROM messages "
                     "WHERE direction='in'").fetchall()
    for row in rows:
        body = row["body"] or ""
        if not sms_pdu.looks_binary(body):
            continue
        c.execute("INSERT INTO binary_sms(instance,peer,ts,transport,body_hex) "
                  "VALUES(?,?,?,?,?)",
                  (str(row["instance"]), row["peer"], int(row["ts"]),
                   row["transport"] or "vowifi", sms_pdu.body_to_hex(body)))
        c.execute("DELETE FROM messages WHERE id=?", (row["id"],))
        moved += 1
    return moved


def add_binary_sms(instance: str, peer: str, ts: int | None = None, *,
                   transport: str = "vowifi", tp_pid: int | None = None,
                   tp_dcs: int | None = None, concat: tuple[int, int, int] | None = None,
                   udh_hex: str = "", tpdu_hex: str = "", body_hex: str = "") -> dict:
    """File one non-text payload. Deliberately NOT a `messages` row: it must not reach a
    conversation, a push notification or an export."""
    ts = int(ts or time.time())
    ref, total, seq = concat or (None, None, None)
    with _lock, _conn() as c:
        if transport == "cellular":
            # A modem object read again (a kept object after a restart) is the same payload.
            same = c.execute("SELECT * FROM binary_sms WHERE instance=? AND peer=? AND ts=? "
                             "AND body_hex=? LIMIT 1",
                             (str(instance), peer, ts, body_hex)).fetchone()
            if same:
                return dict(same)
        cur = c.execute(
            "INSERT INTO binary_sms(instance,peer,ts,transport,tp_pid,tp_dcs,"
            "concat_ref,concat_total,concat_seq,udh_hex,tpdu_hex,body_hex) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(instance), peer, ts, transport, tp_pid, tp_dcs, ref, total, seq,
             udh_hex, tpdu_hex, body_hex))
        rid = cur.lastrowid
    return {"id": rid, "instance": str(instance), "peer": peer, "ts": ts,
            "transport": transport, "tp_pid": tp_pid, "tp_dcs": tp_dcs,
            "concat_ref": ref, "concat_total": total, "concat_seq": seq,
            "udh_hex": udh_hex, "tpdu_hex": tpdu_hex, "body_hex": body_hex}


def list_binary_sms(instance: str | None = None, limit: int = 200) -> list[dict]:
    """Filed payloads, newest first."""
    limit = max(1, min(int(limit), 1000))
    with _lock, _conn() as c:
        if instance is None:
            rows = c.execute("SELECT * FROM binary_sms ORDER BY ts DESC, id DESC LIMIT ?",
                             (limit,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM binary_sms WHERE instance=? "
                             "ORDER BY ts DESC, id DESC LIMIT ?",
                             (str(instance), limit)).fetchall()
    return [dict(r) for r in rows]


def migrate_legacy_history(instance_aliases: dict[str, str]) -> dict:
    """Merge legacy history into current line ids once, without exposing its contents."""
    if not instance_aliases or not os.path.isfile(PREVIOUS_DB_PATH):
        return {"calls": 0, "messages": 0}
    imported = {"calls": 0, "messages": 0}
    with _lock, sqlite3.connect(PREVIOUS_DB_PATH) as source, _conn() as dest:
        source.row_factory = sqlite3.Row
        for kind, table in (("call", "calls"), ("message", "messages")):
            try:
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                old = dict(row)
                target = instance_aliases.get(str(old.get("instance") or "").lower())
                if not target:
                    continue
                source_id = int(old["id"])
                marker = dest.execute(
                    "INSERT OR IGNORE INTO legacy_history_imports(kind,source_id,imported_ts) "
                    "VALUES(?,?,?)", (kind, source_id, int(time.time())))
                if marker.rowcount == 0:
                    continue
                if kind == "call":
                    duplicate = dest.execute(
                        "SELECT 1 FROM calls WHERE instance=? AND direction=? AND peer=? "
                        "AND start_ts=? LIMIT 1",
                        (target, old.get("direction", ""), old.get("peer", ""),
                         int(old.get("start_ts") or 0))).fetchone()
                    if not duplicate:
                        dest.execute(
                            "INSERT INTO calls(instance,direction,peer,status,start_ts,end_ts) "
                            "VALUES(?,?,?,?,?,?)",
                            (target, old.get("direction", ""), old.get("peer", ""),
                             old.get("status", ""), int(old.get("start_ts") or 0),
                             old.get("end_ts")))
                        imported["calls"] += 1
                else:
                    duplicate = dest.execute(
                        "SELECT 1 FROM messages WHERE instance=? AND direction=? AND peer=? "
                        "AND body=? AND ts=? LIMIT 1",
                        (target, old.get("direction", ""), old.get("peer", ""),
                         old.get("body", ""), int(old.get("ts") or 0))).fetchone()
                    if not duplicate:
                        dest.execute(
                            "INSERT INTO messages(instance,direction,peer,body,status,ts,error,transport) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (target, old.get("direction", ""), old.get("peer", ""),
                             old.get("body", ""), old.get("status", "ok"),
                             int(old.get("ts") or 0), old.get("error"),
                             old.get("transport") or "vowifi"))
                        imported["messages"] += 1
    return imported


def set_message_status(mid: int, status: str, error: str | None = None):
    with _lock, _conn() as c:
        c.execute("UPDATE messages SET status=?, error=? WHERE id=?", (status, error, mid))


# A part that never gets its siblings must not be lost, so the caller flushes stale groups
# after this many seconds and stores whatever arrived. Carriers deliver the parts of one text
# within seconds; a gap this long means the rest is not coming.
SEGMENT_TIMEOUT = 180


def _group_sent_ts(rows) -> int | None:
    """The network time of a multi-part text: its first part's, as a handset shows it.

    ModemManager stamps an assembled message with the timestamp of its first part, so using
    the same part keeps a VoWiFi copy's identity aligned with the modem's copy of that text.
    """
    stamped = [(int(r["seq"]), int(r["sent_ts"])) for r in rows if r["sent_ts"]]
    return min(stamped)[1] if stamped else None


def add_sms_segment(instance: str, peer: str, concat_ref: int, total: int, seq: int,
                    body: str, ts: int | None = None, *, sent_ts: int | None = None,
                    with_meta: bool = False, kind: str = "text") -> list[str] | dict | None:
    """Buffer one part of a concatenated SMS.

    Returns the full ordered list of part bodies once the LAST missing part arrives (and drops
    the group from the buffer), or None while parts are still outstanding. Re-delivery of a
    part already held is absorbed by the primary key and never completes a group twice: the
    row count only reaches `total` when every distinct seq is present. `with_meta` returns
    {"bodies", "sent_ts"} instead, carrying the network time of the assembled text."""
    ts = int(ts or time.time())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO sms_segments(instance,peer,concat_ref,total,seq,body,created_ts,"
            "sent_ts,kind) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(instance,peer,concat_ref,total,seq) DO NOTHING",
            (str(instance), peer, int(concat_ref), int(total), int(seq), body, ts,
             int(sent_ts) if sent_ts else None, str(kind)))
        rows = c.execute(
            "SELECT seq,body,sent_ts FROM sms_segments "
            "WHERE instance=? AND peer=? AND concat_ref=? AND total=? ORDER BY seq",
            (str(instance), peer, int(concat_ref), int(total))).fetchall()
        if len(rows) < int(total):
            return None
        c.execute(
            "DELETE FROM sms_segments WHERE instance=? AND peer=? AND concat_ref=? AND total=?",
            (str(instance), peer, int(concat_ref), int(total)))
    bodies = [r["body"] for r in rows]
    return {"bodies": bodies, "sent_ts": _group_sent_ts(rows)} if with_meta else bodies


def take_stale_sms_segments(timeout: int = SEGMENT_TIMEOUT,
                            now: int | None = None, kind: str = "text") -> list[dict]:
    """Remove every part group whose FIRST part arrived more than `timeout` seconds ago and
    return what each one had collected, so an incomplete message is still shown rather than
    silently dropped. Each entry carries the ordered bodies plus the seq numbers present, so
    the caller can mark which parts are missing."""
    now = int(now or time.time())
    cutoff = now - int(timeout)
    out: list[dict] = []
    with _lock, _conn() as c:
        groups = c.execute(
            "SELECT instance,peer,concat_ref,total,MIN(created_ts) AS first_ts "
            "FROM sms_segments WHERE kind=? GROUP BY instance,peer,concat_ref,total "
            "HAVING MIN(created_ts) <= ?", (str(kind), cutoff)).fetchall()
        for g in groups:
            key = (g["instance"], g["peer"], g["concat_ref"], g["total"])
            rows = c.execute(
                "SELECT seq,body,sent_ts FROM sms_segments "
                "WHERE instance=? AND peer=? AND concat_ref=? AND total=? ORDER BY seq",
                key).fetchall()
            c.execute(
                "DELETE FROM sms_segments "
                "WHERE instance=? AND peer=? AND concat_ref=? AND total=?", key)
            out.append({"instance": g["instance"], "peer": g["peer"],
                        "concat_ref": g["concat_ref"], "total": int(g["total"]),
                        "first_ts": int(g["first_ts"]), "sent_ts": _group_sent_ts(rows),
                        "seqs": [int(r["seq"]) for r in rows],
                        "bodies": [r["body"] for r in rows]})
    return out


# How long a group flushed incomplete stays open to its missing parts. Measured on live
# networks: the parts of one text can be split across SMSC frontends and arrive ten minutes
# apart, so the window that decides "this message is finished" (SEGMENT_TIMEOUT) is far too
# short to also decide "this part belongs to nothing". An hour costs one small row per
# incomplete message and spares the user a second, near-duplicate fragment in the thread.
SEGMENT_LATE_WINDOW = 3600

_LATE_KEY = "instance=? AND peer=? AND concat_ref=? AND total=?"


def remember_partial_sms_group(instance: str, peer: str, concat_ref: int, total: int,
                               message_id: int, seqs: list[int], bodies: list[str],
                               ts: int | None = None) -> None:
    """Record which message a flushed-incomplete group produced, and what it already held."""
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO sms_late_groups(instance,peer,concat_ref,total,message_id,parts,"
            "created_ts) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(instance,peer,concat_ref,total) DO UPDATE SET "
            "message_id=excluded.message_id, parts=excluded.parts, "
            "created_ts=excluded.created_ts",
            (str(instance), peer, int(concat_ref), int(total), int(message_id),
             json.dumps({str(int(s)): b for s, b in zip(seqs, bodies)}),
             int(ts or time.time())))


def merge_late_sms_segment(instance: str, peer: str, concat_ref: int, total: int, seq: int,
                           body: str, now: int | None = None,
                           window: int = SEGMENT_LATE_WINDOW) -> dict | None:
    """Fold a part that arrived after its group was flushed into the message it produced.

    Returns None when this part belongs to no remembered group — the caller then buffers it
    normally as the start of a new message. Otherwise returns the merged state: the ordered
    parts held so far (so the caller can rebuild the body and mark the gaps that remain), the
    message to update, and whether the text is now whole.

    The concatenation reference is one byte and carriers reuse it, so a repeat of a sequence
    number already merged is only a re-delivery while the text is identical. A DIFFERENT text
    in a slot that is already filled means the reference has wrapped onto a new message: the
    memory is dropped and the part starts a fresh group instead of corrupting the old one."""
    now = int(now or time.time())
    key = (str(instance), peer, int(concat_ref), int(total))
    with _lock, _conn() as c:
        row = c.execute(
            f"SELECT message_id,parts,created_ts FROM sms_late_groups WHERE {_LATE_KEY}",
            key).fetchone()
        if not row:
            return None
        if now - int(row["created_ts"]) > int(window):
            c.execute(f"DELETE FROM sms_late_groups WHERE {_LATE_KEY}", key)
            return None
        try:
            parts = dict(json.loads(row["parts"]))
        except (TypeError, ValueError):
            c.execute(f"DELETE FROM sms_late_groups WHERE {_LATE_KEY}", key)
            return None

        slot = str(int(seq))
        held = parts.get(slot)
        if held is not None and held != body:
            c.execute(f"DELETE FROM sms_late_groups WHERE {_LATE_KEY}", key)
            return None

        duplicate = held is not None
        if not duplicate:
            parts[slot] = body
        complete = len(parts) >= int(total)
        if complete:
            c.execute(f"DELETE FROM sms_late_groups WHERE {_LATE_KEY}", key)
        elif not duplicate:
            c.execute(f"UPDATE sms_late_groups SET parts=? WHERE {_LATE_KEY}",
                      (json.dumps(parts), *key))

    order = sorted(int(k) for k in parts)
    return {"message_id": int(row["message_id"]), "seqs": order,
            "bodies": [parts[str(n)] for n in order],
            "complete": complete, "duplicate": duplicate}


def prune_late_sms_groups(window: int = SEGMENT_LATE_WINDOW, now: int | None = None) -> int:
    """Forget groups whose missing parts never came, so the table cannot grow without bound."""
    cutoff = int(now or time.time()) - int(window)
    with _lock, _conn() as c:
        return c.execute("DELETE FROM sms_late_groups WHERE created_ts <= ?",
                         (cutoff,)).rowcount


# A copy of one text reaching a line over both transports carries the same network timestamp
# when both expose it, and a receipt time a few seconds apart when one of them does not.
_CROSS_TRANSPORT_WINDOW = 180
# A network timestamp this far ahead of receipt is a wrong SMSC clock, not a real time.
_SENT_TS_FUTURE_SLACK = 24 * 3600


def _duplicate_window(transport: str, other: str) -> int:
    return _CROSS_TRANSPORT_WINDOW if transport != other else 0


def _plausible_sent_ts(sent_ts, received_ts: int) -> int | None:
    try:
        value = int(sent_ts or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value > received_ts + _SENT_TS_FUTURE_SLACK:
        return None
    return value


def normalize_peer(peer) -> str:
    """The comparison form of an address: one number written two ways must compare equal.

    A carrier may present the same sender as +447700900123 over IMS and 07700900123 on the
    modem. Comparing the trailing nine digits of a number covers national and international
    forms without a numbering-plan table; an alphanumeric sender compares case-insensitively.
    """
    text = str(peer or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits and len(digits) >= 8 and all(ch.isdigit() or ch in "+-() ." for ch in text):
        return digits[-9:]
    return text.casefold()


def message_content_hash(direction: str, peer, body, kind: str = "sms") -> str:
    raw = "\0".join((str(kind or "sms"), str(direction), normalize_peer(peer), str(body or "")))
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def message_fingerprint(direction: str, peer, body, ts: int, kind: str = "sms") -> str:
    raw = "\0".join(("v2", message_content_hash(direction, peer, body, kind), str(int(ts))))
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


# Resolves a line id to the subscriber whose messages it holds ("iccid:..." or "imsi:..."),
# or "" when unknown. The control plane installs one backed by the line configuration; the
# store itself has no view of configuration.
_subscriber_resolver = None


def set_subscriber_resolver(resolver) -> None:
    global _subscriber_resolver
    _subscriber_resolver = resolver


def identity_scope(instance: str) -> str:
    """Whose messages an identity belongs to: the SIM where known, else the line id."""
    subscriber = ""
    if _subscriber_resolver is not None:
        try:
            subscriber = str(_subscriber_resolver(str(instance)) or "")
        except Exception:  # noqa: BLE001 -- identity must not fail on a config read
            subscriber = ""
    return subscriber or f"line:{instance}"


def _identity_scopes(instance: str) -> tuple[str, str]:
    """The scope written for new identities, and the line-id scope older rows may carry."""
    return identity_scope(instance), f"line:{instance}"


def _legacy_imported(c, instance: str, fingerprint: str) -> bool:
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                     "AND name='legacy_message_imports'").fetchone():
        return False
    return bool(c.execute("SELECT 1 FROM legacy_message_imports WHERE fingerprint=? "
                          "AND instance=?", (str(fingerprint), str(instance))).fetchone())


def _reconcile(c) -> list[int]:
    """Repair what an older version may have left after a rollback and a new upgrade.

    Runs on every start and is idempotent. An older version recreates the tables this one
    retired and writes messages without identities; it also deletes messages without their
    MMS rows and files. None of that can be expressed as a one-time migration, because the
    database version already says it is current.
    """
    missing = c.execute(
        "SELECT m.id,m.instance,m.direction,m.peer,m.body,m.ts,m.transport,m.kind "
        "FROM messages m WHERE NOT EXISTS "
        "(SELECT 1 FROM message_identities i WHERE i.message_id=m.id)").fetchall()
    for row in missing:
        if (row["kind"] or "sms") != "sms":
            continue
        _record_identity(c, str(row["instance"]), int(row["id"]), row["direction"], row["peer"],
                         row["body"], int(row["ts"] or 0), row["transport"] or "vowifi")
    c.execute("UPDATE messages SET received_ts=ts WHERE received_ts IS NULL")
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                 "AND name='local_modem_sms'").fetchone():
        _migration_modem_object_on_message(c)
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                 "AND name='message_imports'").fetchone():
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                     "AND name='legacy_message_imports'").fetchone():
            c.execute("INSERT OR IGNORE INTO legacy_message_imports SELECT * FROM message_imports")
            c.execute("DROP TABLE message_imports")
        else:
            c.execute("ALTER TABLE message_imports RENAME TO legacy_message_imports")
    _convert_filed_mms_pushes(c)
    orphans = [int(r[0]) for r in c.execute(
        "SELECT message_id FROM mms WHERE message_id NOT IN (SELECT id FROM messages)")]
    if orphans:
        marks = _placeholders(len(orphans))
        c.execute(f"DELETE FROM mms_parts WHERE message_id IN ({marks})", orphans)
        c.execute(f"DELETE FROM mms WHERE message_id IN ({marks})", orphans)
    return orphans


def _record_identity(c, instance: str, message_id: int, direction: str, peer, body, ts: int,
                     transport: str, kind: str = "sms") -> None:
    c.execute(
        "INSERT OR IGNORE INTO message_identities(scope,instance,fingerprint,content_hash,"
        "transport,ts,message_id,created_ts) VALUES(?,?,?,?,?,?,?,?)",
        (identity_scope(instance), instance, message_fingerprint(direction, peer, body, ts, kind),
         message_content_hash(direction, peer, body, kind), str(transport or "vowifi"),
         int(ts), int(message_id), int(time.time())))


def _insert_message(c, instance: str, direction: str, peer: str, body: str, *, status: str,
                    transport: str, ts: int, received_ts: int, sent_ts: int | None = None,
                    kind: str = "sms", identity_ts: int | None = None,
                    identity: str | None = None) -> dict:
    cur = c.execute(
        "INSERT INTO messages(instance,direction,peer,body,status,ts,transport,kind,sent_ts,"
        "received_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (instance, direction, peer, body, status, int(ts), transport, kind, sent_ts,
         int(received_ts)))
    mid = int(cur.lastrowid)
    _record_identity(c, instance, mid, direction, peer, body if identity is None else identity,
                     ts if identity_ts is None else identity_ts, transport, kind)
    return {"id": mid, "instance": instance, "direction": direction, "peer": peer,
            "body": body, "status": status, "error": None, "ts": int(ts),
            "transport": transport, "kind": kind, "sent_ts": sent_ts,
            "received_ts": int(received_ts)}


def set_message_body(mid: int, body: str) -> dict | None:
    """Replace a stored message's text and return the record as it now reads."""
    with _lock, _conn() as c:
        c.execute("UPDATE messages SET body=? WHERE id=?", (body, int(mid)))
        row = c.execute(
            "SELECT id,instance,direction,peer,body,status,error,ts,transport,kind "
            "FROM messages WHERE id=?", (int(mid),)).fetchone()
        if row and (row["kind"] or "sms") == "sms":
            # The text grew, so its identity did too; the identity of the partial text stays
            # recorded, which keeps a late re-delivery of the partial form from reappearing.
            _record_identity(c, str(row["instance"]), int(row["id"]), row["direction"],
                             row["peer"], body, int(row["ts"]), row["transport"] or "vowifi",
                             row["kind"] or "sms")
    return dict(row) if row else None


def add_message(instance: str, direction: str, peer: str, body: str, status: str = "ok",
                transport: str = "vowifi", ts: int | None = None) -> dict:
    """Store one message unconditionally (a send the user made, or an already-deduplicated
    delivery). Its identity is still recorded so a later copy of it is recognised."""
    ts = int(ts or time.time())
    with _lock, _conn() as c:
        return _insert_message(c, str(instance), direction, peer, body, status=status,
                               transport=transport, ts=ts, received_ts=ts)


def ingest_message(instance: str, direction: str, peer: str, body: str, *,
                   transport: str, sent_ts: int | None = None,
                   received_ts: int | None = None, kind: str = "sms",
                   status: str = "ok", identity: str | None = None,
                   on_insert=None, legacy_fingerprint: str | None = None) -> dict | None:
    """Store a message delivered from outside unless this line already has it.

    Returns the stored record, or None when it is a copy of a message already stored -- or
    stored once and since deleted. Two tests, atomically with the insert:
      - the exact identity (line, direction, sender, text, network timestamp) was seen before:
        a carrier re-delivery, or a modem object read again after ModemManager restarted;
      - the same text from the same sender reached this line over the other transport within
        a short window: the SIM is registered both over VoWiFi and on the modem, and the
        network delivered to both.
    `sent_ts` is the network's timestamp (TP-SCTS) when the transport exposes one; it becomes
    the displayed time, while `received_ts` stays the local receipt time. `identity` replaces
    the body in the identity when the body is not what makes the message unique (an MMS is
    identified by its MMSC location before any text is known); `on_insert(c, record)` runs in
    the same transaction as the insert.
    """
    with _lock, _conn() as c:
        return _ingest(c, instance, direction, peer, body, transport=transport,
                       sent_ts=sent_ts, received_ts=received_ts, kind=kind, status=status,
                       identity=identity, on_insert=on_insert,
                       legacy_fingerprint=legacy_fingerprint)


def _ingest(c, instance: str, direction: str, peer: str, body: str, *, transport: str,
            sent_ts: int | None = None, received_ts: int | None = None, kind: str = "sms",
            status: str = "ok", identity: str | None = None, on_insert=None,
            legacy_fingerprint: str | None = None) -> dict | None:
    """ingest_message() on the caller's connection, inside the caller's lock and transaction."""
    instance = str(instance)
    now = int(time.time())
    received_ts = int(received_ts or now)
    network_ts = _plausible_sent_ts(sent_ts, received_ts)
    ts = network_ts or received_ts
    # An outgoing object read back from a modem or SIM normally carries no timestamp at all,
    # and its first-seen time changes on every read. Its identity is then its content alone:
    # two identical texts to one recipient that both lack a timestamp are indistinguishable.
    identity_ts = network_ts or (received_ts if direction == "in" else 0)
    if identity is not None:
        # An explicit identity is unique by itself (an MMSC location), and a resent
        # notification carries a new network timestamp: time must not split it in two.
        identity_ts = 0
    key = body if identity is None else identity
    fingerprint = message_fingerprint(direction, peer, key, identity_ts, kind)
    content = message_content_hash(direction, peer, key, kind)
    scope, line_scope = _identity_scopes(instance)
    if c.execute("SELECT 1 FROM message_identities WHERE scope IN (?,?) AND fingerprint=?",
                 (scope, line_scope, fingerprint)).fetchone():
        return None
    if legacy_fingerprint and _legacy_imported(c, instance, legacy_fingerprint):
        # Imported by a version before message identities, and not in the history now:
        # the user deleted it. Remember it under the current identity and keep it deleted.
        c.execute(
            "INSERT OR IGNORE INTO message_identities(scope,instance,fingerprint,"
            "content_hash,transport,ts,message_id,created_ts) VALUES(?,?,?,?,?,?,?,?)",
            (scope, instance, fingerprint, content, str(transport), identity_ts, None, now))
        return None
    window = _CROSS_TRANSPORT_WINDOW
    twin = c.execute(
        "SELECT message_id FROM message_identities WHERE scope IN (?,?) AND content_hash=? "
        "AND transport<>? AND ts BETWEEN ? AND ? LIMIT 1",
        (scope, line_scope, content, str(transport), identity_ts - window,
         identity_ts + window)).fetchone() if identity_ts else None
    if twin:
        # Remember this copy's exact identity too, so its next re-delivery is an exact hit.
        c.execute(
            "INSERT OR IGNORE INTO message_identities(scope,instance,fingerprint,"
            "content_hash,transport,ts,message_id,created_ts) VALUES(?,?,?,?,?,?,?,?)",
            (scope, instance, fingerprint, content, str(transport), identity_ts,
             twin["message_id"], now))
        return None
    record = _insert_message(c, instance, direction, peer, body, status=status,
                             transport=transport, ts=ts, received_ts=received_ts,
                             sent_ts=network_ts, kind=kind, identity_ts=identity_ts,
                             identity=identity)
    if on_insert is not None:
        on_insert(c, record)
    return record


ALLOWANCE_FIELDS = ("balance", "valid_until", "sms_remaining", "data_remaining",
                    "voice_remaining", "activated_at")


def get_allowance(instance: str) -> dict:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM line_allowances WHERE instance=?",
                        (str(instance),)).fetchone()
    if row:
        return dict(row)
    return {"instance": str(instance), **{key: "" for key in ALLOWANCE_FIELDS},
            "updated_ts": None, "source": "manual"}


def save_allowance(instance: str, values: dict, source: str = "manual",
                   updated_ts: int | None = None) -> dict:
    """Replace a line's allowance snapshot. Values stay as display strings so currencies,
    carrier units and plan-specific wording are not silently normalized away."""
    iid = str(instance)
    clean = {key: str(values.get(key) or "").strip() for key in ALLOWANCE_FIELDS}
    stamp = int(updated_ts or time.time())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO line_allowances(instance,balance,valid_until,sms_remaining,"
            "data_remaining,voice_remaining,activated_at,updated_ts,source) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(instance) DO UPDATE SET balance=excluded.balance,"
            "valid_until=excluded.valid_until,sms_remaining=excluded.sms_remaining,"
            "data_remaining=excluded.data_remaining,voice_remaining=excluded.voice_remaining,"
            "activated_at=excluded.activated_at,"
            "updated_ts=excluded.updated_ts,source=excluded.source",
            (iid, clean["balance"], clean["valid_until"], clean["sms_remaining"],
             clean["data_remaining"], clean["voice_remaining"], clean["activated_at"],
             stamp, str(source)),
        )
    return get_allowance(iid)


def get_allowance_query_rule(instance: str) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT recipient,body,updated_ts FROM allowance_query_rules "
                        "WHERE instance=?", (str(instance),)).fetchone()
    return dict(row) if row else None


def save_allowance_query_rule(instance: str, recipient: str, body: str) -> dict:
    stamp = int(time.time())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO allowance_query_rules(instance,recipient,body,updated_ts) "
            "VALUES(?,?,?,?) ON CONFLICT(instance) DO UPDATE SET "
            "recipient=excluded.recipient,body=excluded.body,updated_ts=excluded.updated_ts",
            (str(instance), str(recipient), str(body), stamp),
        )
    return get_allowance_query_rule(instance)


def delete_allowance_query_rule(instance: str) -> bool:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM allowance_query_rules WHERE instance=?",
                        (str(instance),))
        return cur.rowcount > 0


def clear_allowance_data(instance: str) -> None:
    """Remove SIM-specific cached usage and query settings before a reusable line id is freed."""
    with _lock, _conn() as c:
        for table in ("line_allowances", "allowance_query_rules", "allowance_queries",
                      "allowance_reminders", "line_keepalive", "keepalive_runs_claim",
                      "voicemails"):
            # Some recovery/tests open a minimal legacy DB before init() has created these
            # optional tables. Line deletion must still succeed in that degraded state.
            try:
                c.execute(f"DELETE FROM {table} WHERE instance=?", (str(instance),))
            except sqlite3.OperationalError:
                pass


def claim_allowance_reminder(instance: str, expiry_date: str, days_before: int,
                              sent_ts: int | None = None) -> bool:
    """Atomically reserve one reminder so restarts and overlapping pollers cannot duplicate it."""
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO allowance_reminders"
            "(instance,expiry_date,days_before,sent_ts) VALUES(?,?,?,?)",
            (str(instance), str(expiry_date), int(days_before),
             int(sent_ts or time.time())),
        )
        return cur.rowcount == 1


KEEPALIVE_DEFAULTS = {
    "enabled": 0, "action": "sms", "sms_to": "", "sms_body": "", "verify_charge": 1,
    "threshold": "", "interval_days": 30, "last_registered_ts": 0, "last_run_ts": 0,
    "last_status": "", "last_detail": "", "next_due_ts": 0,
    "balance_low_since": 0, "balance_low_last_notified": 0,
}
_KEEPALIVE_CONFIG_KEYS = ("enabled", "action", "sms_to", "sms_body", "verify_charge",
                          "threshold", "interval_days")
_KEEPALIVE_STATE_KEYS = ("last_registered_ts", "last_run_ts", "last_status", "last_detail",
                         "next_due_ts", "balance_low_since", "balance_low_last_notified")


def get_keepalive(instance: str) -> dict:
    """Always returns a full record: a line that was never configured reads as the defaults
    rather than as an absence every caller would have to handle."""
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM line_keepalive WHERE instance=?",
                        (str(instance),)).fetchone()
    out = {**KEEPALIVE_DEFAULTS, "instance": str(instance)}
    if row:
        out.update({k: v for k, v in dict(row).items() if k in out or k == "instance"})
    return out


def _save_keepalive_fields(instance: str, values: dict, allowed: tuple) -> dict:
    clean = {k: v for k, v in (values or {}).items() if k in allowed}
    if not clean:
        return get_keepalive(instance)
    cols = ",".join(clean)
    marks = ",".join("?" for _ in clean)
    sets = ",".join(f"{k}=excluded.{k}" for k in clean)
    with _lock, _conn() as c:
        c.execute(
            f"INSERT INTO line_keepalive(instance,{cols}) VALUES(?,{marks}) "
            f"ON CONFLICT(instance) DO UPDATE SET {sets}",
            (str(instance), *clean.values()),
        )
    return get_keepalive(instance)


def save_keepalive_config(instance: str, values: dict) -> dict:
    """Write only the user-editable fields; scheduler state is never clobbered by a save."""
    return _save_keepalive_fields(instance, values, _KEEPALIVE_CONFIG_KEYS)


def save_keepalive_state(instance: str, values: dict) -> dict:
    """Write only scheduler-owned fields, so a run in flight cannot revert the user's config."""
    return _save_keepalive_fields(instance, values, _KEEPALIVE_STATE_KEYS)


def touch_line_registered(instance: str, ts: int | None = None) -> None:
    """Remember that this line held a registration. Called from the status poller, so it is
    deliberately a single narrow UPDATE-or-INSERT and nothing else."""
    stamp = int(ts if ts is not None else time.time())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO line_keepalive(instance,last_registered_ts) VALUES(?,?) "
            "ON CONFLICT(instance) DO UPDATE SET last_registered_ts=excluded.last_registered_ts",
            (str(instance), stamp),
        )


def claim_keepalive_run(instance: str, slot: str, ts: int | None = None) -> bool:
    """True for exactly one caller per (line, due date). The claim is what makes a restart
    mid-sweep safe: the action being scheduled costs the user real money."""
    stamp = int(ts if ts is not None else time.time())
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO keepalive_runs_claim(instance,slot,claimed_ts) "
            "VALUES(?,?,?)", (str(instance), str(slot), stamp))
        if not cur.rowcount:
            return False
        # Bounded like allowance_queries: the ledger only needs enough history to keep
        # recent slots from re-firing.
        c.execute("DELETE FROM keepalive_runs_claim WHERE instance=? AND slot NOT IN "
                  "(SELECT slot FROM keepalive_runs_claim WHERE instance=? "
                  "ORDER BY claimed_ts DESC LIMIT 20)", (str(instance), str(instance)))
    return True


VOICEMAIL_KEEP_PER_LINE = 30
VOICEMAIL_KEEP_BYTES = 200 * 1024 * 1024


def add_voicemail(instance: str, peer: str, path: str, duration_seconds: int,
                  size_bytes: int, ts: int | None = None) -> tuple[dict, list[str]]:
    """Store one recording and report which files fell out of the retention window.

    messages/calls grow without bound and are only ever trimmed by hand; audio cannot afford
    that on an SD card, so this bounds itself at insert time the way allowance_queries does.
    Deleting the files is the caller's job — the store does not touch the filesystem.
    """
    stamp = int(ts if ts is not None else time.time())
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO voicemails(instance,peer,ts,duration_seconds,path,size_bytes) "
            "VALUES(?,?,?,?,?,?)",
            (str(instance), str(peer or ""), stamp, int(duration_seconds), str(path),
             int(size_bytes)))
        vid = int(cur.lastrowid)
        rows = [dict(r) for r in c.execute(
            "SELECT id,path,size_bytes FROM voicemails WHERE instance=? ORDER BY ts DESC,id DESC",
            (str(instance),))]
        evicted, running = [], 0
        for index, row in enumerate(rows):
            running += int(row["size_bytes"] or 0)
            if index >= VOICEMAIL_KEEP_PER_LINE or running > VOICEMAIL_KEEP_BYTES:
                evicted.append(row)
        if evicted:
            c.execute(
                f"DELETE FROM voicemails WHERE id IN ({_placeholders(len(evicted))})",
                [row["id"] for row in evicted])
        record = dict(c.execute("SELECT * FROM voicemails WHERE id=?", (vid,)).fetchone())
    return record, [str(row["path"]) for row in evicted]


def list_voicemails(instance: str, limit: int = 100) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM voicemails WHERE instance=? "
                         "ORDER BY ts DESC, id DESC LIMIT ?",
                         (str(instance), int(limit))).fetchall()
    return [dict(r) for r in rows]


def get_voicemail(instance: str, vid: int) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM voicemails WHERE instance=? AND id=?",
                        (str(instance), int(vid))).fetchone()
    return dict(row) if row else None


def set_voicemail_listened(instance: str, vid: int, listened: bool = True) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE voicemails SET listened=? WHERE instance=? AND id=?",
                  (1 if listened else 0, str(instance), int(vid)))


def unheard_voicemail_counts() -> dict[str, int]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT instance, COUNT(*) AS n FROM voicemails "
                         "WHERE listened=0 GROUP BY instance").fetchall()
    return {str(r["instance"]): int(r["n"]) for r in rows}


def delete_voicemails(instance: str, ids: list[int] | None = None) -> list[str]:
    """Remove records and return the paths whose files the caller must now delete."""
    with _lock, _conn() as c:
        if ids:
            rows = c.execute(
                f"SELECT path FROM voicemails WHERE instance=? AND id IN "
                f"({_placeholders(len(ids))})", [str(instance), *[int(i) for i in ids]]
            ).fetchall()
            c.execute(f"DELETE FROM voicemails WHERE instance=? AND id IN "
                      f"({_placeholders(len(ids))})",
                      [str(instance), *[int(i) for i in ids]])
        else:
            rows = c.execute("SELECT path FROM voicemails WHERE instance=?",
                             (str(instance),)).fetchall()
            c.execute("DELETE FROM voicemails WHERE instance=?", (str(instance),))
    return [str(r["path"]) for r in rows]


def call_has_voicemail(instance: str, call_id: int) -> bool:
    with _lock, _conn() as c:
        row = c.execute("SELECT voicemail_id FROM calls WHERE instance=? AND id=?",
                        (str(instance), int(call_id))).fetchone()
    return bool(row and row["voicemail_id"])


def link_voicemail_to_call(instance: str, peer: str, voicemail_id: int,
                           within_s: int = 900) -> dict | None:
    """Attach a recording to the call it was left after — the newest inbound call from that
    number, mirroring how a service-code reply finds its call in set_ussd_for_peer."""
    cutoff = int(time.time()) - int(within_s)
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT id FROM calls WHERE instance=? AND direction='in' AND peer=? "
            "AND start_ts>=? ORDER BY start_ts DESC, id DESC LIMIT 1",
            (str(instance), str(peer or ""), cutoff)).fetchone()
        if not row:
            return None
        c.execute("UPDATE calls SET voicemail_id=? WHERE id=?", (int(voicemail_id), row["id"]))
        updated = c.execute("SELECT * FROM calls WHERE id=?", (row["id"],)).fetchone()
    return dict(updated) if updated else None


def start_allowance_query(instance: str, recipient: str, body: str, carrier_key: str,
                          transport: str, started_ts: int | None = None) -> dict:
    stamp = int(started_ts or time.time())
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO allowance_queries(instance,recipient,body,carrier_key,started_ts,"
            "transport,status) VALUES(?,?,?,?,?,?, 'pending')",
            (str(instance), str(recipient), str(body), str(carrier_key), stamp,
             str(transport)),
        )
        qid = int(cur.lastrowid)
        # Query history is useful for debugging, but has no reason to grow without bound.
        c.execute("DELETE FROM allowance_queries WHERE instance=? AND id NOT IN "
                  "(SELECT id FROM allowance_queries WHERE instance=? ORDER BY id DESC LIMIT 20)",
                  (str(instance), str(instance)))
    return {"id": qid, "instance": str(instance), "recipient": str(recipient),
            "body": str(body), "carrier_key": str(carrier_key), "started_ts": stamp,
            "transport": str(transport), "status": "pending"}


def set_allowance_query_status(query_id: int, status: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE allowance_queries SET status=? WHERE id=?",
                  (str(status), int(query_id)))


def latest_allowance_query(instance: str) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM allowance_queries WHERE instance=? "
                        "ORDER BY id DESC LIMIT 1", (str(instance),)).fetchone()
    return dict(row) if row else None


def allowance_query_replies(instance: str, recipient: str, started_ts: int,
                            until_ts: int) -> list[dict]:
    """Read only replies belonging to an explicit, recent query attempt."""
    with _lock, _conn() as c:
        rows = c.execute(
            # Receipt time, not the displayed network timestamp: an SMSC clock a few seconds
            # behind ours must not place the carrier's answer before the query that asked.
            "SELECT id,peer,body,ts FROM messages WHERE instance=? AND direction='in' "
            "AND peer=? AND COALESCE(received_ts,ts)>=? AND COALESCE(received_ts,ts)<=? "
            "ORDER BY ts,id",
            (str(instance), str(recipient), int(started_ts), int(until_ts)),
        ).fetchall()
    return [dict(row) for row in rows]


def begin_local_modem_sms(instance: str, recipient: str, body: str) -> int:
    """Write the history row of a cellular send before its ModemManager object exists."""
    now = int(time.time())
    with _lock, _conn() as c:
        rec = _insert_message(c, str(instance), "out", str(recipient), str(body),
                              status="pending", transport="cellular", ts=now, received_ts=now)
    return int(rec["id"])


def bind_local_modem_sms(message_id: int, modem_path: str, sms_path: str) -> bool:
    """Record which ModemManager object carries a cellular send."""
    with _lock, _conn() as c:
        cur = c.execute("UPDATE messages SET modem_path=?,modem_sms_path=? "
                        "WHERE id=? AND transport='cellular' AND direction='out'",
                        (str(modem_path), str(sms_path), int(message_id)))
        return cur.rowcount == 1


def get_message(mid: int) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM messages WHERE id=?", (int(mid),)).fetchone()
        return _with_mms(c, [dict(row)])[0] if row else None


def owns_local_modem_sms(instance: str, modem_path: str, sms_path: str, peer: str,
                         body: str, now: int | None = None) -> bool:
    """Whether an outgoing ModemManager object was created by this gateway's own send.

    The bound path identifies it, with the content compared as well: ModemManager renumbers
    objects when it restarts, so a path alone may by now name somebody else's message. A send
    interrupted between Create and bind (a timed-out reply, a process exit) leaves its row
    unbound; a recent unbound row with the same content claims the object, which keeps the
    next restart from importing the gateway's own text as a second, external send.
    """
    now = int(now or time.time())
    content = message_content_hash("out", peer, body)
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT id,peer,body FROM messages WHERE instance=? AND transport='cellular' "
            "AND direction='out' AND modem_path=? AND modem_sms_path=?",
            (str(instance), str(modem_path), str(sms_path))).fetchall()
        if any(message_content_hash("out", r["peer"], r["body"]) == content for r in rows):
            return True
        candidates = c.execute(
            "SELECT id,peer,body FROM messages WHERE instance=? AND transport='cellular' "
            "AND direction='out' AND modem_sms_path IS NULL AND ts>=? ORDER BY ts DESC,id DESC",
            (str(instance), now - LOCAL_MODEM_SMS_CLAIM_SECONDS)).fetchall()
        for row in candidates:
            if message_content_hash("out", row["peer"], row["body"]) == content:
                c.execute("UPDATE messages SET modem_path=?,modem_sms_path=? WHERE id=?",
                          (str(modem_path), str(sms_path), int(row["id"])))
                return True
    return False


# ----------------------------- MMS -----------------------------
# Inbound: notified -> downloading -> retrieved | failed | expired (failed may be retried).
# Outbound: sending -> sent -> delivered | failed.
MMS_STATES = ("notified", "downloading", "retrieved", "failed", "expired", "sending", "sent",
              "delivered")


def mms_dir() -> str:
    return os.path.join(DATA_DIR, "mms")


def _mms_message_dir(message_id: int) -> str:
    return os.path.join(mms_dir(), str(int(message_id)))


def _mms_public(row, parts) -> dict:
    record = dict(row)
    for key in ("to_addrs", "cc_addrs"):
        try:
            record[key] = json.loads(record.get(key) or "[]")
        except ValueError:
            record[key] = []
    try:
        record["delivery"] = json.loads(record.get("delivery") or "{}")
    except ValueError:
        record["delivery"] = {}
    # The MMSC location is a bearer credential for the content; the browser never needs it.
    record.pop("content_location", None)
    record["parts"] = [{k: p[k] for k in ("id", "seq", "content_type", "name", "content_id",
                                          "charset", "size", "text")} for p in parts]
    return record


def _with_mms(c, messages: list[dict]) -> list[dict]:
    ids = [int(m["id"]) for m in messages if (m.get("kind") or "sms") == "mms"]
    if not ids:
        return messages
    marks = _placeholders(len(ids))
    states = {int(r["message_id"]): r for r in c.execute(
        f"SELECT * FROM mms WHERE message_id IN ({marks})", ids)}
    parts: dict[int, list] = {}
    for r in c.execute(f"SELECT * FROM mms_parts WHERE message_id IN ({marks}) "
                       "ORDER BY message_id, seq", ids):
        parts.setdefault(int(r["message_id"]), []).append(r)
    for message in messages:
        row = states.get(int(message["id"]))
        if row is not None:
            message["mms"] = _mms_public(row, parts.get(int(message["id"]), []))
    return messages


def canonical_peer(instance: str, peer: str) -> str:
    """The spelling this line's history already uses for `peer`, so one correspondent keeps
    one conversation. An MMSC often writes the sender without "+" where the SMS path had it."""
    with _lock, _conn() as c:
        return _canonical_peer(c, instance, peer)


def _canonical_peer(c, instance: str, peer: str) -> str:
    key = normalize_peer(peer)
    if not key:
        return str(peer or "")
    rows = c.execute("SELECT DISTINCT peer FROM messages WHERE instance=? "
                     "ORDER BY peer", (str(instance),)).fetchall()
    for row in rows:
        if normalize_peer(row["peer"]) == key:
            return str(row["peer"])
    return str(peer or "")


def ingest_mms_notification(instance: str, *, peer: str, transport: str,
                            content_location: str, transaction_id: str = "",
                            subject: str = "", size: int | None = None,
                            expiry_ts: int | None = None, sent_ts: int | None = None,
                            to_addrs: list[str] | None = None) -> dict | None:
    """Store one MMS notification as a pending MMS, unless this line already has it.

    The MMSC location identifies the MMS: the same notification reaches a SIM registered over
    VoWiFi and on its modem, and a carrier resends it when a notify-response goes missing.
    """
    with _lock, _conn() as c:
        mid = _ingest_mms_notification(
            c, instance, peer=peer, transport=transport, content_location=content_location,
            transaction_id=transaction_id, subject=subject, size=size, expiry_ts=expiry_ts,
            sent_ts=sent_ts, to_addrs=to_addrs)
    return get_message(mid) if mid else None


def _ingest_mms_notification(c, instance: str, *, peer: str, transport: str,
                             content_location: str, transaction_id: str = "",
                             subject: str = "", size: int | None = None,
                             expiry_ts: int | None = None, sent_ts: int | None = None,
                             to_addrs: list[str] | None = None) -> int | None:
    now = int(time.time())

    def create(c, record):
        c.execute(
            "INSERT INTO mms(message_id,instance,direction,state,transaction_id,content_location,"
            "subject,from_addr,to_addrs,size,expiry_ts,transport,next_attempt_ts,updated_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record["id"], str(instance), "in", "notified", str(transaction_id or ""),
             str(content_location), str(subject or ""), str(peer or ""),
             json.dumps(list(to_addrs or [])), size, expiry_ts, str(transport), now, now))

    rec = _ingest(c, instance, "in", peer, subject or "", transport=transport,
                  sent_ts=sent_ts, kind="mms", identity=f"mms:{content_location}",
                  on_insert=create)
    return int(rec["id"]) if rec else None


def mms_for_download(message_id: int) -> dict | None:
    """The full MMS state row, including the MMSC location, for the retrieval worker."""
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM mms WHERE message_id=?", (int(message_id),)).fetchone()
    return dict(row) if row else None


_MMS_PUSH_STATUS = {0x80: "expired", 0x81: "retrieved", 0x82: "rejected", 0x83: "deferred",
                    0x84: "unrecognised", 0x85: "indeterminate", 0x86: "forwarded",
                    0x87: "unreachable"}
WAP_PUSH_PORT = 2948


def apply_mms_push(instance: str, sender: str, data: bytes, *, transport: str,
                   sent_ts: int | None = None, now: int | None = None) -> dict:
    """Consume one WAP Push payload addressed to the MMS user agent; see _apply_mms_push."""
    with _lock, _conn() as c:
        return _apply_mms_push(c, instance, sender, data, transport=transport,
                               sent_ts=sent_ts, now=now)


def _apply_mms_push(c, instance: str, sender: str, data: bytes, *, transport: str,
                    sent_ts: int | None = None, now: int | None = None) -> dict:
    """The one implementation of what a WAP Push does to the store, for live deliveries and
    for payloads filed before MMS existed.

    {"handled": False, "error"?} when the payload is not an MMS push; otherwise "kind" is
    "notification" (with "message_id", None when already held), "delivery" (the outgoing MMS
    it updated, if any) or "other" (a read report and the like, consumed without a trace).
    """
    from . import mms_pdu  # pure codec; imported here to keep store importable on its own
    try:
        push = mms_pdu.parse_wap_push(bytes(data))
    except mms_pdu.MmsDecodeError:
        return {"handled": False}
    if push.content_type != "application/vnd.wap.mms-message":
        return {"handled": False}
    try:
        pdu = mms_pdu.decode_pdu(push.body, now=sent_ts or now)
    except mms_pdu.MmsDecodeError as exc:
        return {"handled": False, "error": str(exc)}
    if pdu.message_type == mms_pdu.M_NOTIFICATION_IND:
        if not pdu.content_location:
            return {"handled": False, "error": "notification without a content location"}
        peer = _canonical_peer(c, instance, pdu.from_address or sender)
        mid = _ingest_mms_notification(
            c, instance, peer=peer, transport=transport, content_location=pdu.content_location,
            transaction_id=pdu.transaction_id, subject=pdu.subject, size=pdu.message_size,
            expiry_ts=pdu.expiry, sent_ts=sent_ts, to_addrs=pdu.to)
        return {"handled": True, "kind": "notification", "message_id": mid, "peer": peer,
                "size": pdu.message_size}
    if pdu.message_type == mms_pdu.M_DELIVERY_IND:
        mid = _record_mms_delivery(c, instance, pdu.message_id, pdu.to[0] if pdu.to else "",
                                   _MMS_PUSH_STATUS.get(pdu.status, "indeterminate"), pdu.date)
        return {"handled": True, "kind": "delivery", "message_id": mid}
    return {"handled": True, "kind": "other", "message_id": None}


def _convert_filed_mms_pushes(c) -> int:
    """Turn MMS pushes filed among the non-text payloads into what they are.

    Before MMS support every notification delivered over VoWiFi was filed in binary_sms, and
    until the TPDU fix a truncated body filed one even afterwards. The complete PDU is in
    tpdu_hex, so each such row is decoded again: a notification becomes (or matches) its MMS,
    a delivery report is applied, and the row is removed. Rows that are not MMS pushes, that
    lack a complete TPDU, or that are one part of a concatenated payload stay filed. Runs
    once as a schema step and on every start, since an older version files them again.
    """
    from . import mms_pdu
    converted = 0
    rows = c.execute("SELECT id,instance,peer,ts,transport,udh_hex,tpdu_hex FROM binary_sms "
                     "WHERE tpdu_hex<>'' AND concat_ref IS NULL ORDER BY id").fetchall()
    for row in rows:
        try:
            dest, _src = mms_pdu.extract_wdp_port(bytes.fromhex(row["udh_hex"] or ""))
        except ValueError:
            continue
        if dest != WAP_PUSH_PORT:
            continue
        payload = sms_pdu.deliver_user_data(row["tpdu_hex"])
        if not payload:
            continue
        sent_ts = sms_pdu.deliver_timestamp(row["tpdu_hex"]) or int(row["ts"] or 0) or None
        result = _apply_mms_push(c, str(row["instance"]), row["peer"], payload,
                                 transport=row["transport"] or "vowifi", sent_ts=sent_ts)
        if result.get("handled"):
            c.execute("DELETE FROM binary_sms WHERE id=?", (int(row["id"]),))
            converted += 1
    return converted


def _migration_filed_mms_pushes(c) -> None:
    _convert_filed_mms_pushes(c)


def reset_interrupted_mms(now: int | None = None) -> int:
    """After a restart: a download that was in flight is simply due again; a send that was
    in flight may or may not have reached the MMSC, so it is reported, never repeated."""
    now = int(now or time.time())
    with _lock, _conn() as c:
        count = c.execute("UPDATE mms SET state='notified', next_attempt_ts=?, updated_ts=? "
                          "WHERE state='downloading'", (now, now)).rowcount
        ids = [r[0] for r in c.execute("SELECT message_id FROM mms WHERE state='sending'")]
        if ids:
            marks = _placeholders(len(ids))
            c.execute(f"UPDATE mms SET state='failed', updated_ts=?, last_error=? "
                      f"WHERE message_id IN ({marks})",
                      (now, "Interrupted while sending; it may or may not have been sent.", *ids))
            c.execute(f"UPDATE messages SET status='unknown', error=? WHERE id IN ({marks})",
                      ("Interrupted while sending; it may or may not have been sent.", *ids))
    return count + len(ids)


# A download holds its MMS in "downloading" for one MMSC exchange -- minutes at most. One
# still there after this long was abandoned by a failure that could not even be recorded.
STUCK_DOWNLOAD_SECONDS = 600


def schedule_mms_download(instance: str, message_id: int, now: int | None = None) -> bool:
    """Queue an inbound MMS for retrieval now (a manual download or retry)."""
    now = int(now or time.time())
    with _lock, _conn() as c:
        # A manual request always reaches the MMSC: counting it as an attempt keeps an MMS
        # first seen already expired from being short-circuited again.
        cur = c.execute("UPDATE mms SET state='notified', next_attempt_ts=?, updated_ts=?, "
                        "attempts=MAX(attempts,1) "
                        "WHERE message_id=? AND instance=? AND direction='in' "
                        "AND (state IN ('notified','failed','expired') "
                        "OR (state='downloading' AND updated_ts<=?))",
                        (now, now, int(message_id), str(instance),
                         now - STUCK_DOWNLOAD_SECONDS))
    return cur.rowcount == 1


def release_stuck_mms_download(message_id: int, now: int | None = None,
                               delay: int = 60) -> bool:
    """Return an MMS left in "downloading" to the retry schedule."""
    now = int(now or time.time())
    with _lock, _conn() as c:
        cur = c.execute("UPDATE mms SET state='failed', next_attempt_ts=?, updated_ts=?, "
                        "last_error=CASE WHEN last_error='' THEN 'Download interrupted' "
                        "ELSE last_error END WHERE message_id=? AND state='downloading'",
                        (now + int(delay), now, int(message_id)))
    return cur.rowcount == 1


def due_mms_downloads(now: int | None = None, limit: int = 5) -> list[dict]:
    now = int(now or time.time())
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM mms WHERE direction='in' AND state IN ('notified','failed') "
            "AND next_attempt_ts IS NOT NULL AND next_attempt_ts<=? "
            "ORDER BY next_attempt_ts LIMIT ?", (now, int(limit))).fetchall()
    return [dict(r) for r in rows]


def set_mms_state(message_id: int, state: str, *, error: str | None = None,
                  next_attempt_ts: int | None | bool = False, attempts_increment: int = 0,
                  message_ref: str | None = None, message_status: str | None = None) -> None:
    """Move an MMS to `state`. `next_attempt_ts=None` clears the retry schedule; leaving it
    False keeps whatever is scheduled."""
    fields, args = ["state=?", "updated_ts=?", "attempts=attempts+?"], \
        [str(state), int(time.time()), int(attempts_increment)]
    if error is not None:
        fields.append("last_error=?")
        args.append(str(error)[:500])
    if next_attempt_ts is not False:
        fields.append("next_attempt_ts=?")
        args.append(next_attempt_ts)
    if message_ref is not None:
        fields.append("message_ref=?")
        args.append(str(message_ref))
    with _lock, _conn() as c:
        c.execute(f"UPDATE mms SET {','.join(fields)} WHERE message_id=?",
                  (*args, int(message_id)))
        if message_status is not None:
            c.execute("UPDATE messages SET status=?, error=? WHERE id=?",
                      (message_status, error if message_status == "failed" else None,
                       int(message_id)))


# Part files are written once under a fresh name and never rewritten, so a reader, a backup
# hard link or an interrupted save never sees a file change under it. One lock per message
# keeps its saves, its deletion and the orphan sweep from interleaving on its directory.
_mms_dir_locks: dict[int, threading.Lock] = {}
_mms_dir_locks_guard = threading.Lock()
# A file nothing refers to is only removed once it is this old: a save writes its files
# before it commits the rows that point at them.
MMS_ORPHAN_GRACE_SECONDS = 3600


def _mms_dir_lock(message_id: int) -> threading.Lock:
    with _mms_dir_locks_guard:
        return _mms_dir_locks.setdefault(int(message_id), threading.Lock())


def _remove_files(directory: str, names) -> None:
    for name in names:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def save_mms_content(message_id: int, parts: list[dict], *, subject: str | None = None,
                     body: str | None = None, from_addr: str | None = None,
                     to_addrs: list[str] | None = None, cc_addrs: list[str] | None = None,
                     size: int | None = None) -> None:
    """Replace an MMS's parts with `parts` ({content_type, data, name, content_id, charset,
    text}) and update its summary.

    New content goes to new files (mms_media.storage_name: sequence, random ID, an extension
    chosen by type -- never the sender's file name, which is kept as metadata). The rows are
    switched to them in one transaction, and only then are the files they replace removed; if
    anything fails first, the new files are removed and the previous content is untouched. A
    message deleted meanwhile keeps nothing of the save."""
    message_id = int(message_id)
    directory = _mms_message_dir(message_id)
    with _mms_dir_lock(message_id):
        written: list[str] = []
        try:
            os.makedirs(directory, exist_ok=True)
            rows = []
            for seq, part in enumerate(parts):
                data = bytes(part.get("data") or b"")
                content_type = str(part.get("content_type") or "application/octet-stream")
                filename = mms_media.storage_name(seq, content_type)
                temporary = os.path.join(directory, f".{filename}.tmp")
                written.append(f".{filename}.tmp")
                with open(temporary, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, os.path.join(directory, filename))
                written[-1] = filename
                name = str(part.get("name") or "")
                rows.append((message_id, seq, content_type,
                             mms_media.display_name(name, content_type) if name else "",
                             str(part.get("content_id") or ""), str(part.get("charset") or ""),
                             len(data), filename, part.get("text")))
            with _lock, _conn() as c:
                if c.execute("SELECT 1 FROM mms WHERE message_id=?",
                             (message_id,)).fetchone() is None:
                    raise LookupError(f"MMS {message_id} no longer exists")
                old = [r[0] for r in c.execute("SELECT path FROM mms_parts WHERE message_id=?",
                                               (message_id,))]
                c.execute("DELETE FROM mms_parts WHERE message_id=?", (message_id,))
                c.executemany("INSERT INTO mms_parts(message_id,seq,content_type,name,"
                              "content_id,charset,size,path,text) VALUES(?,?,?,?,?,?,?,?,?)",
                              rows)
                updates, args = ["updated_ts=?"], [int(time.time())]
                for column, value in (("subject", subject), ("from_addr", from_addr),
                                      ("size", size)):
                    if value is not None:
                        updates.append(f"{column}=?")
                        args.append(value)
                for column, value in (("to_addrs", to_addrs), ("cc_addrs", cc_addrs)):
                    if value is not None:
                        updates.append(f"{column}=?")
                        args.append(json.dumps(list(value)))
                c.execute(f"UPDATE mms SET {','.join(updates)} WHERE message_id=?",
                          (*args, message_id))
                if body is not None:
                    c.execute("UPDATE messages SET body=? WHERE id=?", (str(body), message_id))
        except BaseException:
            _remove_files(directory, written)
            try:
                os.rmdir(directory)   # only when the failed save left it empty
            except OSError:
                pass
            raise
        keep = set(written)
        _remove_files(directory, [n for n in old if n and n not in keep])


def sweep_mms_orphans(now: float | None = None,
                      grace: int = MMS_ORPHAN_GRACE_SECONDS) -> int:
    """Remove MMS files and directories nothing refers to -- left by a save or a deletion
    that was interrupted (a crash, a full disk) -- once they are older than `grace`. Returns
    how many files and directories were removed."""
    root = mms_dir()
    if not os.path.isdir(root):
        return 0
    now = time.time() if now is None else now
    with _lock, _conn() as c:
        live = {int(r[0]) for r in c.execute("SELECT message_id FROM mms")}
        referenced = {(int(r[0]), r[1]) for r in c.execute(
            "SELECT message_id, path FROM mms_parts")}
    removed = 0
    for entry in os.scandir(root):
        if not entry.name.isdigit() or not entry.is_dir(follow_symlinks=False):
            continue
        message_id = int(entry.name)
        with _mms_dir_lock(message_id):
            try:
                if message_id not in live:
                    if now - entry.stat(follow_symlinks=False).st_mtime > grace:
                        shutil.rmtree(entry.path, ignore_errors=True)
                        removed += 1
                    continue
                for item in os.scandir(entry.path):
                    if (message_id, item.name) in referenced:
                        continue
                    if item.is_file(follow_symlinks=False) and \
                            now - item.stat(follow_symlinks=False).st_mtime > grace:
                        os.remove(item.path)
                        removed += 1
            except OSError:
                continue
    return removed


def mms_part_file(instance: str, message_id: int, part_id: int) -> dict | None:
    """A stored part and the absolute path of its content, scoped to its line."""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT p.* FROM mms_parts p JOIN messages m ON m.id=p.message_id "
            "WHERE m.instance=? AND p.message_id=? AND p.id=?",
            (str(instance), int(message_id), int(part_id))).fetchone()
    if not row or not row["path"]:
        return None
    directory = os.path.realpath(_mms_message_dir(message_id))
    path = os.path.realpath(os.path.join(directory, row["path"]))
    if os.path.dirname(path) != directory or not os.path.isfile(path):
        return None
    return {**dict(row), "file": path}


def mms_parts_with_data(message_id: int) -> list[dict]:
    """Every stored part of an MMS with its content read back, in order."""
    with _lock, _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT p.*, m.instance FROM mms_parts p JOIN messages m ON m.id=p.message_id "
            "WHERE p.message_id=? ORDER BY p.seq", (int(message_id),))]
    parts = []
    for row in rows:
        found = mms_part_file(row["instance"], message_id, row["id"])
        if not found:
            raise OSError(f"MMS part {row['id']} is missing its content")
        with open(found["file"], "rb") as handle:
            parts.append({**row, "data": handle.read()})
    return parts


def create_outgoing_mms(instance: str, peer: str, *, to_addrs: list[str], subject: str,
                        body: str, transaction_id: str, transport: str = "") -> dict:
    now = int(time.time())
    with _lock, _conn() as c:
        rec = _insert_message(c, str(instance), "out", str(peer), str(body), status="pending",
                              transport=transport or "mms", ts=now, received_ts=now,
                              kind="mms", identity=f"mms-out:{transaction_id}")
        c.execute(
            "INSERT INTO mms(message_id,instance,direction,state,transaction_id,subject,"
            "to_addrs,transport,updated_ts) VALUES(?,?,?,?,?,?,?,?,?)",
            (rec["id"], str(instance), "out", "sending", str(transaction_id), str(subject or ""),
             json.dumps(list(to_addrs)), str(transport or ""), now))
    return rec


def record_mms_delivery(instance: str, message_ref: str, recipient: str, status: str,
                        ts: int | None = None) -> dict | None:
    """Apply one delivery report to the outgoing MMS it belongs to; None if none matches."""
    with _lock, _conn() as c:
        mid = _record_mms_delivery(c, instance, message_ref, recipient, status, ts)
    return get_message(mid) if mid else None


# X-Mms-Status values after which that recipient's outcome will not change.
_MMS_FINAL_DELIVERY = {"retrieved", "rejected", "unreachable", "expired", "unrecognised"}


def _mms_delivery_summary(recipients: list[str], delivery: dict) -> tuple[str, str | None]:
    """(message status, error) for an outgoing MMS from its per-recipient reports.

    One delivery report speaks for one recipient only. The message is "delivered" when every
    recipient retrieved it, "failed" once every recipient has a final outcome and at least one
    of them is not a retrieval (the error names who was not reached), and stays "sent" while
    any recipient's outcome is still open -- so a later rejection is never hidden by an
    earlier retrieval, whichever order the reports arrive in.
    """
    targets = [normalize_peer(r) for r in recipients if str(r or "").strip()]
    by_peer = {normalize_peer(k): v.get("status") for k, v in delivery.items()}
    if not targets:
        targets = list(by_peer) or [""]
    outcomes = {t: by_peer.get(t) for t in targets}
    if all(status == "retrieved" for status in outcomes.values()):
        return "delivered", None
    if all(status in _MMS_FINAL_DELIVERY for status in outcomes.values()):
        reached = sum(1 for status in outcomes.values() if status == "retrieved")
        missed = [f"{r}: {delivery_status}" for r, delivery_status in
                  ((r, by_peer.get(normalize_peer(r))) for r in recipients)
                  if delivery_status != "retrieved"]
        prefix = (f"Delivered to {reached} of {len(outcomes)} recipients; "
                  if len(outcomes) > 1 else "")
        return "failed", f"{prefix}MMS not delivered ({', '.join(missed)})"
    return "sent", None


def _record_mms_delivery(c, instance: str, message_ref: str, recipient: str, status: str,
                         ts: int | None = None) -> int | None:
    if not message_ref:
        return None
    row = c.execute("SELECT message_id,delivery,to_addrs FROM mms WHERE instance=? "
                    "AND direction='out' AND message_ref=? ORDER BY message_id DESC LIMIT 1",
                    (str(instance), str(message_ref))).fetchone()
    if not row:
        return None
    try:
        delivery = json.loads(row["delivery"] or "{}")
    except ValueError:
        delivery = {}
    try:
        recipients = [str(r) for r in json.loads(row["to_addrs"] or "[]")]
    except ValueError:
        recipients = []
    key = str(recipient or "")
    if not key and len(recipients) == 1:
        key = recipients[0]              # a report naming no recipient is about the only one
    for known in recipients:
        if normalize_peer(known) == normalize_peer(key):
            key = known                  # keep one entry per recipient, however it is spelled
            break
    delivery[key] = {"status": str(status), "ts": int(ts or time.time())}
    message_status, error = _mms_delivery_summary(recipients, delivery)
    state = {"delivered": "delivered", "failed": "failed"}.get(message_status, "sent")
    c.execute("UPDATE mms SET delivery=?, state=?, updated_ts=? WHERE message_id=?",
              (json.dumps(delivery), state, int(time.time()), int(row["message_id"])))
    c.execute("UPDATE messages SET status=?, error=? WHERE id=?",
              (message_status, error, int(row["message_id"])))
    return int(row["message_id"])


def list_threads(instance: str) -> list:
    with _lock, _conn() as c:
        rows = c.execute(
            """SELECT peer, MAX(ts) AS last_ts,
                      (SELECT body FROM messages m2 WHERE m2.instance=m.instance AND m2.peer=m.peer
                       ORDER BY ts DESC LIMIT 1) AS last_body,
                      (SELECT kind FROM messages m2 WHERE m2.instance=m.instance AND m2.peer=m.peer
                       ORDER BY ts DESC LIMIT 1) AS last_kind,
                      COUNT(*) AS n
               FROM messages m WHERE instance=? GROUP BY peer ORDER BY last_ts DESC""",
            (str(instance),),
        ).fetchall()
    return [dict(r) for r in rows]


def list_messages(instance: str, peer: str, limit: int = 200) -> list:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE instance=? AND peer=? ORDER BY ts ASC LIMIT ?",
            (str(instance), peer, limit),
        ).fetchall()
        return _with_mms(c, [dict(r) for r in rows])


def recent_messages(instance: str, limit: int = 10) -> list:
    """The newest messages of a line across every peer, newest first. The WebUI reads one
    conversation at a time; a chat/bot view wants the tail of the whole line."""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE instance=? ORDER BY ts DESC, id DESC LIMIT ?",
            (str(instance), max(1, int(limit))),
        ).fetchall()
        return _with_mms(c, [dict(r) for r in rows])


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _delete_where(where: str, args: tuple) -> int:
    """Delete messages and everything an MMS among them owns: its state, parts and files."""
    with _lock, _conn() as c:
        ids = [int(r[0]) for r in c.execute(f"SELECT id FROM messages WHERE {where}", args)]
        if not ids:
            return 0
        marks = _placeholders(len(ids))
        c.execute(f"DELETE FROM mms_parts WHERE message_id IN ({marks})", ids)
        c.execute(f"DELETE FROM mms WHERE message_id IN ({marks})", ids)
        removed = c.execute(f"DELETE FROM messages WHERE id IN ({marks})", ids).rowcount
    for mid in ids:
        with _mms_dir_lock(mid):
            shutil.rmtree(_mms_message_dir(mid), ignore_errors=True)
    return removed


def delete_messages(instance: str, ids: list[int]) -> int:
    """Delete specific messages of this instance by id. Returns the number removed."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    return _delete_where(f"instance=? AND id IN ({_placeholders(len(ids))})",
                         (str(instance), *ids))


def delete_thread(instance: str, peer: str) -> int:
    """Delete every message in one conversation (instance + peer). Returns rows removed."""
    return _delete_where("instance=? AND peer=?", (str(instance), peer))


def clear_messages(instance: str) -> int:
    """Delete ALL messages for this instance. Returns rows removed."""
    return _delete_where("instance=?", (str(instance),))


def add_call(instance: str, direction: str, peer: str, status: str = "ringing",
             transport: str = "vowifi") -> dict:
    ts = int(time.time())
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO calls(instance,direction,peer,status,start_ts,transport) VALUES(?,?,?,?,?,?)",
            (str(instance), direction, peer, status, ts, str(transport)),
        )
        cid = cur.lastrowid
    return {"id": cid, "instance": str(instance), "direction": direction,
            "peer": peer, "status": status, "start_ts": ts, "transport": str(transport)}


def get_open_call_for_transport(instance: str, transport: str) -> dict | None:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM calls WHERE instance=? AND direction='out' AND transport=? "
            "AND end_ts IS NULL ORDER BY start_ts DESC,id DESC LIMIT 1",
            (str(instance), str(transport))).fetchone()
    return dict(row) if row else None


def get_open_call(instance: str, direction: str, within_s: int | None = None) -> dict | None:
    """The most recent still-open (not yet finalized) call for (instance, direction), or None.

    A softphone handles one call at a time, so an inbound INVITE that the IMS delivers more
    than once (VoLTE preconditions / GRUU fork / retransmit) fires `call_in` several times
    for the SAME call. Reusing the open record instead of inserting a new one keeps one row
    per call — otherwise every extra `call_in` leaves a ghost 'ringing' entry that the single
    `call_result` never finalizes. `within_s` bounds how old the open record may be so a
    genuinely new call (after a stale unfinalized one) still starts fresh."""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM calls WHERE instance=? AND direction=? AND end_ts IS NULL "
            "ORDER BY start_ts DESC LIMIT 1", (str(instance), direction)).fetchone()
        if not row:
            return None
        if within_s is not None and int(time.time()) - row["start_ts"] > within_s:
            return None
        return dict(row)


def get_open_call(instance: str, direction: str, within_s: int | None = None) -> dict | None:
    """The most recent still-open (not yet finalized, end_ts IS NULL) call for (instance,
    direction), or None. `within_s` bounds how old the open record may be so a genuinely new
    call after a stale unfinalized one still starts fresh."""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM calls WHERE instance=? AND direction=? AND end_ts IS NULL "
            "ORDER BY start_ts DESC LIMIT 1", (str(instance), direction)).fetchone()
        if not row:
            return None
        if within_s is not None and int(time.time()) - row["start_ts"] > within_s:
            return None
        return dict(row)


def add_call_deduped(instance: str, direction: str, peer: str, status: str = "ringing",
                     open_within_s: int = 90) -> dict:
    """Insert an inbound-call record, coalescing concurrent duplicate `call_in` events for the
    SAME still-ringing call into ONE record.

    The IMS can deliver a call_in more than once while the call is still being set up (VoLTE
    preconditions / GRUU fork): those extra events all arrive BEFORE the call_result, so the
    record is still open (end_ts IS NULL) and is simply reused — no ghost row, and no reliance
    on any time heuristic that could swallow a genuine call-back.

    (The other historical source of duplicates — the dialplan 'h' hangup handler falling
    through to the broad `_.` pattern and firing a SECOND call_in AFTER finalization — is fixed
    at the source in extensions.conf.j2 with `h => …,Return()`, so no post-finalize dedupe is
    needed here.)

    An anonymous first call_in ('') whose number arrives on a later duplicate is filled in."""
    open_rec = get_open_call(instance, direction, within_s=open_within_s)
    # Only coalesce into an open record with a compatible peer: an anonymous dup ('') matches
    # anything; a numbered dup must match (or fill) the open record's peer. A different number
    # is a distinct call and starts its own record.
    if open_rec:
        rp = open_rec.get("peer") or ""
        if not peer or not rp or peer == rp:
            if peer and not rp:
                with _lock, _conn() as c:
                    c.execute("UPDATE calls SET peer=? WHERE id=?", (peer, open_rec["id"]))
                open_rec["peer"] = peer
            return open_rec
    return add_call(instance, direction, peer, status)


def update_call(cid: int, status: str, ended: bool = False):
    with _lock, _conn() as c:
        if ended:
            c.execute("UPDATE calls SET status=?, end_ts=? WHERE id=?",
                      (status, int(time.time()), cid))
        else:
            c.execute("UPDATE calls SET status=? WHERE id=?", (status, cid))


def set_call_ussd(call_id: int, text: str) -> None:
    """Attach a carrier's USSD reply to an existing call record."""
    with _lock, _conn() as c:
        c.execute("UPDATE calls SET ussd_text=? WHERE id=?", (str(text or ""), int(call_id)))


def set_ussd_for_peer(instance: str, peer: str, text: str) -> dict | None:
    """Attach a USSD reply to the most recent outgoing call to this peer.

    The reply is reported by a hangup handler on the carrier's leg, which races the 'h'
    handler on the caller's leg that finalizes the record — so the record may be either still
    open or already closed when this lands. Matching the most recent call to that peer covers
    both, and a service code is not something a user dials twice in the same second.
    """
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT id FROM calls WHERE instance=? AND direction='out' AND peer=? "
            "ORDER BY start_ts DESC LIMIT 1", (str(instance), str(peer))).fetchone()
        if not row:
            return None
        c.execute("UPDATE calls SET ussd_text=? WHERE id=?", (str(text or ""), row["id"]))
        r = c.execute("SELECT * FROM calls WHERE id=?", (row["id"],)).fetchone()
        return dict(r) if r else None


def update_last_call(instance: str, direction: str, peer: str | None, status: str) -> dict | None:
    """Finalize the most recent still-open call for (instance, direction[, peer]).

    peer may be None/empty: Asterisk's 'h' hangup handler loses pre-Dial channel variables
    (incl. the dialled number) when the caller hangs up mid-Dial and the channel is
    masqueraded, so the disposition callback can arrive with no peer. Since a softphone
    handles one call at a time, finalizing the most-recent OPEN call of that direction is
    unambiguous and correct in that case."""
    with _lock, _conn() as c:
        if peer:
            row = c.execute(
                "SELECT id FROM calls WHERE instance=? AND direction=? AND peer=? AND end_ts IS NULL "
                "ORDER BY start_ts DESC LIMIT 1", (str(instance), direction, peer)).fetchone()
        else:
            row = c.execute(
                "SELECT id FROM calls WHERE instance=? AND direction=? AND end_ts IS NULL "
                "ORDER BY start_ts DESC LIMIT 1", (str(instance), direction)).fetchone()
        if not row:
            return None
        c.execute("UPDATE calls SET status=?, end_ts=? WHERE id=?",
                  (status, int(time.time()), row["id"]))
        r = c.execute("SELECT * FROM calls WHERE id=?", (row["id"],)).fetchone()
        return dict(r) if r else None



def list_calls(instance: str, limit: int = 100) -> list:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM calls WHERE instance=? ORDER BY start_ts DESC LIMIT ?",
            (str(instance), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_calls(instance: str, ids: list[int]) -> int:
    """Delete specific call-log entries of this instance by id. Returns rows removed."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    with _lock, _conn() as c:
        cur = c.execute(
            f"DELETE FROM calls WHERE instance=? AND id IN ({_placeholders(len(ids))})",
            (str(instance), *ids),
        )
        return cur.rowcount


def clear_calls(instance: str) -> int:
    """Delete the ENTIRE call log for this instance. Returns rows removed."""
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM calls WHERE instance=?", (str(instance),))
        return cur.rowcount


def record_line_state(instance: str, state: str, ts: int | None = None,
                      reason: str = "", detail: str = "") -> None:
    """Append one connectivity observation for a line to its timeline.

    A sample that continues the current state only moves the segment's end. A different
    state starts a new segment where the previous one ended, so an uninterrupted record has
    no artificial holes; a silence longer than the continuity window deliberately leaves
    one, because the control plane cannot claim to know what happened while it was down.

    ``reason`` is the status reason_code that came with the sample, ``detail`` the evidence
    behind it (the name that failed to resolve, the resolvers it was tried against…). A
    segment keeps the first non-empty reason it saw — an outage's cause is what broke it,
    not the "registering…" it passes through on the way back up — and detail travels with
    the reason it belongs to.
    """
    state = state if state in LINE_STATES else "down"
    ts = int(ts if ts is not None else time.time())
    reason, detail = str(reason or ""), str(detail or "")
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT id, state, end_ts, reason FROM line_states WHERE instance=? "
            "ORDER BY end_ts DESC, id DESC LIMIT 1", (str(instance),)).fetchone()
        if row and ts < row["end_ts"]:
            # A clock stepping backwards (NTP sync after boot) must never produce a segment
            # that ends before it starts.
            ts = int(row["end_ts"])
        continuous = bool(row) and ts - int(row["end_ts"]) <= LINE_STATE_CONTINUITY_SECONDS
        if continuous and row["state"] == state:
            # The first symptom can be above the actual fault: an IMS registration may become
            # Rejected seconds before swu_ike proves that the ePDG ignored every CHILD_SA rekey.
            # Replace only with a strictly stronger causal observation; recovery's generic
            # "registering" state must never erase the fault that began the outage.
            cause_priority = {
                "": 0, "registering": 5, "tunnel_setup": 10, "reg_rejected": 20,
                "reg_unanswered": 30, "tunnel_network": 35,
                "tunnel_child_rekey_timeout": 50, "tunnel_ike_rekey_timeout": 50,
                "tunnel_rekey_send_error": 50, "tunnel_sim_auth": 50,
                "tunnel_not_authorized": 50, "tunnel_proposal": 50,
                "reg_reauth_failed": 50, "maintenance_rebuild": 60,
                "client_engine_failure": 60,
            }
            stronger = (reason and cause_priority.get(reason, 25)
                        > cause_priority.get(str(row["reason"] or ""), 25))
            if reason and (not row["reason"] or stronger):
                c.execute("UPDATE line_states SET end_ts=?, reason=?, detail=? WHERE id=?",
                          (ts, reason, detail, row["id"]))
            else:
                c.execute("UPDATE line_states SET end_ts=? WHERE id=?", (ts, row["id"]))
            return
        start = int(row["end_ts"]) if continuous else ts
        c.execute("INSERT INTO line_states(instance,state,start_ts,end_ts,reason,detail) "
                  "VALUES(?,?,?,?,?,?)", (str(instance), state, start, ts, reason, detail))


def line_states(instance: str, since_ts: int) -> list[dict]:
    """Recorded segments of one line that overlap [since_ts, now], oldest first."""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT state, start_ts, end_ts, reason, detail FROM line_states "
            "WHERE instance=? AND end_ts>=? "
            "ORDER BY start_ts ASC, id ASC", (str(instance), int(since_ts))).fetchall()
    return [dict(r) for r in rows]


def line_state_timeline(instance: str, start_ts: int, end_ts: int) -> list[dict]:
    """Gap-aware timeline for a window: recorded segments clipped to it, every hole in the
    record reported as `unknown`.

    A hole shorter than the continuity window is sampling jitter and is absorbed by the
    neighbouring segment. A longer one is left as `unknown` on purpose: the control plane
    was not watching, so drawing that period as either healthy or failed would be a claim
    it cannot make.
    """
    start, end = int(start_ts), int(end_ts)
    result: list[dict] = []
    cursor = start
    for row in line_states(instance, start):
        segment_start = max(int(row["start_ts"]), start)
        segment_end = min(int(row["end_ts"]), end)
        # A segment holding a single sample is still zero-length; keep it so the newest
        # state appears immediately instead of after the second sample of that state.
        if segment_end < segment_start:
            continue
        hole = segment_start - cursor
        if hole > 0:
            if result and hole <= LINE_STATE_CONTINUITY_SECONDS:
                result[-1]["end"] = segment_start
            else:
                result.append({"state": "unknown", "start": cursor, "end": segment_start})
        if result and result[-1]["state"] == row["state"] and result[-1]["end"] >= segment_start:
            result[-1]["end"] = max(result[-1]["end"], segment_end)
            if not result[-1].get("reason"):
                # Reason and detail describe the same sample; they move as a pair.
                result[-1]["reason"] = str(row.get("reason") or "")
                result[-1]["detail"] = str(row.get("detail") or "")
        else:
            result.append({"state": str(row["state"]), "start": segment_start,
                           "end": segment_end, "reason": str(row.get("reason") or ""),
                           "detail": str(row.get("detail") or "")})
        cursor = result[-1]["end"]
    if end > cursor:
        if result and end - cursor <= LINE_STATE_CONTINUITY_SECONDS:
            result[-1]["end"] = end
        else:
            result.append({"state": "unknown", "start": cursor, "end": end})
    return result


def line_state_summary(segments: list[dict]) -> dict:
    """Totals for a timeline. Availability counts observed time only, never assumed time."""
    totals = {"up": 0, "down": 0, "off": 0, "unknown": 0}
    outages, longest = 0, 0
    for segment in segments:
        length = max(0, int(segment["end"]) - int(segment["start"]))
        totals[segment["state"]] = totals.get(segment["state"], 0) + length
        if segment["state"] == "down":
            outages += 1
            longest = max(longest, length)
    observed = totals["up"] + totals["down"]
    return {**totals, "observed_seconds": observed, "outages": outages,
            "longest_outage_seconds": longest,
            "uptime_ratio": (totals["up"] / observed) if observed else None}


def line_state_recorded_since(instance: str) -> int | None:
    """Oldest retained observation for this line, or None when nothing was ever recorded."""
    with _lock, _conn() as c:
        row = c.execute("SELECT MIN(start_ts) AS first_ts FROM line_states WHERE instance=?",
                        (str(instance),)).fetchone()
    return int(row["first_ts"]) if row and row["first_ts"] is not None else None


def prune_line_states(before_ts: int) -> int:
    """Drop history that has aged past retention. Returns rows removed."""
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM line_states WHERE end_ts < ?", (int(before_ts),))
        return cur.rowcount


def clear_line_states(instance: str) -> int:
    """Delete the connectivity timeline of one line. Returns rows removed."""
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM line_states WHERE instance=?", (str(instance),))
        return cur.rowcount


# ----------------------------- address book -----------------------------
# Where a contact's numbers are found by key: match_key on every line, and the per-country keys
# only on a line in that country (contacts.number_keys). `region` is the arriving line's.
_CONTACT_KEY_ROWS = (
    "SELECT contact_numbers.contact_id AS contact_id, contact_numbers.match_key AS key "
    "FROM contact_numbers WHERE contact_numbers.match_key IN ({keys}) "
    "UNION ALL "
    "SELECT contact_numbers.contact_id, contact_number_keys.match_key "
    "FROM contact_number_keys "
    "JOIN contact_numbers ON contact_numbers.id = contact_number_keys.number_id "
    "WHERE contact_number_keys.region {region} AND contact_number_keys.match_key IN ({keys})")


def _contact_numbers(c, ids) -> dict[int, list[dict]]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = c.execute(f"SELECT contact_id,label,number FROM contact_numbers "
                     f"WHERE contact_id IN ({placeholders}) ORDER BY id", tuple(ids)).fetchall()
    out: dict[int, list[dict]] = {}
    for row in rows:
        out.setdefault(int(row["contact_id"]), []).append(
            {"label": row["label"] or "", "number": row["number"]})
    return out


def _contact_docs(c, rows) -> list[dict]:
    numbers = _contact_numbers(c, [int(r["id"]) for r in rows])
    return [{"id": int(r["id"]), "name": r["name"], "company": r["company"] or "",
             "note": r["note"] or "", "updated_ts": int(r["updated_ts"] or 0),
             "numbers": numbers.get(int(r["id"]), [])} for r in rows]


def _contacts_by_key(c, owner: int, keys, region: str | None, extra: str = "",
                     limit: int | None = None) -> list:
    """The owner's contacts holding one of `keys`, by name then id.

    `region` is the arriving line's country: its per-country keys are searched and no other
    country's. None searches every country's -- right for the owner's own search and for telling
    whether an import is somebody already in the book, never for naming a caller.
    """
    keys = sorted({k for k in keys if k})
    if not keys:
        return []
    placeholders = ",".join("?" for _ in keys)
    region_clause, region_args = ("IS NOT NULL", ()) if region is None else ("= ?", (region,))
    union = _CONTACT_KEY_ROWS.format(keys=placeholders, region=region_clause)
    sql = (f"SELECT contacts.*, matched.key AS matched_key FROM ({union}) AS matched "
           f"JOIN contacts ON contacts.id = matched.contact_id "
           f"WHERE contacts.owner=? {extra} ORDER BY contacts.name COLLATE NOCASE, contacts.id")
    args = (*keys, *region_args, *keys, int(owner))
    if limit is not None:
        sql += " LIMIT ?"
        args += (int(limit),)
    return c.execute(sql, args).fetchall()


def contacts_list(owner: int, query: str = "", limit: int = 1000, regions=()) -> list[dict]:
    """The owner's contacts, optionally narrowed by name, company or number.

    `regions` lets a search typed in national form -- "07700 900123" -- reach a contact stored
    as +447700900123, in whichever of the gateway's countries it is a number.
    """
    text = str(query or "").strip()
    with _lock, _conn() as c:
        if not text:
            rows = c.execute("SELECT * FROM contacts WHERE owner=? ORDER BY name COLLATE NOCASE, id "
                             "LIMIT ?", (int(owner), int(limit))).fetchall()
            return _contact_docs(c, rows)
        like = f"%{text}%"
        digits = contacts_format.digits_of(text)
        rows = c.execute(
            "SELECT DISTINCT contacts.* FROM contacts "
            "LEFT JOIN contact_numbers ON contact_numbers.contact_id = contacts.id "
            "WHERE contacts.owner=? AND (contacts.name LIKE ? OR contacts.company LIKE ? "
            "   OR contact_numbers.number LIKE ? OR (?<>'' AND contact_numbers.match_key LIKE ?)) "
            "ORDER BY contacts.name COLLATE NOCASE, contacts.id LIMIT ?",
            (int(owner), like, like, like, digits, f"%{digits}", int(limit))).fetchall()
        # A search that looks like a number is also tried the way an arriving number is, so a
        # national spelling finds an international one; a fragment such as "900123" is found by
        # the suffix match above.
        if digits:
            rows = list(rows)
            seen = {int(r["id"]) for r in rows}
            for row in _contacts_by_key(c, owner, contacts_format.number_keys(text, regions)
                                        .values(), None):
                if int(row["id"]) not in seen:
                    seen.add(int(row["id"]))
                    rows.append(row)
            rows.sort(key=lambda r: (str(r["name"]).casefold(), int(r["id"])))
            rows = rows[:int(limit)]
        return _contact_docs(c, rows)


def contact_get(owner: int, contact_id: int) -> dict | None:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM contacts WHERE owner=? AND id=?",
                         (int(owner), int(contact_id))).fetchall()
        docs = _contact_docs(c, rows)
        return docs[0] if docs else None


def contacts_count(owner: int) -> int:
    with _lock, _conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM contacts WHERE owner=?",
                             (int(owner),)).fetchone()[0])


def _drop_numbers(c, contact_ids) -> None:
    ids = [int(i) for i in contact_ids]
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        placeholders = ",".join("?" for _ in chunk)
        c.execute(f"DELETE FROM contact_number_keys WHERE number_id IN "
                  f"(SELECT id FROM contact_numbers WHERE contact_id IN ({placeholders}))",
                  tuple(chunk))
        c.execute(f"DELETE FROM contact_numbers WHERE contact_id IN ({placeholders})",
                  tuple(chunk))


def _add_country_keys(c, number_id: int, keys: dict[str, str]) -> None:
    c.executemany("INSERT INTO contact_number_keys(number_id,region,match_key) VALUES(?,?,?)",
                  [(int(number_id), region, key) for region, key in keys.items() if region])


def _add_numbers(c, contact_id: int, numbers, regions) -> None:
    for item in numbers:
        keys = contacts_format.number_keys(item["number"], regions)
        cur = c.execute("INSERT INTO contact_numbers(contact_id,label,number,match_key) "
                        "VALUES(?,?,?,?)",
                        (int(contact_id), item.get("label") or "", item["number"], keys[""]))
        _add_country_keys(c, int(cur.lastrowid), keys)


def _write_numbers(c, contact_id: int, numbers, regions) -> None:
    _drop_numbers(c, [contact_id])
    _add_numbers(c, contact_id, numbers, regions)


def contact_create(owner: int, contact: dict, regions=()) -> dict:
    clean = contacts_format.normalize_contact(contact, regions)
    now = int(time.time())
    with _lock, _conn() as c:
        count = int(c.execute("SELECT COUNT(*) FROM contacts WHERE owner=?",
                              (int(owner),)).fetchone()[0])
        if count >= contacts_format.MAX_CONTACTS_PER_OWNER:
            raise contacts_format.ContactError("this address book is full")
        cur = c.execute("INSERT INTO contacts(owner,name,company,note,created_ts,updated_ts) "
                        "VALUES(?,?,?,?,?,?)",
                        (int(owner), clean["name"], clean["company"], clean["note"], now, now))
        contact_id = int(cur.lastrowid)
        _write_numbers(c, contact_id, clean["numbers"], regions)
        return {"id": contact_id, "updated_ts": now, **clean}


def contact_update(owner: int, contact_id: int, contact: dict, regions=()) -> dict | None:
    clean = contacts_format.normalize_contact(contact, regions)
    now = int(time.time())
    with _lock, _conn() as c:
        exists = c.execute("SELECT id FROM contacts WHERE owner=? AND id=?",
                           (int(owner), int(contact_id))).fetchone()
        if not exists:
            return None
        c.execute("UPDATE contacts SET name=?,company=?,note=?,updated_ts=? WHERE id=?",
                  (clean["name"], clean["company"], clean["note"], now, int(contact_id)))
        _write_numbers(c, int(contact_id), clean["numbers"], regions)
        return {"id": int(contact_id), "updated_ts": now, **clean}


def contact_delete(owner: int, contact_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM contacts WHERE owner=? AND id=?",
                        (int(owner), int(contact_id)))
        if cur.rowcount:
            _drop_numbers(c, [int(contact_id)])
        return bool(cur.rowcount)


def contacts_resolve(owner: int, numbers, region: str = "") -> dict[str, dict]:
    """Map each number as the caller wrote it to the contact it belongs to, if any.

    Answering in the caller's own spelling is what lets a conversation list built from message
    rows use the result without normalising anything itself. `region` is the country of the
    line the numbers arrived on: a number written in national form is national to that country,
    and a contact typed in national form is found through that country's key and no other's.
    """
    region = str(region or "").lower()
    wanted: dict[str, list[str]] = {}
    for number in numbers or []:
        key = contacts_format.number_key(str(number), region)
        if key:
            wanted.setdefault(key, []).append(str(number))
    if not wanted:
        return {}
    out: dict[str, dict] = {}
    with _lock, _conn() as c:
        keys = list(wanted)
        for start in range(0, len(keys), 400):
            for row in _contacts_by_key(c, owner, keys[start:start + 400], region):
                for original in wanted.get(str(row["matched_key"]), []):
                    out.setdefault(original, {"id": int(row["id"]), "name": row["name"]})
    return out


def contacts_rekey(regions=()) -> int:
    """Rebuild every stored key for `regions`, the countries the gateway has lines in.

    A number typed in national form has a key for each of them, and the set changes as lines are
    added and removed and as a SIM reports where it is. The number as typed is kept, so the keys
    can always be worked out again. Returns how many numbers' keys changed.
    """
    regions = contacts_format.as_regions(regions)
    with _lock, _conn() as c:
        stored: dict[int, dict[str, str]] = {}
        for row in c.execute("SELECT id, match_key FROM contact_numbers").fetchall():
            stored[int(row["id"])] = {"": row["match_key"]}
        for row in c.execute("SELECT number_id, region, match_key FROM contact_number_keys"):
            stored.setdefault(int(row["number_id"]), {})[row["region"]] = row["match_key"]
        changed = 0
        for row in c.execute("SELECT id, number FROM contact_numbers").fetchall():
            number_id = int(row["id"])
            keys = contacts_format.number_keys(row["number"], regions)
            if keys == stored.get(number_id):
                continue
            changed += 1
            c.execute("UPDATE contact_numbers SET match_key=? WHERE id=?", (keys[""], number_id))
            c.execute("DELETE FROM contact_number_keys WHERE number_id=?", (number_id,))
            _add_country_keys(c, number_id, keys)
    return changed


def _all_keys(numbers, regions) -> set[str]:
    return {key for item in numbers
            for key in contacts_format.number_keys(item["number"], regions).values()}


def _name_is_a_number(name: str, keys: set[str], regions) -> bool:
    """Whether `name` is only one of the contact's own numbers standing in for a name.

    That is what a card without a readable name is stored under, and a later import that does
    carry a name should be able to give it one.
    """
    if re.search(r"[^\d\s()+\-.]", str(name or "")):
        return False
    return bool(set(contacts_format.number_keys(name, regions).values()) & keys)


def contacts_import(owner: int, incoming, regions=()) -> dict:
    """Add contacts, folding one into an existing entry only when it is the same person.

    A re-import of the same export must not double the address book, so an incoming contact
    that shares a number with an entry of the same name is folded into it, gaining any number it
    did not have. Sharing a number is not enough on its own: two people on one home landline are
    two contacts, as they are on a phone.

    An entry named after its own number -- a card whose name could not be read -- is the same
    person as a named one sharing its number, and takes the name. So is an incoming card without
    a name, which is folded into the entry it shares a number with.
    """
    added = merged = skipped = 0
    now = int(time.time())
    with _lock, _conn() as c:
        count = int(c.execute("SELECT COUNT(*) FROM contacts WHERE owner=?",
                              (int(owner),)).fetchone()[0])
        for contact in incoming:
            clean = contacts_format.normalize_contact(contact, regions)
            keys = _all_keys(clean["numbers"], regions)
            candidates, seen = [], set()
            for row in _contacts_by_key(c, owner, keys, None):
                if int(row["id"]) not in seen:
                    seen.add(int(row["id"]))
                    candidates.append(row)
            numbers = _contact_numbers(c, list(seen))
            have = {contact_id: _all_keys(numbers.get(contact_id, []), regions)
                    for contact_id in seen}
            unnamed = _name_is_a_number(clean["name"], keys, regions)
            target = rename = None
            for row in candidates:
                if row["name"].casefold() == clean["name"].casefold() or unnamed:
                    target = row
                    break
            if target is None and not unnamed:
                target = next((row for row in candidates
                               if _name_is_a_number(row["name"], have[int(row["id"])], regions)),
                              None)
                rename = target
            if target is not None:
                existing = int(target["id"])
                fresh = [item for item in clean["numbers"]
                         if not _all_keys([item], regions) & have[existing]]
                if not fresh and rename is None:
                    skipped += 1
                    continue
                _add_numbers(c, existing, fresh, regions)
                if rename is not None:
                    c.execute("UPDATE contacts SET name=? WHERE id=?", (clean["name"], existing))
                c.execute("UPDATE contacts SET updated_ts=? WHERE id=?", (now, existing))
                merged += 1
                continue
            if count >= contacts_format.MAX_CONTACTS_PER_OWNER:
                raise contacts_format.ContactError("this address book is full")
            cur = c.execute("INSERT INTO contacts(owner,name,company,note,created_ts,updated_ts) "
                            "VALUES(?,?,?,?,?,?)",
                            (int(owner), clean["name"], clean["company"], clean["note"], now, now))
            _write_numbers(c, int(cur.lastrowid), clean["numbers"], regions)
            count += 1
            added += 1
    return {"added": added, "merged": merged, "skipped": skipped}
