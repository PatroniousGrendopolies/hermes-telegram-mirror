"""Inbound media: photos, voice/audio memos, documents, video notes.

Files are downloaded from Telegram into the profile's private plugin-data folder (0600),
then described to the agent as text: images via the standard ``[Image attached at: path]``
marker that Hermes routes to vision, voice/audio via a local faster-whisper transcript
(no cloud STT, the memo never leaves the Mac), everything else as a path hint.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024  # Bot API getFile ceiling.
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.bmp', '.tif', '.tiff'}
AUDIO_EXT = {'.ogg', '.oga', '.opus', '.mp3', '.m4a', '.wav', '.aac', '.flac', '.mp4a'}
MEDIA_KEYS = ('photo', 'voice', 'audio', 'document', 'video_note', 'video', 'sticker')


def describe(msg: dict):
    """Return (kind, file_id, suggested_name, size) for the first media part, or None."""
    for key in MEDIA_KEYS:
        part = msg.get(key)
        if not part:
            continue
        if key == 'photo':  # list of sizes; take the largest
            part = max(part, key=lambda p: p.get('file_size', 0) or 0)
            return 'image', part['file_id'], 'photo.jpg', part.get('file_size')
        name = part.get('file_name') or ''
        mime = part.get('mime_type') or ''
        if key in {'voice', 'audio', 'video_note'}:
            kind = 'audio' if key != 'video_note' else 'video'
            name = name or (key + ('.ogg' if key == 'voice' else '.mp4' if key == 'video_note' else '.mp3'))
        elif key == 'document':
            ext = Path(name).suffix.lower()
            kind = 'image' if ext in IMAGE_EXT or mime.startswith('image/') else \
                   'audio' if ext in AUDIO_EXT or mime.startswith('audio/') else 'document'
            name = name or 'document.bin'
        elif key == 'sticker':
            kind, name = 'image', 'sticker.webp'
        else:
            kind, name = 'video', name or 'video.mp4'
        return kind, part['file_id'], name, part.get('file_size')
    return None


def _safe_name(name: str) -> str:
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', Path(name).name).strip('._') or 'file'
    return base[:80]


def fetch(api, msg: dict, root: Path):
    """Download the message's media. Returns (kind, path) or raises RuntimeError with a
    credential-free reason suitable for echoing back to the user."""
    info = describe(msg)
    if not info:
        return None
    kind, file_id, name, size = info
    if size and size > MAX_BYTES:
        raise RuntimeError('That file is over 20 MB, which is more than Telegram lets a bot download.')
    media_dir = root / 'media'
    media_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(media_dir, 0o700)
    remote = api.call('getFile', {'file_id': file_id}, timeout=15)
    remote_path = remote.get('file_path') or ''
    ext = Path(remote_path).suffix or Path(name).suffix
    stem = Path(_safe_name(name)).stem
    dest = media_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{msg.get('message_id', 0)}_{stem}{ext}"
    api.download(remote_path, dest, MAX_BYTES)
    return kind, dest


def transcribe(path: Path, model_name: str = 'base') -> str:
    """Local faster-whisper transcript; empty string on any failure (caller falls back)."""
    try:
        from faster_whisper import WhisperModel  # venv dependency of hermes-agent
    except Exception:
        log.warning('telegram-mirror: faster_whisper unavailable; memo attached without transcript')
        return ''
    try:
        model = WhisperModel(model_name, device='cpu', compute_type='int8')
        segments, _ = model.transcribe(str(path), vad_filter=True, beam_size=1)
        return ' '.join(s.text.strip() for s in segments if s.text.strip()).strip()
    except Exception as exc:  # never crash the poller on a bad audio file
        log.warning('telegram-mirror: transcription failed: %s', type(exc).__name__)
        return ''


def compose(kind: str, path: Path, caption: str, stt_model: str = 'base') -> str:
    """Build the text turn the agent receives for a media message."""
    caption = (caption or '').strip()
    if kind == 'image':
        head = caption or 'Patrick sent this image from Telegram. Look at it and respond.'
        return f'{head}\n\n[Image attached at: {path}]'
    if kind == 'audio':
        text = transcribe(path, stt_model)
        if text:
            body = f'Voice memo from Patrick (transcribed locally):\n\n"{text}"'
        else:
            body = 'Voice memo from Patrick (transcription unavailable). Use the audio file directly if needed.'
        if caption:
            body = f'{caption}\n\n{body}'
        return f'{body}\n\n[Audio file: {path}]'
    label = 'Video' if kind == 'video' else 'File'
    head = caption or f'Patrick sent a {label.lower()} from Telegram.'
    return f'{head}\n\n[{label} attached at: {path}]'
