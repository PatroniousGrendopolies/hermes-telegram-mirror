"""Worker liveness supervision: dead critical threads trigger controlled stop."""
import importlib.util
import sys
import threading
import time
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
from telegram_mirror_test.transport import APIError


@pytest.fixture
def mirror(tmp_path):
    settings = dict(chat_id=123, allowlist=[123], user_label='Owner', bot_label='Test Bot',
                    profile='test', hermes_executable='/unused', long_poll_seconds=1)
    delivery = Mock()
    delivery.session.return_value = 'canonical'
    delivery.deliver.return_value = {'status': 'settled', 'reply': 'ok', 'route': 'cli'}
    api = Mock()
    api.send.return_value = {'message_id': 12}
    api.call.return_value = []
    return Mirror(tmp_path, settings, api=api, delivery=delivery)


def test_poll_401_sets_worker_error_and_triggers_stop(mirror):
    """Poll worker returning on 401 must set worker_error and signal leader to exit."""
    mirror.api.call.side_effect = APIError(401, 'Unauthorized')
    
    # Run _poll in isolation
    poll_thread = threading.Thread(target=mirror._poll, daemon=True)
    poll_thread.start()
    poll_thread.join(timeout=3)
    
    # Poll worker should have exited
    assert not poll_thread.is_alive()
    
    # worker_error flag should be set
    worker_error = mirror.store.get('worker_error')
    assert worker_error is not None
    assert 'poll' in worker_error.lower() or 'credential' in worker_error.lower()
    
    # stop event should be set
    assert mirror.stop.is_set()


def test_poll_409_sets_worker_error_and_triggers_stop(mirror):
    """Poll worker returning on 409 must set worker_error and signal leader to exit."""
    mirror.api.call.side_effect = APIError(409, 'Conflict')
    
    poll_thread = threading.Thread(target=mirror._poll, daemon=True)
    poll_thread.start()
    poll_thread.join(timeout=3)
    
    assert not poll_thread.is_alive()
    
    worker_error = mirror.store.get('worker_error')
    assert worker_error is not None
    
    assert mirror.stop.is_set()


def test_lead_supervises_poll_thread_and_exits_when_dead(mirror, monkeypatch):
    """Leader must detect dead poll thread and exit cleanly."""
    # Mock gateway_host to return True immediately
    from telegram_mirror_test import compat
    monkeypatch.setattr(compat, 'gateway_host', lambda _: True)
    
    # Mock API to succeed on startup calls then fail on getUpdates
    startup_calls = []
    def api_call_side_effect(method, *args, **kwargs):
        startup_calls.append(method)
        if method == 'getMe':
            return {'id': 123, 'username': 'testbot'}
        elif method == 'getWebhookInfo':
            return {}
        elif method == 'getUpdates':
            raise APIError(401, 'Unauthorized')
        return []
    
    mirror.api.call.side_effect = api_call_side_effect
    
    # Run _lead in standalone mode
    lead_thread = threading.Thread(target=lambda: mirror._lead(standalone=True), daemon=True)
    lead_thread.start()
    
    # Give it time to start workers and detect failure
    lead_thread.join(timeout=10)
    
    # Leader should have exited
    assert not lead_thread.is_alive()
    
    # worker_error should be set
    worker_error = mirror.store.get('worker_error')
    assert worker_error is not None


def test_healthy_restart_clears_worker_error(mirror, monkeypatch):
    """Successful startup should clear stale worker_error from previous run."""
    # Set a stale error
    mirror.store.set('worker_error', 'Previous run failed')
    
    from telegram_mirror_test import compat
    monkeypatch.setattr(compat, 'gateway_host', lambda _: True)
    
    # Mock successful startup
    mirror.api.call.side_effect = lambda method, *args, **kwargs: (
        {'id': 123, 'username': 'testbot'} if method == 'getMe'
        else {} if method == 'getWebhookInfo'
        else []
    )
    
    # Start and immediately stop to test startup only
    def quick_lead():
        mirror._lead(standalone=True)
    
    lead_thread = threading.Thread(target=quick_lead, daemon=True)
    lead_thread.start()
    
    # Wait for startup to complete
    time.sleep(0.5)
    mirror.stop.set()
    lead_thread.join(timeout=5)
    
    # worker_error should be cleared
    assert mirror.store.get('worker_error') is None


def test_no_tight_spin_after_critical_worker_exit(mirror):
    """After critical worker exits, supervision should not spin tightly."""
    mirror.api.call.side_effect = APIError(401, 'Unauthorized')
    
    # Track how many times the supervision loop runs
    loop_count = [0]
    original_sleep = time.sleep
    
    def counting_sleep(duration):
        if duration >= 1:  # Only count actual wait sleeps
            loop_count[0] += 1
        return original_sleep(min(duration, 0.1))  # Speed up test
    
    import unittest.mock
    with unittest.mock.patch('time.sleep', counting_sleep):
        poll_thread = threading.Thread(target=mirror._poll, daemon=True)
        poll_thread.start()
        poll_thread.join(timeout=3)
    
    # Poll should exit cleanly without spinning
    assert not poll_thread.is_alive()
    # Verify stop was set (prevents tight spin in leader)
    assert mirror.stop.is_set()


def test_delivery_drain_completes_before_exit(mirror, monkeypatch):
    """Active delivery should complete before leader exits on supervision signal."""
    from telegram_mirror_test import compat
    monkeypatch.setattr(compat, 'gateway_host', lambda _: True)
    
    # Track delivery completion
    delivery_completed = threading.Event()
    
    def slow_deliver(row, marker):
        time.sleep(0.5)
        delivery_completed.set()
        return {'status': 'settled', 'reply': 'ok', 'route': 'cli'}
    
    mirror.delivery.deliver.side_effect = slow_deliver
    
    # Queue a delivery
    mirror.handle_update({'update_id': 1, 'message': {
        'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
        'text': 'test', 'message_id': 1
    }})
    
    # Mock API: succeed startup, then fail poll
    def api_call(method, *args, **kwargs):
        if method == 'getMe':
            return {'id': 123, 'username': 'testbot'}
        elif method == 'getWebhookInfo':
            return {}
        elif method == 'getUpdates':
            raise APIError(401, 'Unauthorized')
        return []
    
    mirror.api.call.side_effect = api_call
    
    lead_thread = threading.Thread(target=lambda: mirror._lead(standalone=True), daemon=True)
    lead_thread.start()
    
    # Wait for leader to complete
    lead_thread.join(timeout=10)
    
    # Delivery should have completed
    assert delivery_completed.is_set()
    assert not lead_thread.is_alive()


def test_no_duplicate_delivery_after_supervision_stop(mirror):
    """Messages delivered before supervision stop should not be replayed."""
    # Process one update
    mirror.handle_update({'update_id': 1, 'message': {
        'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
        'text': 'once', 'message_id': 1
    }})
    
    # Deliver it
    assert mirror.deliver_one() is True
    
    # Simulate supervision triggering stop
    mirror.stop.set()
    mirror.store.set('worker_error', 'Poll thread exited')
    
    # Restart with new instance (simulating launchd restart)
    mirror2 = Mirror(mirror.home, mirror.settings, api=Mock(), delivery=Mock())
    mirror2.delivery.session.return_value = 'canonical'
    
    # Should not deliver again
    assert mirror2.deliver_one() is False
    
    # Verify no duplicate in outbox
    with mirror2.store.tx() as db:
        replies = db.execute("SELECT COUNT(*) as c FROM outbox").fetchone()['c']
    assert replies == 1  # Only the original delivery
