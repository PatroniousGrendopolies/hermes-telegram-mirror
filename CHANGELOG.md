# Changelog

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
