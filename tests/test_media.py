"""Inbound media admission: photos, voice memos, documents. No network, no whisper."""
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
from telegram_mirror_test import media


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    settings = dict(chat_id=123, allowlist=[123], user_label='Owner', bot_label='Test Bot',
                    profile='test', hermes_executable='/unused')
    delivery = Mock()
    delivery.session.return_value = 'canonical'
    api = Mock()
    api.call.return_value = {'file_path': 'voice/file_1.oga'}

    def fake_download(file_path, dest, max_bytes, timeout=60):
        import os
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, b'fake-bytes'); os.close(fd)
    api.download.side_effect = fake_download
    monkeypatch.setattr(media, 'transcribe', lambda path, model='base': 'remind me about payroll')
    return Mirror(tmp_path, settings, api=api, delivery=delivery)


def inbox(mirror):
    with mirror.store.tx() as db:
        return [dict(r) for r in db.execute('SELECT * FROM inbox')]


def msg(uid, **parts):
    return {'update_id': uid, 'message': {'message_id': uid, 'from': {'id': 123},
            'chat': {'id': 123, 'type': 'private'}, **parts}}


def test_voice_memo_is_transcribed_into_turn(mirror):
    mirror.handle_update(msg(1, voice={'file_id': 'v1', 'file_size': 100, 'mime_type': 'audio/ogg'}))
    row = inbox(mirror)[0]
    assert 'remind me about payroll' in row['text']
    assert '[Audio file:' in row['text']
    assert row['status'] == 'pending'
    saved = list((mirror.store.root / 'media').iterdir())
    assert len(saved) == 1 and saved[0].suffix == '.oga'
    assert (saved[0].stat().st_mode & 0o777) == 0o600
    mirror.api.call.assert_called_with('getFile', {'file_id': 'v1'}, timeout=15)


def test_photo_with_caption_uses_image_marker(mirror):
    mirror.api.call.return_value = {'file_path': 'photos/file_2.jpg'}
    mirror.handle_update(msg(2, photo=[{'file_id': 'small', 'file_size': 1}, {'file_id': 'big', 'file_size': 9}],
                             caption='what is this?'))
    row = inbox(mirror)[0]
    assert row['text'].startswith('what is this?')
    assert '[Image attached at:' in row['text'] and row['text'].rstrip().endswith('.jpg]')
    mirror.api.call.assert_called_with('getFile', {'file_id': 'big'}, timeout=15)


def test_document_is_attached_as_file(mirror):
    mirror.api.call.return_value = {'file_path': 'documents/file_3.pdf'}
    mirror.handle_update(msg(3, document={'file_id': 'd', 'file_name': '../../etc/passwd Invoice.pdf'}))
    row = inbox(mirror)[0]
    assert '[File attached at:' in row['text']
    assert '..' not in Path(row['text'].split('[File attached at: ')[1].rstrip(']')).name


def test_oversize_is_refused_without_download(mirror):
    mirror.handle_update(msg(4, document={'file_id': 'd', 'file_name': 'huge.zip', 'file_size': 50 * 1024 * 1024}))
    row = inbox(mirror)[0]
    assert row['text'] == '' and row['status'] == 'local'
    with mirror.store.tx() as db:
        out = [dict(r) for r in db.execute('SELECT * FROM outbox')]
    assert any('20 MB' in str(o) for o in out)
    mirror.api.download.assert_not_called()


def test_download_failure_is_credential_free(mirror):
    mirror.api.download.side_effect = RuntimeError('Could not download that file from Telegram.')
    mirror.handle_update(msg(5, voice={'file_id': 'v', 'file_size': 10}))
    row = inbox(mirror)[0]
    assert row['text'] == ''
    assert 'bot' not in str(row).lower() or 'token' not in str(row).lower()


def test_unauthorized_media_still_ignored(mirror):
    u = msg(6, voice={'file_id': 'v', 'file_size': 10})
    u['message']['from']['id'] = 999
    assert mirror.handle_update(u) is None
    mirror.api.download.assert_not_called()
