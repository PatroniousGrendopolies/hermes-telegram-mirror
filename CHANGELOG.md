# Changelog

## 0.3.7

- Watchdog opens mirror databases read-write (SELECT only) instead of a read-only URI, which failed on a just-restarted worker's WAL and raised false alarms.
- Unconfirmed final replies only alert for 24 h instead of forever.
- launchd `ExitTimeOut` should exceed `turn_timeout_seconds` (now 1260 s) so a restart lets the current turn finish.

## 0.3.6

- **Self-healing turns.** A turn that is interrupted (CLI timeout or error, mirror restart, Desktop window closing mid-turn, failed/ambiguous receipt) now resumes automatically in the same Bot Chat session, up to 2 times with backoff (20 s, 2 min). The resumed prompt tells the agent to check the conversation for what already happened and not repeat completed actions. The owner hears about it only if all attempts fail. A turn cancelled in Desktop is never resumed.
- A resuming turn keeps its place in line, so later messages that depend on it never jump ahead.
- Each resume gets its own mailbox ticket id (tickets are immutable per id); receipts are read under the current attempt's key.
- **Self-healing workers.** `fleet_health.py` now restarts a worker whose heartbeat or polling is stale, or that recorded a worker/startup error, instead of only alerting. Capped at 3 restarts per hour per bot; past that it escalates to a Mac notification.
- Default `turn_timeout_seconds` raised 600 → 1200: real multi-tool turns (reports, emails) were being killed at 10 min.

## 0.3.5

- Orphaned Desktop turns no longer freeze a bot. If the Desktop window that claimed a Telegram turn goes away without a receipt, after a 2 minute grace the turn fails visibly (never replayed) and the queue moves on; a ticket that was never claimed is re-queued. Previously the row sat in `waiting` forever (Corbys, 14 h).
- No DeliveryError holds the queue any more (all are known outcomes); unexpected exceptions on the CLI route fail only that turn. `/unhold` stays for legacy held rows.
- Restart recovery fails interrupted CLI turns (they died with the worker) instead of holding, and tells the owner.
- Worker supervision: a dead poll/deliver/send thread (e.g. Telegram 401/409) stops the worker cleanly so launchd restarts it.
- `fleet_health.py`: independent 60 s launchd watchdog across all mirrors (polling, heartbeat, held queues, delayed replies) with Mac notifications; reads SQLite metadata only, no credentials.

- A CLI turn timeout now fails only that turn and notifies the owner, the same as a clean CLI failure. `subprocess.run` kills the CLI on timeout, so nothing is left running. Holding the queue here had silently wedged hermes-dev (since Oct 5), pme-grant (since Oct 1) and corbys (since Oct 6). The timed-out turn is still never replayed.
- SQLite store switched to WAL with a 10s busy timeout (was 2s rollback journal), so readers no longer block the poller.
- Poll failures now record the exception class (e.g. `UncertainSend` = network, `database locked`) instead of "details suppressed", and log a warning. Still no URLs or credentials.

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
