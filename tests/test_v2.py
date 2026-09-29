"""v2 regression tests: no Telegram/Keychain or real agent calls."""
import importlib.util
from pathlib import Path
import sys
import time
from html.parser import HTMLParser
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mirror_v2_test', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pkg
spec.loader.exec_module(pkg)
Mirror = importlib.import_module('mirror_v2_test.mirror').Mirror
HookBridge = importlib.import_module('mirror_v2_test.hooks').HookBridge
redact = importlib.import_module('mirror_v2_test.redaction').redact_args
render = importlib.import_module('mirror_v2_test.html_render').render_chunks
APIError = importlib.import_module('mirror_v2_test.transport').APIError


@pytest.fixture
def runtime(tmp_path):
    delivery = Mock()
    delivery.session.return_value = 'canonical'
    api = Mock()
    api.call.return_value = True
    api.send.return_value = {'message_id': 42}
    return Mirror(tmp_path, dict(chat_id=123, allowlist=[123], bot_label='Bot', user_label='Owner',
                               turn_timeout_seconds=10), api=api, delivery=delivery)


def emit(r, kind, **kwargs):
    e = dict(kind=kind, session_id='canonical', turn_id='turn', at=time.time())
    e.update(kwargs)
    r.progress.handle(e)


def outputs(r):
    with r.store.tx() as db:
        return [dict(x) for x in db.execute('SELECT * FROM outbox ORDER BY seq')]


def open_gate(r):
    r.store.set('next_send_at', 0)
    with r.store.tx() as db:
        db.execute('UPDATE outbox SET ready=0')


def test_typing_refresh_and_stop(runtime):
    emit(runtime, 'start')
    assert runtime.send_one()
    runtime.api.call.assert_called_once_with('sendChatAction', {'chat_id': 123, 'action': 'typing'})
    assert not runtime.send_one()  # Shared rate limiter also covers typing.
    open_gate(runtime)
    assert not runtime.send_one()  # No second refresher for the same turn.
    with runtime.store.tx() as db:
        db.execute('UPDATE live_turns SET typing_next=0')
    assert runtime.send_one()
    emit(runtime, 'final', user_message='', assistant_response='done')
    open_gate(runtime)
    assert runtime.send_one()
    assert runtime.api.call.call_count == 2


def test_typing_timeout_and_error(runtime):
    emit(runtime, 'start', at=time.time() - 20)
    assert not runtime.send_one()
    with runtime.store.tx() as db:
        db.execute('UPDATE live_turns SET expires=?', (time.time() + 10,))
    runtime.api.call.side_effect = OSError('network')
    assert runtime.send_one()
    open_gate(runtime)
    assert not runtime.send_one()
    emit(runtime, 'interim', text='still working', iteration=1)
    assert len(outputs(runtime)) == 1  # Typing failure must not silence assistant progress.


def test_interim_final_dedupe_and_no_late_resurrection(runtime):
    emit(runtime, 'start')
    emit(runtime, 'interim', text='done', iteration=1)
    emit(runtime, 'interim', text='done', iteration=1)
    emit(runtime, 'final', assistant_response='done', user_message='')
    emit(runtime, 'interim', text='late duplicate', iteration=2)
    live = [o for o in outputs(runtime) if o['status'] != 'dropped']
    assert len(live) == 1  # The interim equal to the answer leaves the card; final carries it.
    assert live[0]['kind'] == 'final'
    assert 'late duplicate' not in ''.join(o['body'] for o in outputs(runtime))


def test_dropped_interim_does_not_lose_final(runtime):
    emit(runtime, 'start')
    emit(runtime, 'interim', text='done', iteration=1)
    with runtime.store.tx() as db:
        db.execute("UPDATE outbox SET status='dropped'")
    emit(runtime, 'final', assistant_response='done')
    assert outputs(runtime)[-1]['kind'] == 'final'
    assert outputs(runtime)[-1]['status'] == 'pending'


def test_tool_batch_edits_and_never_results(runtime):
    for i in range(3):
        emit(runtime, 'tool_start', tool_call_id=f'tool{i}', tool_name='terminal', primary='date')
    assert len(outputs(runtime)) == 1 and outputs(runtime)[0]['body'].count('🔧') == 3
    assert not runtime.send_one()  # 1.5s coalescing window.
    open_gate(runtime)
    assert runtime.send_one()
    emit(runtime, 'tool_end', tool_call_id='tool1', tool_name='terminal', primary='date',
         status='success', duration_ms=1200, result='MUST NOT APPEAR')
    open_gate(runtime)
    assert runtime.send_one()
    assert runtime.api.send.call_args.kwargs['message_id'] == 42
    assert '✅ 1.2s' in outputs(runtime)[0]['body']
    assert 'MUST NOT APPEAR' not in outputs(runtime)[0]['body']
    emit(runtime, 'tool_end', tool_call_id='tool2', status='error', duration_ms=300)
    assert '❌ 0.3s' in outputs(runtime)[0]['body']


def test_inflight_tool_completion_requeues_edit(runtime):
    emit(runtime, 'tool_start', tool_call_id='a', tool_name='terminal', primary='date')
    open_gate(runtime)
    old = runtime.store.claim_output()
    emit(runtime, 'tool_end', tool_call_id='a', status='success', duration_ms=50)
    runtime.store.finish_output(old['seq'], 'sent', message_id=42, revision=old['revision'])
    assert outputs(runtime)[0]['status'] == 'pending'
    assert outputs(runtime)[0]['message_id'] == 42


def test_html_rejection_falls_back_next_rate_slot(runtime):
    runtime.store.enqueue_text('test', '**hello**')
    runtime.api.send.side_effect = APIError(400, "Bad Request: can't parse entities")
    assert runtime.send_one()
    assert outputs(runtime)[0]['html'] is None
    assert not runtime.send_one()
    open_gate(runtime)
    runtime.api.send.side_effect = None
    assert runtime.send_one()
    assert runtime.api.send.call_args.kwargs['html'] is None
    assert runtime.api.send.call_args.args[0] == 'hello'


def test_progress_queue_bounds_and_final_preserved(runtime):
    runtime.settings['progress_queue_limit'] = 2
    for i in range(4):
        emit(runtime, 'tool_start', tool_call_id=str(i), tool_name='terminal', primary='date', at=time.time() + i * 2)
    emit(runtime, 'final', assistant_response='final')
    pending = [x for x in outputs(runtime) if x['status'] == 'pending']
    cards = [x for x in pending if x['kind'] == 'tool']
    assert len(cards) == 1 and cards[0]['body'].count('🔧') == 4  # One edited card, not 4 messages.
    assert pending[-1]['body'] == '🤖 Bot: final'


def test_disabled_progress(runtime):
    runtime.settings.update(show_tools=False, show_interim=False)
    emit(runtime, 'start')
    emit(runtime, 'tool_start', tool_call_id='x', primary='date')
    emit(runtime, 'interim', text='not shown')
    assert outputs(runtime) == []


@pytest.mark.parametrize('value,secret', [
    ('API_KEY="small-secret" date', 'small-secret'),
    ("TOKEN='small-secret' date", 'small-secret'),
    ('curl -H "Authorization: Bearer abcdefg" example.org', 'abcdefg'),
    ('{"password": "abc123"}', 'abc123'),
    ('curl --password abc123 url', 'abc123'),
    ('https://bob:abc123@example.org', 'abc123'),
    ('echo sk-secret123456', 'sk-secret123456'),
    ('{"key": "tiny-key"}', 'tiny-key'),
    ('curl --password "a b c" url', 'a b c'),
])
def test_argument_redaction(value, secret):
    assert secret not in redact(value)


def test_exact_bot_token_and_no_code_reformat():
    token = '123456789:' + 'a' * 35
    assert token not in redact('curl ' + token, token)
    parts = render('`**literal** _name_ <&>`')
    assert '<code>**literal** _name_ &lt;&amp;&gt;</code>' == parts[0]['html']


class Balanced(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.text = [], ''

    def handle_starttag(self, tag, attrs):
        assert tag in {'b', 'i', 'code', 'pre', 'a'}
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, data):
        self.text += data


@pytest.mark.parametrize('text', ['**' + ('emoji 😀<& ' * 900) + '**',
                                  '```\n' + ('a<&>' * 6000) + '\n```',
                                  '[x](https://example.org/?a=1&b=2) ' * 900])
def test_long_chunks_entities_balanced_exact_plain(text):
    for chunk in render(text, limit=512):
        assert len(chunk['html'].encode('utf-16-le')) // 2 <= 512
        p = Balanced()
        p.feed(chunk['html'])
        p.close()
        assert not p.stack and p.text == chunk['plain']


def test_table_empty_cells_and_header_escaping():
    result = render('| <A> | B |\n|---|---|\n| | **ok** |')
    assert '• &lt;A&gt;:  · B: <b>ok</b>' in result[0]['html']
    assert result[0]['plain'].strip() == '• <A>:  · B: ok'


def test_hooks_run_off_callback_thread_and_return_none(runtime):
    import threading
    observed = []
    runtime.progress.handle = lambda e: observed.append((e, threading.current_thread().name))
    bridge = HookBridge(runtime, capacity=2)
    try:
        cb = bridge.callback('tool_start')
        assert cb(session_id='canonical', turn_id='turn', tool_call_id='one', args={'command': 'date'}, result='secret result') is None
        bridge.callback('final')(session_id='canonical', turn_id='turn', assistant_response='done')
        bridge.close()
        assert observed and all(name == 'telegram-mirror-persist' for _, name in observed)
        assert all('result' not in event for event, _ in observed)
    finally:
        bridge.close()


def test_short_marker_collision_is_disambiguated(runtime):
    import hashlib
    storemod = importlib.import_module('mirror_v2_test.store')
    prospective = hashlib.sha256(b'telegram-update:10').hexdigest()
    other = prospective[:6] + 'f' * 58
    with runtime.store.tx() as db:
        db.execute('INSERT INTO inbox(id,update_id,text,created) VALUES (?,?,?,?)', (other, 9, 'same', time.time()))
    identity = runtime.store.accept(10, 'same')
    assert identity[:6] != other[:6]
    assert runtime.store.accept(10, 'same') == identity
    text = storemod.marker({'id': identity, 'text': 'same'})
    assert text == f'📱 [{identity[:6]}] same' and identity not in text
    with runtime.store.tx() as db:
        assert runtime.store.inbound_for_text(db, text)['id'] == identity


def test_stop_event_and_other_session_filter(runtime):
    emit(runtime, 'start')
    emit(runtime, 'stop')
    assert runtime.progress.typing_due() is None
    seen = []
    runtime.progress.handle = lambda e: seen.append(e)
    bridge = HookBridge(runtime)
    bridge.callback('final')(session_id='unrelated', turn_id='x', assistant_response='private')
    bridge.close()
    assert seen == []


def test_tool_html_bound_under_escape_expansion(runtime):
    for i in range(20):
        emit(runtime, 'tool_start', tool_call_id=str(i), tool_name='n' * 60, primary='&' * 120)
    cards = outputs(runtime)
    assert 1 < len(cards) < 20  # Rolls over near the limit instead of one message per call.
    assert all(len(c['html'].encode('utf-16-le')) // 2 < 3800 for c in cards)


def test_turn_activity_is_one_edited_card(runtime):
    emit(runtime, 'start')
    emit(runtime, 'interim', text='Checking the thread first.', iteration=1)
    emit(runtime, 'tool_start', tool_call_id='a', tool_name='read_file', primary='notes.md', at=time.time() + 5)
    emit(runtime, 'tool_end', tool_call_id='a', status='success', duration_ms=500)
    emit(runtime, 'interim', text='Now building the draft.', iteration=2)
    emit(runtime, 'tool_start', tool_call_id='b', tool_name='terminal', primary='date', at=time.time() + 30)
    cards = [o for o in outputs(runtime) if o['kind'] == 'tool']
    assert len(cards) == 1
    body = cards[0]['body']
    assert body.index('💬 Checking') < body.index('read_file') < body.index('💬 Now building') < body.index('terminal')
    assert '✅ 0.5s' in body and '⏳' in body
    emit(runtime, 'final', assistant_response='Draft ready.', user_message='')
    assert outputs(runtime)[-1]['body'] == '🤖 Bot: Draft ready.'


def test_receipt_reactions_follow_status(runtime):
    runtime.handle_update({'update_id': 7, 'message': {'message_id': 555, 'text': 'hello',
                           'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}}})
    assert runtime.send_one()
    runtime.api.call.assert_called_with('setMessageReaction', {
        'chat_id': 123, 'message_id': 555, 'reaction': [{'type': 'emoji', 'emoji': '👀'}]})
    with runtime.store.tx() as db:
        db.execute("UPDATE inbox SET status='settled'")
    open_gate(runtime)
    assert runtime.send_one()
    assert runtime.api.call.call_args.args[1]['reaction'][0]['emoji'] == '👍'
    open_gate(runtime)
    runtime.api.call.reset_mock()
    runtime.send_one()
    assert not any(c.args[0] == 'setMessageReaction' for c in runtime.api.call.call_args_list)


def test_receipts_can_be_disabled(runtime):
    runtime.settings['receipts'] = False
    runtime.handle_update({'update_id': 8, 'message': {'message_id': 556, 'text': 'hi',
                           'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}}})
    runtime.send_one()
    assert not any(c.args[0] == 'setMessageReaction' for c in runtime.api.call.call_args_list)


def test_table_blank_header_column():
    md = '| | Amount |\n|---|---|\n| Catering | $11,400 |\n| Bar | $5,400 |'
    plain = ''.join(c['plain'] for c in render(md))
    assert '• Catering · Amount: $11,400' in plain
    assert ': Catering' not in plain
