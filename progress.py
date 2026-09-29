"""Worker-side live turn state and batched tool cards. Never runs in a hook."""
import hashlib
import time

from .redaction import preview


class Progress:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store, self.settings = runtime.store, runtime.settings

    def handle(self, event):
        kind, sid = event['kind'], event['session_id']
        turn = event.get('turn_id')
        key = f'{sid}:{turn}'
        if kind == 'stop':
            with self.store.tx() as db:
                db.execute('UPDATE live_turns SET active=0 WHERE session_id=?', (sid,))
            return
        if not turn:
            return
        if kind in {'start', 'stream'}:
            with self.store.tx() as db:
                user = event.get('user_message', '')
                origin = self.store.inbound_for_text(db, user)
                origin_key = f"inbound:{origin['id']}" if origin else f'turn:{key}'
                db.execute('INSERT OR IGNORE INTO live_turns VALUES (?,?,?,?,?,?,?,?)',
                           (key, sid, origin_key, 1, event['at'] + self.settings.get('turn_timeout_seconds', 600), 0, '', ''))
                if kind == 'start' and user and not origin:
                    self.store.enqueue(db, f'{origin_key}:user', f"👤 {self.settings['user_label']} (Desktop): {user}", kind='user')
            return
        if kind == 'interim' and self.settings.get('show_interim', True):
            text = event.get('text', '').strip()
            if not text:
                return
            identity = f"note:{key}:{event.get('iteration')}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
            with self.store.tx() as db:
                # A late asynchronous stream observer must not resurrect a finished turn.
                row = db.execute('SELECT active FROM live_turns WHERE id=?', (key,)).fetchone()
                if not row or not row['active']:
                    return
                if not db.execute('SELECT 1 FROM live_tools WHERE id=?', (identity,)).fetchone():
                    note = text if len(text) <= self.NOTE_LIMIT else text[:self.NOTE_LIMIT - 1] + '…'
                    self._card_add(db, key, identity, note, event['at'], 'note')
                db.execute('UPDATE live_turns SET last_interim=?,interim_key=? WHERE id=?', (text, identity, key))
                self.trim(db)
            return
        if kind.startswith('tool_') and self.settings.get('show_tools', True):
            self.tool(event, key)
            return
        if kind == 'final':
            reply = event.get('assistant_response', '')
            with self.store.tx() as db:
                row = db.execute('SELECT * FROM live_turns WHERE id=?', (key,)).fetchone()
                if row and reply.strip() and row['last_interim'] == reply.strip() and row['interim_key']:
                    # The last interim IS the answer: drop it from the card; the final carries it.
                    note = db.execute('SELECT batch FROM live_tools WHERE id=?', (row['interim_key'],)).fetchone()
                    if note:
                        db.execute('DELETE FROM live_tools WHERE id=?', (row['interim_key'],))
                        self._card_render(db, note['batch'], event['at'])
                db.execute('UPDATE live_turns SET active=0 WHERE id=?', (key,))
            self.store.record_turn(key, event.get('user_message', ''), reply,
                                   self.settings['user_label'], self.settings['bot_label'], skip_reply=False)

    def trim(self, db):
        cap = self.settings.get('progress_queue_limit', 256)
        count = db.execute("SELECT count(*) FROM outbox WHERE status='pending' AND kind IN ('tool','interim')").fetchone()[0]
        if count > cap:
            db.execute("UPDATE outbox SET status='dropped' WHERE seq IN (SELECT seq FROM outbox WHERE status='pending' AND kind IN ('tool','interim') ORDER BY CASE kind WHEN 'tool' THEN 0 ELSE 1 END,seq LIMIT ?)", (count - cap,))

    # One live "activity card" per turn: interim notes and tool lines accumulate in a single
    # Telegram message that is edited in place; it rolls over to a new card near the size limit.
    CARD_LIMIT = 3400
    NOTE_LIMIT = 700

    def tool(self, event, turn):
        call_id = event.get('tool_call_id')
        if not call_id:
            return  # Never guess tool pairing from names or result content.
        tool_id = f'{turn}:{call_id}'
        at = event['at']
        with self.store.tx() as db:
            existing = db.execute('SELECT * FROM live_tools WHERE id=?', (tool_id,)).fetchone()
            if not existing:
                line = preview(event.get('tool_name', 'tool'), event.get('primary', ''))
                batch = self._card_add(db, turn, tool_id, line, at, 'running')
            else:
                batch = existing['batch']
            if event['kind'] == 'tool_end':
                status = 'ok' if event.get('status') in {'success', 'ok', 'completed'} else 'error'
                duration = max(0, float(event.get('duration_ms') or 0)) / 1000
                db.execute('UPDATE live_tools SET status=?,duration=? WHERE id=?', (status, duration, tool_id))
                self._card_render(db, batch, at)
            self.trim(db)

    def _card_add(self, db, turn, item_id, line, at, status):
        last = db.execute('SELECT batch FROM live_tools WHERE turn_id=? ORDER BY rowid DESC LIMIT 1',
                          (turn,)).fetchone()
        batch = last['batch'] if last else f'card:{turn}:0'
        db.execute('INSERT INTO live_tools VALUES (?,?,?,?,?,?,?)', (item_id, turn, batch, line, at, status, None))
        if last and self._html_len(self._card_body(db, batch)) > self.CARD_LIMIT:
            index = int(batch.rsplit(':', 1)[1]) + 1 if batch.startswith('card:') else 1
            batch = f'card:{turn}:{index}'
            db.execute('UPDATE live_tools SET batch=? WHERE id=?', (batch, item_id))
        self._card_render(db, batch, at)
        return batch

    @staticmethod
    def _html_len(body):
        import html
        return len(html.escape(body, quote=False).encode('utf-16-le')) // 2

    @staticmethod
    def _card_body(db, batch):
        lines = []
        for row in db.execute('SELECT * FROM live_tools WHERE batch=? ORDER BY rowid', (batch,)):
            if row['status'] == 'note':
                lines.append(f"💬 {row['line']}")
            elif row['status'] == 'running':
                lines.append(f"{row['line']} ⏳")
            else:
                mark = '✅' if row['status'] == 'ok' else '❌'
                lines.append(f"{row['line']} {mark} {row['duration']:.1f}s")
        return '\n'.join(lines)

    def _card_render(self, db, batch, at):
        import html
        body = self._card_body(db, batch)
        # Preview arguments and notes are literal text, not Markdown supplied by a tool.
        html_body = html.escape(body, quote=False)
        row = db.execute('SELECT * FROM outbox WHERE id=?', (batch,)).fetchone()
        if not body.strip():
            if row and row['status'] == 'pending' and row['message_id'] is None:
                db.execute("UPDATE outbox SET status='dropped' WHERE id=?", (batch,))
            return
        if row is None:
            db.execute('INSERT INTO outbox(id,body,html,kind,created,ready) VALUES (?,?,?,?,?,?)',
                       (batch, body, html_body, 'tool', at, at + 1.5))
        elif row['status'] not in {'dropped', 'uncertain', 'failed'}:
            db.execute("UPDATE outbox SET body=?,html=?,revision=revision+1,"
                       "status=CASE WHEN status='sent' THEN 'pending' ELSE status END WHERE id=?",
                       (body, html_body, batch))

    def typing_due(self):
        with self.store.tx() as db:
            row = db.execute('SELECT * FROM live_turns WHERE active=1 AND expires>? AND typing_next<=? ORDER BY typing_next LIMIT 1', (time.time(), time.time())).fetchone()
            if row:
                db.execute('UPDATE live_turns SET typing_next=? WHERE id=?', (time.time() + 4, row['id']))
                return row['id']
        return None

    def typing_failed(self, key):
        with self.store.tx() as db:
            db.execute('UPDATE live_turns SET typing_next=expires+1 WHERE id=?', (key,))

    def audit(self, kind, item, message_id, outcome):
        with self.store.tx() as db:
            db.execute('INSERT INTO api_events(at,kind,item,message_id,outcome) VALUES (?,?,?,?,?)',
                       (time.time(), kind, item, message_id, outcome))
