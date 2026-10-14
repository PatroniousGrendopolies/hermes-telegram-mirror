"""Health checks inspect queue metadata, not private message text."""
import importlib.util
import json
from pathlib import Path
import sqlite3

spec = importlib.util.spec_from_file_location('fleet_health', Path(__file__).resolve().parents[1]/'fleet_health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


def database(tmp_path):
    path = tmp_path/'mirror.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE meta(key TEXT,value TEXT); CREATE TABLE inbox(status TEXT,created REAL); CREATE TABLE outbox(status TEXT,created REAL,kind TEXT);')
        db.executemany('INSERT INTO meta VALUES (?,?)', [('heartbeat',json.dumps({'pid':123,'time':1000})),('last_poll_ok','1000')])
    return path


def test_healthy_idle(tmp_path):
    result = health.inspect(database(tmp_path), 1050)
    assert result == {'issues': [], 'pid':123, 'idle':True}


def test_detects_dead_poll_despite_live_heartbeat(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET value='0' WHERE key='last_poll_ok'")
    assert health.inspect(path,1050)['issues'] == ['Telegram polling stale']


def test_active_work_not_eligible_for_rollout(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO inbox VALUES ('delivering',1000)")
    assert not health.inspect(path,1050)['idle']


def test_held_and_unsent_are_visible(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO inbox VALUES ('uncertain',1000)")
        db.execute("INSERT INTO outbox VALUES ('uncertain',1000,'final')")
        db.execute("INSERT INTO outbox VALUES ('pending',500,'final')")
    result = health.inspect(path,1050)
    assert 'inbound queue held for inspection' in result['issues']
    assert 'outbound pending over five minutes' in result['issues']
    assert '1 final reply delivery outcomes need inspection' in result['issues']
    assert not result['idle']


def test_report_permissions(tmp_path):
    path = tmp_path/'health.json'
    health.save(path, {'ok':True})
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {'ok':True}


def test_dead_worker_is_restarted_not_just_reported():
    import fleet_health as fh
    kicked = []
    health = {'issues': ['worker heartbeat stale']}
    assert fh.heal('lbl', 'corbys', health, {}, 1000.0, kick=lambda l: kicked.append(l) or 0)
    assert kicked == ['lbl'] and health['healed']


def test_healthy_worker_is_never_restarted():
    import fleet_health as fh
    kicked = []
    assert not fh.heal('lbl', 'p', {'issues': ['outbound pending over five minutes']}, {}, 0.0,
                       kick=lambda l: kicked.append(l) or 0)
    assert kicked == []


def test_restart_loop_is_capped_and_escalates():
    import fleet_health as fh
    heals = {'p': [900.0, 950.0, 990.0]}
    health = {'issues': ['Telegram polling stale']}
    assert not fh.heal('lbl', 'p', health, heals, 1000.0, kick=lambda l: 0)
    assert 'auto-restart limit reached; needs a human' in health['issues']


def test_old_restarts_age_out_of_the_cap():
    import fleet_health as fh
    heals = {'p': [0.0, 1.0, 2.0]}
    assert fh.heal('lbl', 'p', {'issues': ['worker error']}, heals, 10000.0, kick=lambda l: 0)
    assert heals['p'] == [10000.0]
