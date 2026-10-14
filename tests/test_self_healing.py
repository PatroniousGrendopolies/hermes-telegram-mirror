"""Self-healing: interrupted turns resume on their own; the owner only hears about real give-ups."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if 'telegram_mirror_test' not in sys.modules:
    spec = importlib.util.spec_from_file_location('telegram_mirror_test', ROOT / '__init__.py',
                                                  submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules['telegram_mirror_test'] = package
    spec.loader.exec_module(package)

from telegram_mirror_test.compat import DeliveryError  # noqa: E402
from telegram_mirror_test.mirror import Mirror  # noqa: E402
from telegram_mirror_test.store import Store, delivery_key  # noqa: E402

OWNER = {'lease_id': 'lease-a', 'live_session_id': 'live-a'}
OK = {'status': 'settled', 'reply': 'done', 'route': 'cli'}


@pytest.fixture
def mirror(tmp_path):
    settings = dict(chat_id=123, allowlist=[123], user_label='Owner', bot_label='Test Bot',
                    profile='test', hermes_executable='/unused')
    delivery = Mock()
    delivery.session.return_value = 'canonical'
    api = Mock()
    api.send.return_value = {'message_id': 12}
    m = Mirror(tmp_path, settings, api=api, delivery=delivery)
    m.OWNER_GRACE_SECONDS = 0
    m.RESUME_DELAYS = (0, 0)
    return m


def update(uid, text):
    return {'update_id': uid, 'message': {'message_id': uid, 'from': {'id': 123},
            'chat': {'id': 123, 'type': 'private'}, 'text': text}}


def rows(m):
    with m.store.tx() as db:
        return {r['update_id']: dict(r) for r in db.execute('SELECT * FROM inbox')}


def outbox_text(m):
    with m.store.tx() as db:
        return ' '.join(r['body'] for r in db.execute('SELECT body FROM outbox'))


def test_cli_timeout_resumes_automatically_and_owner_sees_only_the_answer(mirror):
    mirror.handle_update(update(1, 'send the H1 report'))
    mirror.delivery.deliver.side_effect = [DeliveryError('CLI timed out after 600s'), OK]
    mirror.deliver_one()
    assert rows(mirror)[1]['status'] == 'pending'  # healed, not failed
    mirror.deliver_one()
    assert rows(mirror)[1]['status'] == 'settled'
    assert '😢' not in outbox_text(mirror)
    resumed_text = mirror.delivery.deliver.call_args_list[1].args[1]
    assert 'Do not repeat actions that already completed' in resumed_text


def test_resume_note_is_added_once_not_per_attempt(mirror):
    mirror.handle_update(update(1, 'task'))
    mirror.delivery.deliver.side_effect = [DeliveryError('x'), DeliveryError('y'), OK]
    for _ in range(3):
        mirror.deliver_one()
    assert rows(mirror)[1]['text'].count('Telegram mirror: an earlier attempt') == 1
    assert rows(mirror)[1]['status'] == 'settled'


def test_gives_up_after_max_resumes_and_tells_owner_once(mirror):
    mirror.handle_update(update(1, 'task'))
    mirror.handle_update(update(2, 'next'))
    mirror.delivery.deliver.side_effect = [DeliveryError('a'), DeliveryError('b'), DeliveryError('c'), OK]
    for _ in range(3):
        mirror.deliver_one()
    assert rows(mirror)[1]['status'] == 'failed'
    assert outbox_text(mirror).count('😢') == 1
    assert 'retried it 2 times' in outbox_text(mirror)
    mirror.deliver_one()
    assert rows(mirror)[2]['status'] == 'settled'


def test_resuming_turn_keeps_its_place_in_line(mirror):
    mirror.RESUME_DELAYS = (3600, 3600)
    mirror.handle_update(update(1, 'send report'))
    mirror.handle_update(update(2, 'did it go out?'))
    mirror.delivery.deliver.side_effect = [DeliveryError('x'), OK]
    mirror.deliver_one()
    assert mirror.deliver_one() is False  # #2 waits behind #1's backoff, never jumps ahead
    assert rows(mirror)[2]['status'] == 'pending'


def test_orphaned_desktop_turn_resumes_with_fresh_mailbox_key(mirror):
    mirror.handle_update(update(1, 'confirm the cocktails'))
    with mirror.store.tx() as db:
        db.execute("UPDATE inbox SET status='waiting',route='mailbox' WHERE update_id=1")
    ident = rows(mirror)[1]['id']
    mirror.delivery.receipt.return_value = {'status': 'claimed', **OWNER}
    mirror.delivery.live_owner.return_value = None
    mirror.check_receipts()
    row = rows(mirror)[1]
    assert row['status'] == 'pending' and row['attempt'] == 1
    assert delivery_key(ident, 1) != ident  # immutable ticket ids never collide
    mirror.delivery.deliver.return_value = OK
    mirror.deliver_one()
    assert rows(mirror)[1]['status'] == 'settled'


def test_receipt_lookup_uses_current_attempt_key(mirror):
    mirror.handle_update(update(1, 'task'))
    with mirror.store.tx() as db:
        db.execute("UPDATE inbox SET status='waiting',route='mailbox',attempt=1 WHERE update_id=1")
    ident = rows(mirror)[1]['id']
    mirror.delivery.receipt.return_value = {'status': 'settled', 'reply': 'ok'}
    mirror.check_receipts()
    mirror.delivery.receipt.assert_called_with(delivery_key(ident, 1))
    assert rows(mirror)[1]['status'] == 'settled'


def test_cancelled_in_desktop_is_respected_never_resumed(mirror):
    mirror.handle_update(update(1, 'task'))
    with mirror.store.tx() as db:
        db.execute("UPDATE inbox SET status='waiting',route='mailbox' WHERE update_id=1")
    mirror.delivery.receipt.return_value = {'status': 'cancelled', **OWNER}
    mirror.check_receipts()
    assert rows(mirror)[1]['status'] == 'failed'
    assert 'retried' not in outbox_text(mirror)


def test_restart_resumes_interrupted_cli_turn(mirror):
    mirror.handle_update(update(1, 'task'))
    with mirror.store.tx() as db:
        db.execute("UPDATE inbox SET status='delivering',route='cli' WHERE update_id=1")
    for ident in mirror.store.recover():
        mirror._heal_turn(ident, 'interrupted by a mirror restart')
    assert rows(mirror)[1]['status'] == 'pending'
    mirror.delivery.deliver.return_value = OK
    mirror.deliver_one()
    assert rows(mirror)[1]['status'] == 'settled'
    assert '😢' not in outbox_text(mirror)


def test_schema_migration_adds_retry_columns_to_old_store(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / 'mirror.sqlite3')
    db.executescript("CREATE TABLE inbox (id TEXT PRIMARY KEY, update_id INTEGER UNIQUE NOT NULL, "
                     "text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', route TEXT, "
                     "error TEXT, created REAL NOT NULL);"
                     "INSERT INTO inbox VALUES ('a',1,'old','pending',NULL,NULL,0);")
    db.close()
    store = Store(tmp_path)
    row = store.claim_input()
    assert row['text'] == 'old' and row['attempt'] == 0
