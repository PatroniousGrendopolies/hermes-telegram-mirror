"""Independent, credential-free launchd mirror health check and idle rollout."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import time


def inspect(db_path, now):
    issues = []
    # Plain connect (SELECTs only): a read-only URI cannot open a WAL database whose -shm
    # file is being recreated by a just-restarted worker, which caused false alarms.
    with sqlite3.connect(db_path, timeout=10) as db:
        meta = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM meta')}
        hb = meta.get('heartbeat') or {}
        if now - hb.get('time', 0) > 180:
            issues.append('worker heartbeat stale')
        if now - (meta.get('last_poll_ok') or 0) > 180:
            issues.append('Telegram polling stale')
        for key in ('startup_error', 'worker_error'):
            if meta.get(key):
                issues.append(key.replace('_', ' '))
        if db.execute("SELECT count(*) FROM inbox WHERE status='uncertain'").fetchone()[0]:
            issues.append('inbound queue held for inspection')
        if db.execute("SELECT count(*) FROM inbox WHERE status='pending' AND created<?", (now-900,)).fetchone()[0]:
            issues.append('inbound pending over 15 minutes')
        if db.execute("SELECT count(*) FROM inbox WHERE status IN ('delivering','waiting') AND created<?", (now-3600,)).fetchone()[0]:
            issues.append('active turn over one hour')
        if db.execute("SELECT count(*) FROM outbox WHERE status='pending' AND created<?", (now-300,)).fetchone()[0]:
            issues.append('outbound pending over five minutes')
        # Only recent ones: a days-old unconfirmed send is history, not an outage to re-alert on.
        uncertain = db.execute("SELECT count(*) FROM outbox WHERE status IN ('uncertain','failed') AND kind='final' "
                               "AND created>?", (now - 86400,)).fetchone()[0]
        if uncertain:
            issues.append(f'{uncertain} final reply delivery outcomes need inspection')
        busy = db.execute("SELECT count(*) FROM inbox WHERE status IN ('delivering','waiting')").fetchone()[0]
        pending_send = db.execute("SELECT count(*) FROM outbox WHERE status IN ('pending','sending')").fetchone()[0]
    return {'issues': issues, 'pid': hb.get('pid'), 'idle': not (busy or pending_send)}


DEAD = ('worker heartbeat stale', 'Telegram polling stale', 'worker error', 'startup error')
MAX_HEALS_PER_HOUR = 3


def heal(label, profile, health, heals, now, kick=None):
    """Self-healing: restart a dead/stalled worker ourselves instead of only alerting.

    Restart is safe: interrupted turns are resumed by the mirror (same session, told not to
    redo finished actions). Rate-limited so a persistent fault (revoked token, no network)
    escalates to a notification instead of a restart loop. Returns True if it restarted."""
    if not any(i in health['issues'] for i in DEAD):
        return False
    recent = [t for t in heals.get(profile, []) if now - t < 3600]
    heals[profile] = recent
    if len(recent) >= MAX_HEALS_PER_HOUR:
        health['issues'].append('auto-restart limit reached; needs a human')
        return False
    kick = kick or (lambda l: subprocess.run(['launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/{l}'],
                                              capture_output=True, timeout=30).returncode)
    if kick(label) == 0:
        recent.append(now)
        health['healed'] = True
        return True
    return False


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--notify', action='store_true')
    args = parser.parse_args()
    now = time.time()
    state_path = args.state_dir / 'health.json'
    old = json.loads(state_path.read_text()) if state_path.exists() else {}
    rollout_path = args.state_dir / 'rollout.json'
    rollout = json.loads(rollout_path.read_text()) if rollout_path.exists() else {}
    report = {'checked_at': now, 'profiles': {}, 'last_alert': old.get('last_alert', 0),
              'heals': old.get('heals', {})}
    for path in sorted((Path.home()/'Library/LaunchAgents').glob('ai.hermes.telegram-mirror.*.plist')):
        label = path.stem
        profile = label.removeprefix('ai.hermes.telegram-mirror.')
        try:
            conf = plistlib.loads(path.read_bytes())
            argv = conf['ProgramArguments']
            home = Path(argv[argv.index('--home')+1])
            health = inspect(home/'plugin-data/telegram-mirror/mirror.sqlite3', now)
            request = rollout.get(profile)
            if request and health['pid'] != request['old_pid'] and not any(x in health['issues'] for x in ('worker heartbeat stale', 'Telegram polling stale', 'startup error', 'worker error')):
                rollout.pop(profile)
            elif request and health['idle'] and now-request.get('signalled_at',0)>120:
                # Signal only the explicitly requested old worker, never force kill an active turn.
                result = subprocess.run(['launchctl','kill','SIGTERM',f'gui/{os.getuid()}/{label}'],capture_output=True,timeout=10)
                if result.returncode == 0:
                    request['signalled_at'] = now
            if heal(label, profile, health, report['heals'], now):
                # Healed silently; only alert if the restart itself does not take.
                health['issues'] = []
            report['profiles'][profile] = health
        except Exception as exc:
            report['profiles'][profile] = {'issues': [f'health check failed ({type(exc).__name__})']}
    signature = {p:h['issues'] for p,h in report['profiles'].items() if h['issues']}
    previous = {p:h['issues'] for p,h in old.get('profiles',{}).items() if h['issues']}
    if args.notify and signature and (signature != previous or now-report['last_alert'] >= 21600):
        # Fixed AppleScript; only sanitized profile names leave this process. No tokens or message text.
        names = ', '.join(signature)
        script = 'on run argv\ndisplay notification (item 1 of argv) with title "Hermes mirror needs attention"\nend run'
        result = subprocess.run(['osascript','-e',script,f'Check: {names}. Health report saved locally.'],capture_output=True,timeout=15)
        report['notification_exit'] = result.returncode
        report['last_alert'] = now
    report['rollout_pending'] = sorted(rollout)
    save(state_path, report)
    if rollout_path.exists():
        save(rollout_path, rollout)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
