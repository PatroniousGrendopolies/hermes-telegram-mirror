"""Clean CLI failures must not wedge the queue; genuine unknown outcomes hold until /unhold."""
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

from telegram_mirror_test.mirror import Mirror
from telegram_mirror_test.compat import DeliveryError


@pytest.fixture
def mirror(tmp_path):
    settings = dict(chat_id=123, allowlist=[123], user_label='Owner', bot_label='Test Bot',
                    profile='test', hermes_executable='/unused')
    delivery = Mock()
    delivery.session.return_value = 'canonical'
    api = Mock()
    api.send.return_value = {'message_id': 12}
    return Mirror(tmp_path, settings, api=api, delivery=delivery)


def update(uid, text):
    return {'update_id': uid, 'message': {'message_id': uid, 'from': {'id': 123},
            'chat': {'id': 123, 'type': 'private'}, 'text': text}}


def statuses(mirror):
    with mirror.store.tx() as db:
        return {r['update_id']: r['status'] for r in db.execute('SELECT update_id,status FROM inbox')}


def outbox_text(mirror):
    with mirror.store.tx() as db:
        return ' '.join(r['body'] for r in db.execute('SELECT body FROM outbox'))


def test_clean_cli_failure_does_not_block_next_turn(mirror):
    mirror.handle_update(update(1, 'first'))
    mirror.handle_update(update(2, 'second'))
    mirror.delivery.deliver.side_effect = [DeliveryError('CLI exited 1; inspect profile logs before resending'),
                                           {'status': 'settled', 'reply': 'ok', 'route': 'cli'}]
    assert mirror.deliver_one() is True
    assert statuses(mirror)[1] == 'failed'
    assert 'could not run that turn' in outbox_text(mirror)
    assert mirror.deliver_one() is True
    assert statuses(mirror)[2] == 'settled'


def test_cli_timeout_fails_turn_and_does_not_block_next(mirror):
    mirror.handle_update(update(1, 'first'))
    mirror.handle_update(update(2, 'second'))
    mirror.delivery.deliver.side_effect = [
        DeliveryError('CLI timed out after 600s and was stopped; not automatically retried'),
        {'status': 'settled', 'reply': 'ok', 'route': 'cli'}]
    assert mirror.deliver_one() is True
    assert statuses(mirror)[1] == 'failed'
    assert 'could not run that turn' in outbox_text(mirror)
    assert mirror.deliver_one() is True
    assert statuses(mirror)[2] == 'settled'
    assert mirror.delivery.deliver.call_count == 2  # timed-out turn never replayed


def test_pre_submission_refusal_fails_turn_without_holding(mirror):
    mirror.handle_update(update(1, 'first'))
    mirror.handle_update(update(2, 'second'))
    mirror.delivery.deliver.side_effect = [
        DeliveryError('Bot Chat has an unsupported live owner; no turn submitted'),
        {'status': 'settled', 'reply': 'ok', 'route': 'cli'}]
    mirror.deliver_one()
    assert statuses(mirror)[1] == 'failed'
    assert mirror.deliver_one() is True  # queue keeps moving, no /unhold needed
    assert statuses(mirror)[2] == 'settled'
    assert mirror.delivery.deliver.call_count == 2


def test_unhold_still_releases_legacy_held_rows(mirror):
    mirror.handle_update(update(1, 'first'))
    mirror.handle_update(update(2, 'second'))
    with mirror.store.tx() as db:
        db.execute("UPDATE inbox SET status='uncertain' WHERE update_id=1")
    assert mirror.deliver_one() is False  # legacy hold
    mirror.handle_update(update(3, '/unhold'))
    assert statuses(mirror)[1] == 'failed'
    mirror.delivery.deliver.return_value = {'status': 'settled', 'reply': 'ok', 'route': 'cli'}
    assert mirror.deliver_one() is True
    assert statuses(mirror)[2] == 'settled'
    assert 'Released 1 held turn' in outbox_text(mirror)
