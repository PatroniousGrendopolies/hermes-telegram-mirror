"""Shared-code isolation and explicit worker-host regressions."""
import ast
import importlib.util
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mirror_v3_test', ROOT / '__init__.py',
                                            submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pkg
spec.loader.exec_module(pkg)
config = importlib.import_module('mirror_v3_test.configuration')
Mirror = importlib.import_module('mirror_v3_test.mirror').Mirror


def setup_home(home, profile):
    folder = home / 'plugin-data' / 'telegram-mirror'
    folder.mkdir(parents=True)
    settings = dict(profile=profile, chat_id=123, allowlist=[123], host='standalone',
                    user_label='Owner', bot_label=profile)
    (folder / 'mirror.yaml').write_text(yaml.safe_dump(settings))
    return settings


def test_shared_package_reads_independent_homes(tmp_path):
    for name in ('alpha', 'beta'):
        setup_home(tmp_path / name, name)
    a = config.load_settings(tmp_path / 'alpha', 'alpha')
    b = config.load_settings(tmp_path / 'beta', 'beta')
    assert a['bot_label'] != b['bot_label']
    ma = Mirror(tmp_path / 'alpha', a, delivery=Mock())
    mb = Mirror(tmp_path / 'beta', b, delivery=Mock())
    ma.store.set('offset', 42)
    assert mb.store.get('offset') is None
    with pytest.raises(ValueError):
        config.load_settings(tmp_path / 'alpha', 'beta')


def test_standalone_config_never_launches_from_plugin(tmp_path):
    settings = setup_home(tmp_path, 'test')
    runtime = Mirror(tmp_path, settings, delivery=Mock())
    runtime._thread = Mock()
    runtime.start()
    runtime._thread.assert_not_called()


def test_standalone_leader_bypasses_gateway_probe(tmp_path, monkeypatch):
    module = importlib.import_module('mirror_v3_test.mirror')
    settings = setup_home(tmp_path, 'test')
    runtime = Mirror(tmp_path, settings, delivery=Mock())
    probe = Mock(side_effect=AssertionError('No gateway imports on standalone path'))
    monkeypatch.setattr(module, 'gateway_host', probe)
    runtime._acquire_leader = Mock(return_value=None)
    runtime._lead(standalone=True)
    runtime._acquire_leader.assert_called_once()
    probe.assert_not_called()


def test_worker_has_no_scheduler_imports():
    tree = ast.parse((ROOT / 'worker.py').read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(x.name for x in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or '')
    assert not any(x.startswith(('gateway', 'cron', 'hermes_cli')) for x in imports)
    assert 'kanban' not in imports


def test_leader_lock_prevents_second_worker(tmp_path):
    settings = setup_home(tmp_path, 'test')
    first = Mirror(tmp_path, settings, delivery=Mock())
    second = Mirror(tmp_path, settings, delivery=Mock())
    lock = first._acquire_leader()
    assert lock is not None
    import fcntl
    with open(second.store.root / 'poller.lock', 'a') as other:
        with pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock.close()
    lock = second._acquire_leader()
    assert lock is not None
    lock.close()
