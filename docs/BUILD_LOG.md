# Build log

This log contains aggregate evidence only. Deployment profiles, identities, paths, session IDs, Telegram message IDs, transcript row IDs, Keychain identifiers and conversation contents are deliberately omitted. Private operational receipts are kept outside this repository.

## v1: durable two-way final turns

Design: persist admissions and outgoing messages, use a single elected gateway worker, filter one canonical Bot Chat, require an allowlisted private conversation, and retrieve credentials from macOS Keychain only. Public process-local injection cannot safely route between a gateway and Desktop, so private owner-pinned mailbox compatibility was isolated from the public CLI fallback.

Historical verification: 33 unit tests passed; Ruff passed. Bot identity and empty webhook were verified. A connection post, one harmless CLI turn and a repeated synthetic inbound update produced the expected remote posts and canonical transcript rows, with no duplicate reply. Same-text remote comparisons confirmed text; restart preserved receipts and resumed polling without replay. Initial live tests did not exercise an open Desktop mailbox.

## v2: live progress without blocking hooks

Design: hot-path callbacks enqueue selected scalar references only; background persistence handles SQLite, rendering and redaction. One elected sender handles typing, interim messages, batched tool starts and completion edits, plus finals. HTML chunks remain balanced; explicit parser rejection permits plain-text fallback. Ambiguous sends remain held, not replayed.

Historical verification: 78 tests passed in 4.76 seconds; Ruff passed. A real live Desktop mailbox turn produced interim, a single harmless tool invocation and final in source-event order. Separate labelled synthetic transport tests verified editing the same remote message and fallback after deliberately rejected HTML. Remote text comparisons confirmed the resulting posts. Recorded API completion spacing was at least 1.139 seconds.

Performance evidence: 10,000 callback stress test measured median 0.792 microseconds, p95 1.417 microseconds, maximum 169.291 microseconds. The live Desktop turn measured maximum 27.125 microseconds with no dropped progress. These are historical local measurements, not universal latency guarantees.

## v3: shared checkout and independent workers

Design: one clean checkout symlinked into five profile plugin directories. Configuration moved to each profile's `plugin-data/telegram-mirror/mirror.yaml`; existing per-profile state remains in place. Code is not installed or enabled in the default profile. The original gateway host remains supported. Four additional mirrors use separate launchd-managed Python workers, an explicit entry point that does not start gateway/cron/kanban services. Process isolation avoids cross-profile global configuration leakage.

Five additional unit regressions cover shared settings/state isolation, rejecting profile mismatch, preventing ordinary plugin loads from starting standalone workers, bypassing gateway probes in the standalone leader, scheduler-free worker imports, and exclusive leadership.

Executed from the clean staging checkout: **83 passed in 1.19 seconds; Ruff: All checks passed!**

Live evidence:

- All four new workers verified expected bot identity and no webhook, then ran healthy long polling.
- Each new bot received one connection post and exactly one final response to one harmless test input, with one user and one final row in its canonical Bot Chat. No tool calls occurred. Remote text comparisons verified the posts. Tests used CLI fallback because the new chats had no live Desktop owner.
- The original mirror passed the same no-action regression through its live Desktop mailbox after migration to shared code. Exactly one reply and matching transcript rows were confirmed.
- Profile-scoped activation reported all nine hooks active in Desktop for each profile. The four CLI tests do not establish live Desktop-hook firing for those new profiles; the original profile's live mailbox test does establish it there.
- Requested stale platform token assignments were backed up and commented locally without exposing values. Those secret-bearing backups never entered this checkout.

### Scheduling finding (unresolved, not hidden)

Pre-existing gateways both enumerate all local profiles. Profiles without a dedicated gateway are not excluded by the gateway ownership gate, and the Desktop ticker has its own fallback path. Per-profile tick locks serialize overlapping ticks, but that does **not** prove exactly one ticker per profile. The existing global kanban dispatcher has a singleton lock. No new mirror process starts either scheduler, and no existing gateway was restarted to change scheduling ownership. Converging the existing scheduler topology safely remains separate work; this release does not claim that acceptance criterion was met.

### Not verified

Phone-originated typing/visual rendering for each new bot, new-profile live Desktop-hook firing, extended production soak, and exact-single-ticker ownership in the pre-existing Hermes topology. API text comparisons are not a visual phone inspection. No real business action was requested by integration tests.

## Publication discipline

The public tree was assembled using a runtime/test file allowlist, not by committing a live plugin directory. Local settings, state, backups, operational check scripts and receipts are excluded. A pre-commit regex scan and post-commit full-history scan are required before pushing, covering token patterns and deployment-specific identifiers. The exact project name is a permitted generic identifier; the copyright holder appears only in LICENSE. Git author metadata uses the public account handle rather than a local personal name/email.

No Hermes core changes are part of this plugin. Future upstream work requires a stable public cross-process canonical-chat API, activation/lifecycle contracts, portable credential stores, crash/reconnect testing, retention controls and explicit reconciliation tooling.
