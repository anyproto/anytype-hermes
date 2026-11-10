"""Transactional input checkpoints and per-operation delivery payloads.

A payload changes only once: to its plain-text fallback, before that is sent.
"""

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path


class Store:
    def __init__(self, path, namespace):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.ns = hashlib.sha256(namespace.encode()).hexdigest()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS chats (
                ns TEXT, target TEXT, cursor TEXT, latest_id TEXT, health TEXT,
                PRIMARY KEY(ns,target));
            CREATE TABLE IF NOT EXISTS inputs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ns TEXT, target TEXT, message_id TEXT, payload TEXT,
                status TEXT, updated REAL, UNIQUE(ns,target,message_id));
            CREATE TABLE IF NOT EXISTS deliveries (
                ns TEXT, operation TEXT, target TEXT, content_hash TEXT,
                chunk INTEGER, payload TEXT, request_key TEXT, status TEXT,
                remote_id TEXT, updated REAL, PRIMARY KEY(ns,operation,chunk));
            CREATE TABLE IF NOT EXISTS catalog (
                ns TEXT, target TEXT, space_id TEXT, chat_id TEXT, name TEXT,
                kind TEXT, parent_id TEXT, available INTEGER, reasons TEXT,
                baseline_needed INTEGER, PRIMARY KEY(ns,target));
            CREATE TABLE IF NOT EXISTS owned_objects (
                ns TEXT, space_id TEXT, object_id TEXT, discussion TEXT,
                PRIMARY KEY(ns,space_id,object_id));
            CREATE TABLE IF NOT EXISTS discovery_state (
                ns TEXT, space_id TEXT, initialized INTEGER,
                PRIMARY KEY(ns,space_id));
            CREATE TABLE IF NOT EXISTS observations (
                ns TEXT, target TEXT, message_id TEXT, confirmed INTEGER,
                PRIMARY KEY(ns,target,message_id));
            CREATE TABLE IF NOT EXISTS recovery_coverage (
                ns TEXT, target TEXT, incomplete INTEGER, PRIMARY KEY(ns,target));
            CREATE TABLE IF NOT EXISTS watch_boundaries (
                ns TEXT, target TEXT, cursor TEXT, PRIMARY KEY(ns,target));
            CREATE TABLE IF NOT EXISTS bootstrap_boundaries (
                ns TEXT, space_id TEXT, cursor TEXT, PRIMARY KEY(ns,space_id));
            CREATE TABLE IF NOT EXISTS baseline_candidates (
                ns TEXT, target TEXT, message_id TEXT, payload TEXT,
                PRIMARY KEY(ns,target,message_id));
            CREATE TABLE IF NOT EXISTS discovery_spaces (
                ns TEXT, space_id TEXT, health TEXT, PRIMARY KEY(ns,space_id));
            CREATE TABLE IF NOT EXISTS discovery_epochs (
                ns TEXT PRIMARY KEY, since REAL);
        ''')

    def close(self):
        self.db.close()

    def catalog(self, space=None):
        return [dict(r, reasons=json.loads(r['reasons'])) for r in self.db.execute(
            'SELECT * FROM catalog WHERE ns=?' + (' AND space_id=?' if space else ''),
            (self.ns, space) if space else (self.ns,))]

    def set_discovery_spaces(self, spaces):
        with self.db:
            self.db.execute('DELETE FROM discovery_spaces WHERE ns=?', (self.ns,))
            self.db.executemany('INSERT INTO discovery_spaces VALUES (?,?,?)',
                                [(self.ns, space, 'discovering') for space in spaces])

    def discovery_spaces(self):
        return dict(self.db.execute('SELECT space_id,health FROM discovery_spaces WHERE ns=?', (self.ns,)))

    def space_health(self, space, health):
        with self.db:
            self.db.execute('UPDATE discovery_spaces SET health=? WHERE ns=? AND space_id=?',
                            (health, self.ns, space))

    def discovery_epoch(self, since):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO discovery_epochs VALUES (?,?)', (self.ns, since))
        return self.db.execute('SELECT since FROM discovery_epochs WHERE ns=?', (self.ns,)).fetchone()[0]

    def discovered(self, target):
        row = self.db.execute('SELECT * FROM catalog WHERE ns=? AND target=?', (self.ns, target)).fetchone()
        return dict(row, reasons=json.loads(row['reasons'])) if row else None

    def upsert_chat(self, target, row, *, baseline=False):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO chats VALUES (?,?,?,?,?)',
                            (self.ns, target.key, None, None, 'discovering'))
            self.db.execute('''INSERT INTO catalog VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ns,target) DO UPDATE SET name=excluded.name, kind=excluded.kind,
                parent_id=excluded.parent_id, available=1''',
                (self.ns, target.key, target.space_id, target.chat_id, row.get('name', ''),
                 row['kind'], row.get('parent_id'), 1, '[]', int(baseline)))

    def _reason(self, target, reason, enabled):
        row = self.discovered(target)
        reasons = set(row['reasons'])
        (reasons.add if enabled else reasons.discard)(reason)
        self.db.execute('UPDATE catalog SET reasons=? WHERE ns=? AND target=?',
                        (json.dumps(sorted(reasons)), self.ns, target))

    def reason(self, target, reason, enabled=True):
        with self.db:
            self._reason(target, reason, enabled)

    def unavailable(self, target):
        with self.db:
            self.db.execute('UPDATE catalog SET available=0 WHERE ns=? AND target=?', (self.ns, target))
            self.db.execute("UPDATE chats SET health='target_missing' WHERE ns=? AND target=?", (self.ns, target))

    def initialized(self, space):
        row = self.db.execute('SELECT initialized FROM discovery_state WHERE ns=? AND space_id=?',
                              (self.ns, space)).fetchone()
        return bool(row and row[0])

    def bootstrap_boundary(self, space, *, since=None):
        """Heart's state_id is a BSON ObjectId minted at local materialization.

        Persist before subscribing. One second of overlap conservatively includes
        changes racing startup, independent of buffered-event delivery or crashes.
        This is a classification boundary, never a processing acknowledgement.
        """
        with self.db:
            cursor = f'{int(time.time() if since is None else since) - 1:08x}' + '0' * 16
            self.db.execute('INSERT OR IGNORE INTO bootstrap_boundaries VALUES (?,?,?)',
                            (self.ns, space, cursor))
        return self.db.execute('SELECT cursor FROM bootstrap_boundaries WHERE ns=? AND space_id=?',
                               (self.ns, space)).fetchone()[0]

    def finish_snapshot(self, space):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO discovery_state VALUES (?,?,1)', (self.ns, space))

    def objects(self, space):
        return {r[0]: r[1] for r in self.db.execute(
            'SELECT object_id,discussion FROM owned_objects WHERE ns=? AND space_id=?', (self.ns, space))}

    def set_objects(self, space, objects):
        with self.db:
            self.db.execute('DELETE FROM owned_objects WHERE ns=? AND space_id=?', (self.ns, space))
            self.db.executemany('INSERT INTO owned_objects VALUES (?,?,?,?)',
                                [(self.ns, space, obj, discussion) for obj, discussion in objects.items()])

    def receipt(self, target, message_id):
        row = self.db.execute('SELECT * FROM inputs WHERE ns=? AND target=? AND message_id=?',
                              (self.ns, target, message_id)).fetchone()
        return dict(row) if row else None

    def confirmed(self, target, message_id):
        row = self.db.execute('SELECT confirmed FROM observations WHERE ns=? AND target=? AND message_id=?',
                              (self.ns, target, message_id)).fetchone()
        return bool(row and row[0])

    def observe(self, target, message, *, origin, confirmed=False):
        """Live additions can rescue a baseline race; recovery never revives old baseline."""
        with self.db:
            old = self.receipt(target, message['id'])
            status = 'baseline' if origin == 'baseline' else 'observed'
            if old:
                status = old['status']
                if status == 'baseline' and not confirmed and origin not in {'mention_edit', 'live'}:
                    # Preserve the REST baseline for later transition comparisons.
                    # A rolled-back mention removal must not make the unchanged
                    # historical mention look newly addressed during recovery.
                    return
                if status in {'baseline', 'ignored'} and origin == 'mention_edit':
                    if status == 'baseline':
                        self.db.execute('INSERT OR IGNORE INTO baseline_candidates VALUES (?,?,?,?)',
                                        (self.ns, target, message['id'], old['payload']))
                    status = 'observed'
                if status == 'baseline' and origin == 'live':
                    status = 'observed'
                # A hint update is not authoritative until REST confirms it.
                if status == 'pending' and not confirmed:
                    status = 'observed'
                self.db.execute('UPDATE inputs SET payload=?,status=?,updated=? WHERE ns=? AND target=? AND message_id=?',
                                (json.dumps(message), status, time.time(), self.ns, target, message['id']))
            else:
                self._insert(target, message, status)
            self.db.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?)',
                            (self.ns, target, message['id'], int(confirmed)))

    def baseline_candidate(self, target, message_id):
        row = self.db.execute('SELECT payload FROM baseline_candidates WHERE ns=? AND target=? AND message_id=?',
                              (self.ns, target, message_id)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def resolve_baseline_candidate(self, target, message_id, *, promote):
        with self.db:
            if not promote:
                self.db.execute("UPDATE inputs SET status='baseline' WHERE ns=? AND target=? AND message_id=? AND status='observed'",
                                (self.ns, target, message_id))
            self.db.execute('DELETE FROM baseline_candidates WHERE ns=? AND target=? AND message_id=?',
                            (self.ns, target, message_id))

    def admit(self, target, message_id, *, mention=False):
        with self.db:
            old = self.receipt(target, message_id)
            if not old or old['status'] not in {'observed', 'pending'}:
                return False
            if old['status'] != 'pending' and self.pending_count() >= 1000:
                raise RuntimeError('Anytype pending input capacity reached')
            if mention:
                if 'mention' not in self.discovered(target)['reasons']:
                    states = [json.loads(r[0]).get('state_id', '') for r in self.db.execute(
                        'SELECT payload FROM inputs WHERE ns=? AND target=? AND payload IS NOT NULL',
                        (self.ns, target))]
                    self.db.execute('INSERT OR REPLACE INTO watch_boundaries VALUES (?,?,?)',
                                    (self.ns, target, max(states, default='')))
                self._reason(target, 'mention', True)
            self.db.execute("UPDATE inputs SET status='pending',updated=? WHERE ns=? AND target=? AND message_id=?",
                            (time.time(), self.ns, target, message_id))
            return True

    def watch_boundary(self, target):
        row = self.db.execute('SELECT cursor FROM watch_boundaries WHERE ns=? AND target=?',
                              (self.ns, target)).fetchone()
        return row[0] if row else ''

    def complete_scan(self, target, cursor, latest_id=None):
        with self.db:
            current = self.chat(target)
            maximum = max(current.get('cursor') or '', cursor or '') or None
            self.db.execute('UPDATE chats SET cursor=?,latest_id=COALESCE(?,latest_id),health=? WHERE ns=? AND target=?',
                            (maximum, latest_id, 'connected', self.ns, target))
            self.db.execute('UPDATE catalog SET baseline_needed=0 WHERE ns=? AND target=?', (self.ns, target))
            self.db.execute('INSERT OR REPLACE INTO recovery_coverage VALUES (?,?,0)', (self.ns, target))

    def scan_incomplete(self, target):
        row = self.db.execute('SELECT incomplete FROM recovery_coverage WHERE ns=? AND target=?',
                              (self.ns, target)).fetchone()
        return bool(row and row[0])

    def start_scan(self, target):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO recovery_coverage VALUES (?,?,1)', (self.ns, target))

    def checkpoint_forward(self, target, cursor):
        if cursor and not self.scan_incomplete(target):
            with self.db:
                old = self.chat(target)['cursor'] or ''
                self._checkpoint(target, max(old, cursor))

    def chat(self, target):
        row = self.db.execute('SELECT * FROM chats WHERE ns=? AND target=?', (self.ns, target)).fetchone()
        return dict(row) if row else None

    def baseline(self, target, messages, cursor):
        with self.db:
            for message in messages:
                self._insert(target, message, 'baseline')
            self.db.execute('INSERT INTO chats VALUES (?,?,?,?,?)',
                            (self.ns, target, cursor, messages[-1]['id'] if messages else None, 'connected'))

    def _insert(self, target, message, status):
        return self.db.execute(
            'INSERT OR IGNORE INTO inputs(ns,target,message_id,payload,status,updated) VALUES (?,?,?,?,?,?)',
            (self.ns, target, message['id'], json.dumps(message, ensure_ascii=False), status, time.time()),
        ).rowcount > 0

    def record(self, target, message, status, cursor=None):
        with self.db:
            if status == 'pending' and self.pending_count() >= 1000:
                raise RuntimeError('Anytype pending input capacity reached')
            added = self._insert(target, message, status)
            if cursor is not None:
                self._checkpoint(target, cursor)
            if added:
                self.db.execute('UPDATE chats SET latest_id=? WHERE ns=? AND target=?',
                                (message['id'], self.ns, target))
            return added

    def _checkpoint(self, target, cursor):
        self.db.execute('UPDATE chats SET cursor=? WHERE ns=? AND target=?', (cursor, self.ns, target))

    def checkpoint(self, target, cursor):
        with self.db:
            self._checkpoint(target, cursor)

    def health(self, target, value):
        with self.db:
            self.db.execute('UPDATE chats SET health=? WHERE ns=? AND target=?', (value, self.ns, target))

    def known(self, target, message_id):
        return self.db.execute('SELECT status FROM inputs WHERE ns=? AND target=? AND message_id=?',
                               (self.ns, target, message_id)).fetchone() is not None

    def message(self, target, message_id):
        row = self.db.execute('SELECT payload FROM inputs WHERE ns=? AND target=? AND message_id=?',
                              (self.ns, target, message_id)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def refresh_message(self, target, message_id, message=None):
        with self.db:
            # Deletion cancels never-dispatched input, but cannot undo running tools.
            if message is None:
                self.db.execute("UPDATE inputs SET payload=NULL,status=CASE WHEN status='pending' THEN 'deleted' ELSE status END WHERE ns=? AND target=? AND message_id=?",
                                (self.ns, target, message_id))
            else:
                self.db.execute('UPDATE inputs SET payload=? WHERE ns=? AND target=? AND message_id=?',
                                (json.dumps(message), self.ns, target, message_id))

    def inputs(self, statuses):
        placeholders = ','.join('?' for _ in statuses)
        return [dict(row) for row in self.db.execute(
            f'SELECT * FROM inputs WHERE ns=? AND status IN ({placeholders}) ORDER BY seq',
            (self.ns, *statuses))]

    def mark(self, target, message_id, status):
        with self.db:
            self.db.execute('UPDATE inputs SET status=?,updated=? WHERE ns=? AND target=? AND message_id=?',
                            (status, time.time(), self.ns, target, message_id))

    def recover_inputs(self):
        # Hermes may resume its own durable turn. Never independently rerun side effects.
        with self.db:
            self.db.execute("UPDATE inputs SET status='uncertain' WHERE ns=? AND status IN ('dispatched','processing')",
                            (self.ns,))

    def pending_count(self):
        return self.db.execute(
            "SELECT count(*) FROM inputs WHERE ns=? AND status IN ('pending','dispatched','processing')",
            (self.ns,)).fetchone()[0]

    def delivery(self, operation):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM deliveries WHERE ns=? AND operation=? ORDER BY chunk', (self.ns, operation))]

    def prepare_delivery(self, operation, target, content, payloads):
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self.db:
            rows = self.delivery(operation)
            if rows:
                if rows[0]['target'] != target or rows[0]['content_hash'] != digest:
                    raise ValueError('Delivery identity reused with different target/content')
                return rows
            for i, payload in enumerate(payloads):
                self.db.execute('INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?)',
                                (self.ns, operation, target, digest, i, json.dumps(payload, ensure_ascii=False),
                                 str(uuid.uuid4()), 'pending', None, time.time()))
        return self.delivery(operation)

    def delivery_status(self, operation, chunk, status, remote_id=None):
        with self.db:
            self.db.execute('UPDATE deliveries SET status=?,remote_id=?,updated=? WHERE ns=? AND operation=? AND chunk=?',
                            (status, remote_id, time.time(), self.ns, operation, chunk))

    def delivery_payload(self, operation, chunk, payload):
        with self.db:
            self.db.execute('UPDATE deliveries SET payload=?,updated=? WHERE ns=? AND operation=? AND chunk=?',
                            (json.dumps(payload, ensure_ascii=False), time.time(), self.ns, operation, chunk))

    def recover_deliveries(self):
        with self.db:
            self.db.execute("UPDATE deliveries SET status='uncertain' WHERE ns=? AND status='sending'", (self.ns,))

    def summary(self):
        return {
            'inputs': {row[0]: row[1] for row in self.db.execute(
                'SELECT status,count(*) FROM inputs WHERE ns=? GROUP BY status', (self.ns,))},
            'deliveries': {row[0]: row[1] for row in self.db.execute(
                'SELECT status,count(*) FROM deliveries WHERE ns=? GROUP BY status', (self.ns,))},
        }
