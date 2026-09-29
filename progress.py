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
            text = event.get('text', '')
            if not text.strip():
                return
            identity = f"interim:{key}:{event.get('iteration')}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
            with self.store.tx() as db:
                # A late asynchronous stream observer must not resurrect a finished turn.
                row = db.execute('SELECT active FROM live_turns WHERE id=?', (key,)).fetchone()
                if not row or not row['active']:
                    return
                self.store.enqueue(db, identity, f"🤖 {self.settings['bot_label']}: {text}", kind='interim')
                db.execute('UPDATE live_turns SET last_interim=?,interim_key=? WHERE id=?', (text.strip(), identity, key))
                self.trim(db)
            return
        if kind.startswith('tool_') and self.settings.get('show_tools', True):
            self.tool(event, key)
            return
        if kind == 'final':
            reply = event.get('assistant_response', '')
            skip = False
            with self.store.tx() as db:
                row = db.execute('SELECT * FROM live_turns WHERE id=?', (key,)).fetchone()
                if row and reply.strip() and row['last_interim'] == reply.strip():
                    outputs = db.execute('SELECT status FROM outbox WHERE id LIKE ?', (row['interim_key'] + ':%',)).fetchall()
                    skip = bool(outputs) and all(r['status'] in {'pending', 'sending', 'sent'} for r in outputs)
                    if skip:
                        db.execute("UPDATE outbox SET kind='final' WHERE id LIKE ?", (row['interim_key'] + ':%',))
                db.execute('UPDATE live_turns SET active=0 WHERE id=?', (key,))
            self.store.record_turn(key, event.get('user_message', ''), reply,
                                   self.settings['user_label'], self.settings['bot_label'], skip_reply=skip)

    def trim(self, db):
        cap = self.settings.get('progress_queue_limit', 256)
        count = db.execute("SELECT count(*) FROM outbox WHERE status='pending' AND kind IN ('tool','interim')").fetchone()[0]
        if count > cap:
            db.execute("UPDATE outbox SET status='dropped' WHERE seq IN (SELECT seq FROM outbox WHERE status='pending' AND kind IN ('tool','interim') ORDER BY CASE kind WHEN 'tool' THEN 0 ELSE 1 END,seq LIMIT ?)", (count - cap,))

    def tool(self, event, turn):
        call_id = event.get('tool_call_id')
        if not call_id:
            return  # Never guess tool pairing from names or result content.
        tool_id = f'{turn}:{call_id}'
        at = event['at']
        with self.store.tx() as db:
            existing = db.execute('SELECT * FROM live_tools WHERE id=?', (tool_id,)).fetchone()
            if not existing:
                batch_row = db.execute('SELECT batch,min(started) AS first,count(*) AS n FROM live_tools WHERE turn_id=? GROUP BY batch ORDER BY first DESC LIMIT 1', (turn,)).fetchone()
                # Five lines fit under 3800 even when every preview char is '&'.
                if batch_row and at - batch_row['first'] <= 1.5 and batch_row['n'] < 5:
                    batch, started = batch_row['batch'], batch_row['first']
                else:
                    batch, started = f'tools:{tool_id}', at
                line = preview(event.get('tool_name', 'tool'), event.get('primary', ''))
                db.execute('INSERT INTO live_tools VALUES (?,?,?,?,?,?,?)', (tool_id, turn, batch, line, at, 'running', None))
            else:
                batch = existing['batch']
                started = db.execute('SELECT min(started) FROM live_tools WHERE batch=?', (batch,)).fetchone()[0]
            if event['kind'] == 'tool_end':
                status = 'ok' if event.get('status') in {'success', 'ok', 'completed'} else 'error'
                duration = max(0, float(event.get('duration_ms') or 0)) / 1000
                db.execute('UPDATE live_tools SET status=?,duration=? WHERE id=?', (status, duration, tool_id))
            lines = []
            for row in db.execute('SELECT * FROM live_tools WHERE batch=? ORDER BY started,id', (batch,)):
                suffix = '⏳' if row['status'] == 'running' else f"{'✅' if row['status'] == 'ok' else '❌'} {row['duration']:.1f}s"
                # Preview arguments are literal text, not Markdown supplied by a tool.
                lines.append(row['line'] + ' ' + suffix)
            import html
            body = '\n'.join(lines)
            html_body = html.escape(body, quote=False)
            row = db.execute('SELECT * FROM outbox WHERE id=?', (batch,)).fetchone()
            if row is None:
                db.execute('INSERT INTO outbox(id,body,html,kind,created,ready) VALUES (?,?,?,?,?,?)',
                           (batch, body, html_body, 'tool', at, started + 1.5))
            elif row['status'] not in {'dropped', 'uncertain', 'failed'}:
                db.execute("UPDATE outbox SET body=?,html=?,revision=revision+1,status=CASE WHEN status='sent' THEN 'pending' ELSE status END WHERE id=?",
                           (body, html_body, batch))
            self.trim(db)

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
