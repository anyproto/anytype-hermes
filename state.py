"""Transactional input checkpoints and immutable per-operation delivery payloads."""

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
        ''')

    def close(self):
        self.db.close()

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
