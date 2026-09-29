"""Private durable inbox/outbox. SQLite transactions coordinate all host processes."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .html_render import render_chunks


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / "mirror.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY, update_id INTEGER UNIQUE NOT NULL,
                    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    route TEXT, error TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS outbox (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    message_id INTEGER, error TEXT, ready REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0);
            """)
            columns = {r[1] for r in db.execute('PRAGMA table_info(outbox)')}
            for name, declaration in {'html': 'TEXT', 'kind': "TEXT NOT NULL DEFAULT 'final'",
                                      'revision': 'INTEGER NOT NULL DEFAULT 0',
                                      'created': 'REAL NOT NULL DEFAULT 0'}.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE outbox ADD COLUMN {name} {declaration}')
            db.executescript("""
                CREATE TABLE IF NOT EXISTS live_turns (
                    id TEXT PRIMARY KEY, session_id TEXT, origin TEXT, active INTEGER,
                    expires REAL, typing_next REAL, last_interim TEXT, interim_key TEXT);
                CREATE TABLE IF NOT EXISTS live_tools (
                    id TEXT PRIMARY KEY, turn_id TEXT, batch TEXT, line TEXT, started REAL,
                    status TEXT, duration REAL);
                CREATE TABLE IF NOT EXISTS api_events (
                    id INTEGER PRIMARY KEY, at REAL, kind TEXT, item TEXT, message_id INTEGER,
                    outcome TEXT);
            """)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, key: str, default=None):
        with self.tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    @staticmethod
    def put(db, key, value):
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def set(self, key, value):
        with self.tx() as db:
            self.put(db, key, value)

    @staticmethod
    def enqueue(db, key: str, text: str, kind='final', ready=0):
        for index, chunk in enumerate(render_chunks(text)):
            if not chunk['plain'].strip():
                continue
            db.execute("INSERT OR IGNORE INTO outbox(id,body,html,kind,created,ready) VALUES (?,?,?,?,?,?)",
                       (f"{key}:{index}", chunk['plain'], chunk['html'], kind, time.time(), ready))

    def enqueue_text(self, key: str, text: str):
        with self.tx() as db:
            self.enqueue(db, key, text)

    def offset(self) -> int:
        return self.get("offset", 0)

    def accept(self, update_id: int, text: str | None, local_reply: str | None = None) -> str:
        """Durable admission + offset in one commit. Repeated updates are harmless."""
        identity = hashlib.sha256(f"telegram-update:{update_id}".encode()).hexdigest()
        with self.tx() as db:
            prior = db.execute('SELECT id FROM inbox WHERE update_id=?', (update_id,)).fetchone()
            if prior:
                identity = prior['id']
            else:
                salt = 0
                while db.execute('SELECT 1 FROM inbox WHERE substr(id,1,6)=?', (identity[:6],)).fetchone():
                    salt += 1
                    identity = hashlib.sha256(f'telegram-update:{update_id}:{salt}'.encode()).hexdigest()
            if text is not None:
                db.execute("INSERT OR IGNORE INTO inbox(id,update_id,text,status,created) VALUES (?,?,?,?,?)",
                           (identity, update_id, text, "local" if local_reply else "pending", time.time()))
                if local_reply:
                    self.enqueue(db, f"local:{identity}", local_reply)
            # Synthetic tests use negative ids and never advance the real cursor.
            if update_id >= 0:
                row = db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
                self.put(db, "offset", max(json.loads(row[0]) if row else 0, update_id + 1))
        return identity

    def recover(self):
        """Crash windows are unknown, never authorization for duplicate side effects."""
        with self.tx() as db:
            db.execute("UPDATE outbox SET status='uncertain',error='sender interrupted; inspect before retry' WHERE status='sending'")
            db.execute("UPDATE inbox SET status='uncertain',error='delivery interrupted; inspect before retry' WHERE status='delivering'")

    def claim_input(self):
        with self.tx() as db:
            # One unresolved input blocks following turns to preserve serialization.
            if db.execute("SELECT 1 FROM inbox WHERE status IN ('delivering','uncertain','waiting') LIMIT 1").fetchone():
                return None
            row = db.execute("SELECT * FROM inbox WHERE status='pending' ORDER BY created,update_id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE inbox SET status='delivering' WHERE id=?", (row['id'],))
                return dict(row)
        return None

    def finish_input(self, identity, status, route=None, error=None):
        with self.tx() as db:
            db.execute("UPDATE inbox SET status=?,route=COALESCE(?,route),error=? WHERE id=?",
                       (status, route, error, identity))

    def claim_output(self):
        with self.tx() as db:
            row = db.execute("SELECT * FROM outbox WHERE status IN ('pending','sending') ORDER BY seq LIMIT 1").fetchone()
            if not row or row['status'] == 'sending' or row['ready'] > time.time():
                return None
            db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE seq=?", (row['seq'],))
            return dict(row)

    def finish_output(self, seq, status, message_id=None, error=None, delay=0, revision=None):
        with self.tx() as db:
            current = db.execute('SELECT revision FROM outbox WHERE seq=?', (seq,)).fetchone()
            if status == 'sent' and revision is not None and current['revision'] != revision:
                status = 'pending'  # A tool completed while its send/edit was in flight.
            db.execute("UPDATE outbox SET status=?,message_id=?,error=?,ready=? WHERE seq=?",
                       (status, message_id, error, time.time() + delay, seq))

    def inbound_for_text(self, db, text):
        # Only a marker issued by this profile counts, not arbitrary 'via Telegram' prose.
        import re
        match = re.search(r"📱 via Telegram \[([0-9a-f]{64})\]:\n", text)
        if match:
            row = db.execute("SELECT * FROM inbox WHERE id=?", (match[1],)).fetchone()
            if row and f"📱 via Telegram [{row['id']}]:\n{row['text']}" in text:
                return dict(row)
        match = re.match(r'📱 \[([0-9a-f]{6})\] ', text)
        if match:
            rows = db.execute('SELECT * FROM inbox WHERE substr(id,1,6)=?', (match[1],)).fetchall()
            exact = [r for r in rows if marker(dict(r)) == text]
            if len(exact) == 1:
                return dict(exact[0])
        return None

    def record_turn(self, turn_id, user_text, reply, user_label, bot_label, skip_reply=False):
        with self.tx() as db:
            inbound = self.inbound_for_text(db, user_text)
            key = f"inbound:{inbound['id']}" if inbound else f"turn:{turn_id}"
            if not db.execute("INSERT OR IGNORE INTO turns VALUES (?)", (key,)).rowcount:
                return False
            if not inbound and user_text:
                self.enqueue(db, f"{key}:user", f"👤 {user_label} (Desktop): {user_text}")
            if reply and not skip_reply:
                self.enqueue(db, f"{key}:reply", f"🤖 {bot_label}: {reply}")
            if inbound:
                db.execute("UPDATE inbox SET status='settled',error=NULL WHERE id=?", (inbound['id'],))
            return True

    def reply_once(self, identity, reply, bot_label):
        with self.tx() as db:
            key = f"inbound:{identity}"
            # If the hook already owns the turn, stdout/receipt is never a second reply.
            if db.execute("INSERT OR IGNORE INTO turns VALUES (?)", (key,)).rowcount and reply:
                self.enqueue(db, f"{key}:reply", f"🤖 {bot_label}: {reply}")
            db.execute("UPDATE inbox SET status='settled',error=NULL WHERE id=?", (identity,))

    def summary(self):
        with self.tx() as db:
            return {table: dict(db.execute(f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall())
                    for table in ('inbox', 'outbox')}


def marker(row: dict) -> str:
    return f"📱 [{row['id'][:6]}] {row['text']}"
