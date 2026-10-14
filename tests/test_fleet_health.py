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
