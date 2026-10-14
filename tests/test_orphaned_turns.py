"""A bot must never stay silently stuck: orphaned Desktop turns and restarts resolve on their own.

Real incident (Corbys, Oct 13): a Desktop window claimed a Telegram turn, then went away
without writing a receipt. The row sat in 'waiting' for 14 hours and every later message
queued behind it with no warning.
"""
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

from telegram_mirror_test.mirror import Mirror  # noqa: E402

OWNER = {'lease_id': 'lease-a', 'live_session_id': 'live-a'}


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
    m.MAX_RESUMES = 0  # give-up path; self-healing is in test_self_healing.py
    return m


def update(uid, text):
    return {'update_id': uid, 'message': {'message_id': uid, 'from': {'id': 123},
            'chat': {'id': 123, 'type': 'private'}, 'text': text}}


def statuses(m):
    with m.store.tx() as db:
        return {r['update_id']: r['status'] for r in db.execute('SELECT update_id,status FROM inbox')}


def outbox_text(m):
    with m.store.tx() as db:
        return ' '.join(r['body'] for r in db.execute('SELECT body FROM outbox'))


def park_in_mailbox(m, uid):
    with m.store.tx() as db:
        db.execute("UPDATE inbox SET status='waiting',route='mailbox' WHERE update_id=?", (uid,))


def test_claimed_turn_whose_owner_left_fails_and_unblocks_queue(mirror):
    mirror.handle_update(update(1, 'send the menu'))
    mirror.handle_update(update(2, 'next thing'))
    park_in_mailbox(mirror, 1)
    mirror.delivery.receipt.return_value = {'status': 'claimed', **OWNER}
    mirror.delivery.live_owner.return_value = None  # Desktop window is gone
    assert mirror.deliver_one() is False  # still blocked before the check runs
    mirror.check_receipts()
    assert statuses(mirror)[1] == 'failed'
    assert "didn't finish" in outbox_text(mirror)
    mirror.delivery.deliver.return_value = {'status': 'settled', 'reply': 'ok', 'route': 'cli'}
    assert mirror.deliver_one() is True
    assert statuses(mirror)[2] == 'settled'
    mirror.delivery.deliver.assert_called_once()  # the orphaned turn was NOT replayed


def test_claimed_turn_with_live_owner_keeps_waiting(mirror):
    mirror.handle_update(update(1, 'long task'))
    park_in_mailbox(mirror, 1)
    mirror.delivery.receipt.return_value = {'status': 'claimed', **OWNER}
    mirror.delivery.live_owner.return_value = dict(OWNER, profile_home='x', session_id='s')
    mirror.check_receipts()
    assert statuses(mirror)[1] == 'waiting'


def test_new_desktop_lease_does_not_count_as_owner(mirror):
    mirror.handle_update(update(1, 'task'))
    park_in_mailbox(mirror, 1)
    mirror.delivery.receipt.return_value = {'status': 'claimed', **OWNER}
    mirror.delivery.live_owner.return_value = {'lease_id': 'lease-b', 'live_session_id': 'live-b'}
    mirror.check_receipts()
    assert statuses(mirror)[1] == 'failed'


def test_unclaimed_ticket_with_owner_gone_is_requeued_not_failed(mirror):
    mirror.handle_update(update(1, 'never started'))
    park_in_mailbox(mirror, 1)
    mirror.delivery.receipt.return_value = {'status': 'queued', **OWNER}
    mirror.delivery.live_owner.return_value = None
    mirror.check_receipts()
    assert statuses(mirror)[1] == 'pending'


def test_grace_period_tolerates_brief_owner_blips(mirror):
    mirror.OWNER_GRACE_SECONDS = 3600
    mirror.handle_update(update(1, 'task'))
    park_in_mailbox(mirror, 1)
    mirror.delivery.receipt.return_value = {'status': 'claimed', **OWNER}
    mirror.delivery.live_owner.return_value = None
    mirror.check_receipts()
    assert statuses(mirror)[1] == 'waiting'


def test_failed_receipt_fails_turn_instead_of_holding(mirror):
    mirror.handle_update(update(1, 'task'))
    mirror.handle_update(update(2, 'next'))
    park_in_mailbox(mirror, 1)
    mirror.delivery.receipt.return_value = {'status': 'failed', **OWNER}
    mirror.check_receipts()
    assert statuses(mirror)[1] == 'failed'
    mirror.delivery.deliver.return_value = {'status': 'settled', 'reply': 'ok', 'route': 'cli'}
    assert mirror.deliver_one() is True


def test_restart_fails_interrupted_cli_turn_and_keeps_mailbox_waiting(mirror):
    mirror.handle_update(update(1, 'cli turn'))
    mirror.handle_update(update(2, 'mailbox turn'))
    with mirror.store.tx() as db:
        db.execute("UPDATE inbox SET status='delivering',route='cli' WHERE update_id=1")
        db.execute("UPDATE inbox SET status='delivering',route='mailbox' WHERE update_id=2")
    interrupted = mirror.store.recover()
    assert len(interrupted) == 1
    assert statuses(mirror) == {1: 'failed', 2: 'waiting'}


def test_unexpected_exception_on_cli_route_does_not_hold(mirror):
    mirror.handle_update(update(1, 'boom'))
    mirror.handle_update(update(2, 'next'))
    mirror.delivery.deliver.side_effect = [RuntimeError('x'),
                                           {'status': 'settled', 'reply': 'ok', 'route': 'cli'}]
    mirror.deliver_one()
    assert statuses(mirror)[1] == 'failed'
    assert mirror.deliver_one() is True
    assert statuses(mirror)[2] == 'settled'
