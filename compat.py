"""Isolated Hermes compatibility boundary (core is never modified).

PluginContext.inject_message is process-local: a messaging gateway cannot target a
separate Desktop process through it. In particular, a True gateway return can
precede asynchronous 'unknown route' rejection. Do NOT probe that path then fall
back: that could run two turns. This adapter uses Hermes' existing owner-pinned
Bot Chat mailbox when open, and its public CLI when unowned. No private DM tool
or agent impersonation is used.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path


class DeliveryError(Exception):
    pass


def resolve_session(home: Path, override: str | None = None) -> str:
    with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as db:
        if override:
            rows = db.execute("SELECT id FROM sessions WHERE id=?", (override,)).fetchall()
        else:
            rows = db.execute("SELECT id FROM sessions WHERE title='Bot Chat' AND hidden=1 AND archived=0").fetchall()
    if len(rows) != 1:
        raise DeliveryError("Expected exactly one unarchived hidden Bot Chat; set session_id explicitly")
    # Compression lineage is a Hermes concern, isolated here rather than reimplemented.
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / 'state.db', read_only=True)
    try:
        tip = db.get_compression_tip(rows[0][0]) or rows[0][0]
    finally:
        db.close()
    with sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True) as conn:
        valid = conn.execute("SELECT 1 FROM sessions WHERE id=? AND title='Bot Chat' AND hidden=1 AND archived=0", (tip,)).fetchone()
    if not valid:
        raise DeliveryError('Resolved session is not the active canonical Bot Chat')
    return tip


def gateway_host(home: Path) -> bool:
    """Only the actual profile gateway may own network workers, never CLI/Desktop."""
    import sys
    from hermes_constants import get_process_hermes_home
    from gateway.status import owns_gateway_runtime_lock
    return (any(sys.argv[i:i + 2] == ['gateway', 'run'] for i in range(len(sys.argv)))
            and Path(get_process_hermes_home()).resolve() == home.resolve()
            and owns_gateway_runtime_lock())


class Delivery:
    def __init__(self, home, settings, store, stop):
        self.home, self.settings, self.store, self.stop = Path(home), settings, store, stop

    def session(self):
        return resolve_session(self.home, self.settings.get('session_id'))

    def deliver(self, row, text):
        """One serialized call. After admission or uncertainty, NEVER try another route."""
        lock = open(self.store.root / 'delivery.lock', 'a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._deliver(row, text)
        finally:
            lock.close()

    def _deliver(self, row, text):
        from tools.bot_live_delivery import (
            find_canonical_owner, find_canonical_live_owner, deliver_to_live_owner,
        )
        session_id = self.session()
        owner = find_canonical_owner(self.home)
        if owner:
            live = find_canonical_live_owner(self.home)
            if not live or live['session_id'] != session_id:
                raise DeliveryError('Bot Chat has an unsupported live owner; no turn submitted')
            self.store.finish_input(row['id'], 'delivering', 'mailbox')
            deliver_to_live_owner(self.home, live, text, delivery_id=row['id'])
            # Persist acceptance and let the serial worker await the durable receipt.
            self.store.finish_input(row['id'], 'waiting', 'mailbox')
            return {'status': 'waiting', 'route': 'mailbox'}
        self.store.finish_input(row['id'], 'delivering', 'cli')
        return self.run_cli(session_id, text)

    def receipt(self, identity):
        from tools.bot_live_delivery import read_delivery_result
        return read_delivery_result(self.home, identity)

    def run_cli(self, session_id, text):
        """Public CLI; explicit profile, no shell, no approvals changed, no token in env."""
        query_dir = self.store.root / 'queries'
        query_dir.mkdir(mode=0o700, exist_ok=True)
        query = query_dir / f'{uuid.uuid4().hex}.txt'
        fd = os.open(query, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        env = os.environ.copy()
        env['HERMES_HOME'] = str(self.home)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        command = [self.settings['hermes_executable'], '-p', self.settings['profile'],
                   'chat', '--resume', session_id, '--query-file', str(query), '--oneshot', '-Q']
        try:
            result = subprocess.run(command, cwd=str(self.home), env=env,
                                    capture_output=True, text=True,
                                    timeout=self.settings.get('turn_timeout_seconds', 600))
            if result.returncode:
                # Never forward raw stderr/stdout: they may contain private context/credentials.
                raise DeliveryError(f'CLI exited {result.returncode}; inspect profile logs before resending')
            return {'status': 'settled', 'route': 'cli', 'reply': result.stdout.strip()}
        except subprocess.TimeoutExpired:
            raise DeliveryError('CLI timed out; outcome unknown, not automatically retried') from None
        finally:
            # User requested no deletion: retain query in private macOS Trash, not plugin source.
            trash = Path.home() / '.Trash' / 'telegram-mirror-queries'
            trash.mkdir(mode=0o700, parents=True, exist_ok=True)
            trash.chmod(0o700)
            shutil.move(str(query), str(trash / query.name))
