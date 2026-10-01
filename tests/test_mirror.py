"""Hermetic tests: no network, Keychain, agent turns, or real profile writes."""
import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('telegram_mirror_test', ROOT / '__init__.py',
                                             submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
from telegram_mirror_test.mirror import Mirror
from telegram_mirror_test.store import Store, marker
from telegram_mirror_test.transport import APIError, Telegram, UncertainSend, chunks, scrub
from telegram_mirror_test.compat import Delivery, resolve_session


@pytest.fixture
def mirror(tmp_path):
    settings = dict(chat_id=123, allowlist=[123], user_label='Owner', bot_label='Test Bot',
                    profile='test', hermes_executable='/unused')
    delivery = Mock()
    delivery.session.return_value = 'canonical'
    delivery.deliver.return_value = {'status': 'settled', 'reply': 'mirror OK', 'route': 'cli'}
    api = Mock()
    api.send.return_value = {'message_id': 12}
    return Mirror(tmp_path, settings, api=api, delivery=delivery)


def update(uid=1, text='hello', sender=123, chat=123, kind='private'):
    return {'update_id': uid, 'message': {'from': {'id': sender},
            'chat': {'id': chat, 'type': kind}, 'text': text}}


def rows(mirror, table):
    with mirror.store.tx() as db:
        return [dict(row) for row in db.execute(f'SELECT * FROM {table}')]


def test_session_filter_and_durable_turn_dedupe(mirror):
    assert not mirror.on_turn(session_id='other', turn_id='one', user_message='secret', assistant_response='no')
    assert not rows(mirror, 'outbox')
    assert mirror.on_turn(session_id='canonical', turn_id='one', user_message='hello', assistant_response='reply')
    assert not mirror.on_turn(session_id='canonical', turn_id='one', user_message='hello', assistant_response='reply')
    assert [r['body'] for r in rows(mirror, 'outbox')] == ['👤 Owner (Desktop): hello', '🤖 Test Bot: reply']
    assert mirror.on_turn(session_id='canonical', turn_id='two', user_message='hello', assistant_response='reply')
    assert len(rows(mirror, 'outbox')) == 4  # Same text on distinct turns is not lost.


@pytest.mark.parametrize('text', ['', 'a', 'a' * 4000, 'a' * 4001, '✨😀\n<&' * 3000],
                         ids=['empty', 'short', 'boundary', 'overflow', 'unicode'])
def test_chunking(text):
    parts = chunks(text)
    assert ''.join(parts) == text
    assert all(len(p.encode('utf-16-le')) // 2 <= 4000 for p in parts)


@pytest.mark.parametrize('sender,chat,kind', [(999, 123, 'private'), (123, 999, 'private'), (123, 123, 'group')])
def test_allowlist_and_route(mirror, caplog, sender, chat, kind):
    assert mirror.handle_update(update(text='DO NOT LOG THIS', sender=sender, chat=chat, kind=kind)) is None
    assert mirror.store.offset() == 2
    assert rows(mirror, 'inbox') == [] and rows(mirror, 'outbox') == []
    assert 'DO NOT LOG THIS' not in caplog.text


def test_loop_prevention_hook_and_stdout_dedupe(mirror):
    identity = mirror.handle_update(update())
    row = rows(mirror, 'inbox')[0]
    assert mirror.on_turn(session_id='canonical', turn_id='t', user_message=marker(row), assistant_response='mirror OK')
    mirror.store.reply_once(identity, 'noisy stdout must not win', 'Test Bot')
    assert [r['body'] for r in rows(mirror, 'outbox')] == ['🤖 Test Bot: mirror OK']
    assert rows(mirror, 'inbox')[0]['status'] == 'settled'


def test_fallback_before_hook_also_dedupes(mirror):
    identity = mirror.handle_update(update())
    row = rows(mirror, 'inbox')[0]
    mirror.store.reply_once(identity, 'mirror OK', 'Test Bot')
    mirror.on_turn(session_id='canonical', turn_id='t', user_message=marker(row), assistant_response='mirror OK')
    assert len(rows(mirror, 'outbox')) == 1


def test_unknown_origin_marker_does_not_suppress_user(mirror):
    mirror.on_turn(session_id='canonical', turn_id='t', user_message='📱 via Telegram [unknown]:\nhello', assistant_response='OK')
    assert len(rows(mirror, 'outbox')) == 2


def test_offset_persistence_and_duplicate_updates(mirror):
    identity = mirror.handle_update(update(55))
    mirror.handle_update(update(55))
    mirror.handle_update(update(10))
    mirror.handle_update(update(-1))
    restarted = Store(mirror.store.root)
    assert restarted.offset() == 56
    assert len([r for r in rows(mirror, 'inbox') if r['id'] == identity]) == 1


@pytest.mark.parametrize('command', ['/start', '/status', '/status@TestBot'])
def test_local_commands(mirror, command):
    mirror.handle_update(update(text=command))
    mirror.handle_update(update(text=command))
    assert not mirror.deliver_one()
    mirror.delivery.deliver.assert_not_called()
    assert len(rows(mirror, 'outbox')) == 1


def test_other_slash_command_is_conversational(mirror):
    mirror.handle_update(update(text='/something'))
    assert mirror.deliver_one()
    assert '/something' in mirror.delivery.deliver.call_args.args[1]


def test_same_handler_serial_delivery_and_reply_once(mirror):
    mirror.handle_update(update())
    mirror.handle_update(update())
    assert mirror.deliver_one()
    assert not mirror.deliver_one()
    mirror.delivery.deliver.assert_called_once()
    assert [r['body'] for r in rows(mirror, 'outbox')] == ['🤖 Test Bot: mirror OK']


def test_crash_windows_do_not_replay(mirror):
    mirror.handle_update(update())
    mirror.store.claim_input()
    mirror.store.enqueue_text('reply', 'OK')
    mirror.store.claim_output()
    mirror.store.recover()
    assert not mirror.deliver_one() and not mirror.send_one()
    assert rows(mirror, 'inbox')[0]['status'] == 'uncertain'
    assert rows(mirror, 'outbox')[0]['status'] == 'uncertain'


def test_mailbox_wait_does_not_fall_back_or_run_second_input(mirror):
    mirror.delivery.deliver.side_effect = lambda row, text: (
        mirror.store.finish_input(row['id'], 'waiting', 'mailbox') or {'status': 'waiting'})
    mirror.handle_update(update(1))
    mirror.handle_update(update(2))
    assert mirror.deliver_one()
    assert not mirror.deliver_one()
    mirror.delivery.deliver.assert_called_once()
    assert rows(mirror, 'outbox') == []
    mirror.delivery.receipt.return_value = {'status': 'settled', 'reply': 'done'}
    mirror.check_receipts()
    assert len(rows(mirror, 'outbox')) == 1


def test_rate_limit_persists_retry_after(mirror):
    mirror.store.enqueue_text('test', 'hello')
    mirror.api.send.side_effect = APIError(429, 'Too Many Requests', 30)
    assert mirror.send_one()
    assert not mirror.send_one()
    row = rows(mirror, 'outbox')[0]
    assert row['status'] == 'pending' and row['attempts'] == 1
    with mirror.store.tx() as db:
        db.execute('UPDATE outbox SET ready=0')
    mirror.store.set('next_send_at', 0)
    mirror.api.send.side_effect = None
    assert mirror.send_one()
    assert rows(mirror, 'outbox')[0]['message_id'] == 12


def test_ambiguous_send_never_retries(mirror):
    mirror.store.enqueue_text('test', 'hello')
    mirror.api.send.side_effect = UncertainSend('timeout')
    assert mirror.send_one()
    assert not mirror.send_one()
    mirror.api.send.assert_called_once()
    assert rows(mirror, 'outbox')[0]['status'] == 'uncertain'


def test_scrubbing():
    from urllib.parse import quote
    token = '123456789:' + 'x' * 35
    for value in [token, f'https://api.telegram.org/bot{token}/getMe', quote(token, safe='')]:
        assert token not in scrub(value, token)
        assert quote(token, safe='') not in scrub(value, token)
        assert '[REDACTED]' in scrub(value, token)
    assert token not in scrub(token)


def test_transport_suppresses_credential_url(monkeypatch):
    token = '123456789:' + 'x' * 35
    monkeypatch.setattr('subprocess.run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=token.encode()))
    api = Telegram('test', 'test', 123)
    api._opener = Mock()
    api._opener.open.side_effect = OSError(f'https://api.telegram.org/bot{token}/getMe')
    with pytest.raises(UncertainSend) as error:
        api.call('getMe')
    assert token not in str(error.value)
    with pytest.raises(ValueError, match='destination'):
        api.call('sendMessage', {'chat_id': 456, 'text': 'no'})


def test_resolver_fails_closed_on_ambiguity(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / 'state.db')
    db.execute('CREATE TABLE sessions(id,title,hidden,archived)')
    db.executemany('INSERT INTO sessions VALUES (?,?,?,?)', [('a','Bot Chat',1,0), ('b','Bot Chat',1,0)])
    db.commit()
    db.close()
    with pytest.raises(Exception, match='exactly one'):
        resolve_session(tmp_path)


def test_live_owner_adapter_never_calls_cli_after_admission(mirror, monkeypatch):
    home = mirror.home
    live = {'session_id': 'canonical', 'profile_home': str(home)}
    mailbox = SimpleNamespace(find_canonical_owner=lambda h: live,
                              find_canonical_live_owner=lambda h: live,
                              deliver_to_live_owner=Mock())
    monkeypatch.setitem(sys.modules, 'tools', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'tools.bot_live_delivery', mailbox)
    delivery = Delivery(home, mirror.settings, mirror.store, mirror.stop)
    delivery.session = lambda: 'canonical'
    delivery.run_cli = Mock()
    mirror.handle_update(update())
    row = mirror.store.claim_input()
    assert delivery.deliver(row, marker(row))['status'] == 'waiting'
    delivery.run_cli.assert_not_called()
    assert rows(mirror, 'inbox')[0]['route'] == 'mailbox'


def test_cli_turn_does_not_inherit_host_approval_bypass(mirror, monkeypatch, tmp_path):
    # The worker can lead inside any Hermes process; a --yolo/-z host must not leak its bypass.
    monkeypatch.setenv('HERMES_YOLO_MODE', '1')
    monkeypatch.setenv('HERMES_ACCEPT_HOOKS', '1')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs['env'])
        return SimpleNamespace(returncode=0, stdout='ok')

    monkeypatch.setattr('subprocess.run', fake_run)
    delivery = Delivery(mirror.home, mirror.settings, mirror.store, mirror.stop)
    assert delivery.run_cli('canonical', 'hello')['status'] == 'settled'
    assert 'HERMES_YOLO_MODE' not in seen
    assert 'HERMES_ACCEPT_HOOKS' not in seen


def test_exclusive_poller_lock(tmp_path):
    import fcntl
    with open(tmp_path / 'poller.lock', 'a') as a, open(tmp_path / 'poller.lock', 'a') as b:
        fcntl.flock(a, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(b, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_hot_reload_waits_for_old_leader(mirror):
    import threading
    old = mirror._acquire_leader()
    obtained = []
    waiter = threading.Thread(target=lambda: obtained.append(mirror._acquire_leader()))
    waiter.start()
    waiter.join(timeout=0.1)
    assert waiter.is_alive() and not obtained
    old.close()
    waiter.join(timeout=2)
    assert not waiter.is_alive() and len(obtained) == 1
    obtained[0].close()


@pytest.mark.parametrize('argv', [['hermes', 'chat'], ['hermes', 'serve'], ['hermes', 'plugins', 'list']])
def test_non_gateway_hosts_never_start_workers(mirror, monkeypatch, argv):
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(get_process_hermes_home=lambda: mirror.home))
    mirror.start()
    assert mirror._threads == []


def test_compressed_session_override_follows_tip(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / 'state.db')
    db.execute('CREATE TABLE sessions(id,title,hidden,archived)')
    db.executemany('INSERT INTO sessions VALUES (?,?,?,?)', [('old',None,1,0), ('tip','Bot Chat',1,0)])
    db.commit()
    db.close()
    state = Mock()
    state.get_compression_tip.return_value = 'tip'
    monkeypatch.setitem(sys.modules, 'hermes_state', SimpleNamespace(SessionDB=lambda **kw: state))
    assert resolve_session(tmp_path, 'old') == 'tip'
    assert resolve_session(tmp_path) == 'tip'


def test_wrong_profile_registration_is_rejected(mirror, monkeypatch):
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(get_hermes_home=lambda: mirror.home))
    (mirror.store.root / 'mirror.yaml').write_text('profile: test\n')
    with pytest.raises(ValueError, match='different profile'):
        package.register(SimpleNamespace(profile_name='wrong-profile'))
