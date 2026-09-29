"""Profile-local mirror runtime: hooks enqueue; one gateway drains durable queues."""
from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from pathlib import Path

from .compat import Delivery, gateway_host
from .store import Store, marker
from .transport import APIError, Telegram
from .progress import Progress

log = logging.getLogger('telegram-mirror')


class Mirror:
    def __init__(self, home: Path, settings: dict, *, api=None, delivery=None):
        self.home, self.settings = Path(home).resolve(), settings
        if int(settings['chat_id']) not in settings['allowlist']:
            raise ValueError('Private destination must be in allowlist')
        self.store = Store(self.home / 'plugin-data' / 'telegram-mirror')
        self.stop = threading.Event()
        self.api = api
        self.delivery = delivery or Delivery(self.home, settings, self.store, self.stop)
        self._lock = None
        self._threads = []
        self.progress = Progress(self)
        self.bridge = None

    def on_turn(self, session_id=None, turn_id=None, user_message='', assistant_response='', **kwargs):
        """No network or subprocess: bounded local transaction survives oneshot exit."""
        if session_id != self.delivery.session():
            return False
        if not turn_id:
            log.warning('telegram-mirror ignored hook lacking a stable turn_id')
            return False
        if not isinstance(user_message, str) or not isinstance(assistant_response, str):
            log.warning('telegram-mirror ignored non-text hook payload')
            return False
        return self.store.record_turn(
            f'{session_id}:{turn_id}', user_message, assistant_response,
            self.settings['user_label'], self.settings['bot_label'],
        )

    def status(self):
        heartbeat = self.store.get('heartbeat', {})
        healthy = time.time() - heartbeat.get('time', 0) < 60
        last_poll = self.store.get('last_poll_ok', 0) or 0
        poll_health = 'OK' if time.time() - last_poll < 60 else 'not confirmed'
        poll_error = self.store.get('poll_error')
        return (f"{self.settings['bot_label']} Telegram mirror\n"
                f"Mirror worker: {'running' if healthy else 'offline or starting'}\n"
                f"Telegram polling: {poll_error or poll_health}\n"
                f"Bot Chat: {self.delivery.session()}\n"
                f"Queues: {self.store.summary()}\n"
                "Text messages enter the same Bot Chat. /start and /status stay local.\n"
                "Uncertain delivery is held for inspection, never automatically replayed.")

    def handle_update(self, update: dict):
        """The poller and synthetic integration test both call this exact admission path."""
        uid = update.get('update_id')
        if type(uid) is not int:
            return None
        msg = update.get('message') or {}
        sender = msg.get('from') or {}
        chat = msg.get('chat') or {}
        if (sender.get('id') not in self.settings['allowlist']
                or sender.get('is_bot', False)
                or chat.get('id') != self.settings['chat_id']
                or chat.get('type') != 'private'):
            log.warning('telegram-mirror ignored unauthorized or unsupported update')
            self.store.accept(uid, None)
            return None
        text = msg.get('text')
        if not isinstance(text, str) or not text.strip():
            return self.store.accept(uid, '', 'This mirror accepts text messages only; please send text.')
        command = text.split(maxsplit=1)[0].split('@', 1)[0]
        return self.store.accept(uid, text, self.status() if command in {'/start', '/status'} else None)

    def send_one(self):
        # A single gateway sender owns ALL outbound calls, across every hook process.
        # Persist the rate gate so hot reload/restart cannot reset the per-chat budget.
        if time.time() < self.store.get('next_send_at', 0):
            return False
        typing = self.progress.typing_due()
        if typing:
            self.store.set('next_send_at', time.time() + 1.05)
            try:
                accepted = self.api.call('sendChatAction', {'chat_id': self.settings['chat_id'], 'action': 'typing'})
                if accepted is not True:
                    raise RuntimeError('Typing not accepted')
                self.progress.audit('typing', typing, None, 'ok')
            except Exception:
                self.progress.typing_failed(typing)
                self.progress.audit('typing', typing, None, 'failed')
            return True
        row = self.store.claim_output()
        if not row:
            return False
        try:
            self.store.set('next_send_at', time.time() + 1.05)
            result = self.api.send(row['body'], html=row['html'], message_id=row['message_id'])
        except APIError as exc:
            if exc.code == 400 and row['html'] and any(s in exc.description.lower() for s in ("parse entities", "unsupported start tag", "can't find end tag")):
                # Explicit rejection means safe to retry as plain text, in the next rate slot.
                with self.store.tx() as db:
                    db.execute("UPDATE outbox SET html=NULL,status='pending' WHERE seq=?", (row['seq'],))
                self.progress.audit('html_fallback', row['id'], row['message_id'], 'rejected')
            elif exc.code == 400 and row['message_id'] and 'message is not modified' in exc.description:
                self.store.finish_output(row['seq'], 'sent', message_id=row['message_id'], revision=row['revision'])
                self.progress.audit('edit', row['id'], row['message_id'], 'unchanged')
            elif exc.code == 429 and row['attempts'] < 8:
                self.store.set('next_send_at', time.time() + max(1.05, exc.retry_after))
                self.store.finish_output(row['seq'], 'pending', error='Telegram rate limited',
                                         message_id=row['message_id'], delay=max(1, exc.retry_after))
            else:
                status = 'uncertain' if exc.code >= 500 else 'failed'
                self.store.finish_output(row['seq'], status, message_id=row['message_id'], error=f'Telegram API {exc.code}')
                self.progress.audit('edit' if row['message_id'] else row['kind'], row['id'], row['message_id'], f'API {exc.code}')
        except Exception:
            self.store.finish_output(row['seq'], 'uncertain', message_id=row['message_id'], error='Network outcome unknown; no automatic resend')
        else:
            self.store.finish_output(row['seq'], 'sent', message_id=result['message_id'], revision=row['revision'])
            self.progress.audit('edit' if row['message_id'] else row['kind'], row['id'], result['message_id'], 'ok')
            log.info('telegram-mirror sent message_id=%s', result['message_id'])
        return True

    def deliver_one(self):
        row = self.store.claim_input()
        if not row:
            return False
        try:
            result = self.delivery.deliver(row, marker(row))
            if result['status'] == 'settled':
                self.store.reply_once(row['id'], result.get('reply', ''), self.settings['bot_label'])
            elif result['status'] != 'waiting':
                self.store.finish_input(row['id'], 'uncertain', error='Unexpected delivery result')
        except Exception:
            with self.store.tx() as db:
                db.execute("UPDATE inbox SET status='uncertain',error='Delivery outcome unknown; inspect before resending' WHERE id=? AND status!='settled'", (row['id'],))
            log.warning('telegram-mirror delivery needs inspection (no automatic retry)')
        return True

    def check_receipts(self):
        with self.store.tx() as db:
            rows = db.execute("SELECT id FROM inbox WHERE route='mailbox' AND status IN ('waiting','uncertain','delivering')").fetchall()
        for row in rows:
            receipt = self.delivery.receipt(row['id'])
            if receipt and receipt['status'] == 'settled':
                self.store.reply_once(row['id'], receipt.get('reply', ''), self.settings['bot_label'])
            elif receipt and receipt['status'] in {'failed', 'cancelled', 'ambiguous'}:
                self.store.finish_input(row['id'], 'uncertain', error=f"Mailbox {receipt['status']}; no automatic retry")

    def start(self):
        if self.settings.get('host', 'gateway') != 'gateway':
            return
        # Startup may discover plugins before the gateway obtains its runtime lock.
        # This launcher does no network work until BOTH the host gate and flock pass.
        import sys
        from hermes_constants import get_process_hermes_home
        if not any(sys.argv[i:i + 2] == ['gateway', 'run'] for i in range(len(sys.argv))):
            return
        if Path(get_process_hermes_home()).resolve() != self.home:
            return
        self._thread(self._lead, 'telegram-mirror-leader')

    def _thread(self, fn, name):
        thread = threading.Thread(target=fn, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _acquire_leader(self):
        """A hot-reloaded instance waits until the previous instance fully drains."""
        lock = open(self.store.root / 'poller.lock', 'a')
        while not self.stop.is_set():
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except BlockingIOError:
                self.stop.wait(0.25)
        lock.close()
        return None

    def _lead(self, *, standalone=False):
        while not standalone and not self.stop.is_set() and not gateway_host(self.home):
            self.stop.wait(1)
        if self.stop.is_set():
            return
        lock = self._acquire_leader()
        if lock is None:
            return
        self._lock = lock
        try:
            self.store.recover()
            self.api = self.api or Telegram(self.settings['keychain_service'],
                                           self.settings['keychain_account'], self.settings['chat_id'])
            me = self.api.call('getMe')
            expected = self.settings.get('bot_username')
            if expected and me.get('username', '').lower() != expected.lstrip('@').lower():
                raise RuntimeError('Keychain bot identity does not match configuration')
            if self.api.call('getWebhookInfo').get('url'):
                raise RuntimeError('An existing webhook conflicts with long polling')
            self.store.set('bot', {'id': me['id'], 'username': me['username']})
            self.store.set('startup_error', None)
            self._thread(self._poll, 'telegram-mirror-poll')
            self._thread(self._deliver_loop, 'telegram-mirror-deliver')
            self._thread(self._send_loop, 'telegram-mirror-send')
            log.info('telegram-mirror worker leader pid=%s', os.getpid())
            while not self.stop.is_set():
                self.store.set('heartbeat', {'pid': os.getpid(), 'time': time.time()})
                self.stop.wait(5)
        except Exception:
            log.error('telegram-mirror worker startup failed; check Keychain, bot identity and webhook configuration')
            self.store.set('startup_error', 'Worker startup failed; no credential details logged')
        finally:
            # Do NOT release leadership while a worker may still be inside an HTTP/CLI call.
            # Normal process exit releases flock. Unload retains it until all workers finish.
            self.stop.set()
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=self.settings.get('turn_timeout_seconds', 600) + 40)
            lock.close()
            self._lock = None

    def _poll(self):
        while not self.stop.is_set():
            try:
                updates = self.api.call('getUpdates', {
                    'offset': self.store.offset(), 'timeout': self.settings.get('long_poll_seconds', 20),
                    'limit': 100, 'allowed_updates': ['message'],
                }, timeout=self.settings.get('long_poll_seconds', 20) + 10)
                for update in updates:
                    if self.stop.is_set():
                        break
                    self.handle_update(update)
                self.store.set('last_poll_ok', time.time())
                self.store.set('poll_error', None)
            except APIError as exc:
                self.store.set('poll_error', f'Telegram API {exc.code}')
                if exc.code in {401, 409}:
                    log.error('telegram-mirror polling stopped: credential or competing consumer error')
                    return
                self.stop.wait(max(5, exc.retry_after))
            except Exception:
                self.store.set('poll_error', 'Polling failed (details suppressed)')
                self.stop.wait(5)

    def _deliver_loop(self):
        while not self.stop.is_set():
            try:
                self.check_receipts()
                self.deliver_one()
            except Exception:
                log.warning('telegram-mirror delivery worker needs inspection')
            self.stop.wait(1)

    def _send_loop(self):
        while not self.stop.is_set():
            try:
                active = self.send_one()
            except Exception:
                log.warning('telegram-mirror outbox worker needs inspection')
                active = False
            self.stop.wait(0.1 if active else 0.5)

    def close(self):
        if self.bridge:
            self.bridge.close()
        self.stop.set()
