# Changelog

## 0.3.4

- A clean CLI failure (agent exited non-zero: bad model, quota, context) marks only that turn `failed`, notifies the owner on Telegram and lets the next turn through. Previously it was treated as an unknown outcome and held the queue indefinitely.
- Unknown outcomes still hold the queue, but the owner is told on Telegram and `/unhold` releases it (nothing is replayed).

## 0.3.3

- Inbound photos, voice memos, audio, documents and video notes. Images are passed as `[Image attached at:]` markers, voice/audio transcribed locally with faster-whisper (`stt_model` setting, default `base`), other files as path hints. Allowlist is enforced before any download; 20 MB streamed cap, sanitized names, 0600 files, token-free errors.

## 0.3.2

- One live activity card per turn: interim notes (💬) and tool lines share a single Telegram message edited in place, rolling over only near the size limit (was one message per interim and per tool burst).
- Receipt reactions on the owner's Telegram message: 👀 picked up, 👍 delivered into the Bot Chat, 😢 held/failed. `receipts: false` disables; emojis configurable. Rate-gated with all other sends.
- Tables with a blank header cell render the row label alone (was `• : Catering`).

## 0.3.1

- Unconfigured profiles load dormant: declared hooks register as no-ops, no threads or network I/O (lets `hermes plugins validate` run the capability probe).
- Manifest declares `provides_hooks` / `provides_tools` for catalog validation.
- Removed dynamic `__import__` (security scan now `safe`).

## 0.3.0

- Shared code checkout with profile-local configuration and SQLite state.
- Explicit standalone worker, suitable for launchd. No gateway, cron ticker or kanban dispatcher startup.
- Profile-scoped symlink discovery and independent Desktop activation.
- Additional isolation and singleton-worker regression tests.
- Clean public distribution, MIT license, installation and security documentation.

## 0.2.0

- Off-path hook processing, bounded expendable progress queues, retained finals.
- Typing refresh, interim updates, redacted batched tool cards and completion edits.
- Balanced Telegram HTML rendering and plain-text fallback after parser rejection.
- Short collision-checked inbound markers; legacy markers remain readable.
- Shared per-destination send/edit/typing rate gate.

## 0.1.0

- Bidirectional canonical Bot Chat text mirror.
- Keychain-only credentials, private-chat allowlist, persisted offset and deduplication.
- Owner-pinned Desktop mailbox with serialized CLI fallback when unowned.
- Ambiguous send/delivery outcomes held for inspection rather than replayed.
