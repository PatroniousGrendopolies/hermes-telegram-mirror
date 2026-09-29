# Hermes Telegram Mirror

One canonical Hermes Bot Chat, one private Telegram bot conversation. Use the same code checkout for several profiles while keeping their settings, credentials, queues and transcripts isolated.

A community plugin, not an official Hermes component. macOS/POSIX, Python 3.11+, PyYAML and a compatible local Hermes checkout are required. Private compatibility APIs are isolated in `compat.py`; pin and test your Hermes version before updating. See [build evidence](docs/BUILD_LOG.md) and [changes](CHANGELOG.md).

## Features

- Desktop user turns and final responses mirrored to Telegram.
- Telegram text delivered to the same canonical Bot Chat; no echo of your own phone input.
- Typing refresh, assistant interim messages, redacted tool previews, completion edits.
- Markdown subset rendered as Telegram HTML: headings, bold/italic, code, links, tables as readable rows. Balanced chunks under 3,800 UTF-16 units; rejected HTML falls back to plain text.
- Local `/start` and `/status`; other text goes to the Bot as conversational input.
- Profile-local persisted update offset, admission receipts, outbox and rate gate.

## Architecture

```text
Desktop / TUI / one-shot CLI
  plugin hooks (enqueue only)
       |
  background persistence worker
       |
       v
PROFILE_HOME/plugin-data/telegram-mirror/
  mirror.yaml + SQLite queues + leader/delivery locks
       ^                       |
       |                       v
Telegram getUpdates     single sender: typing / send / edit
  allowlist + admission        |
       |                       v
  serial delivery          Telegram private conversation
       |
  live canonical owner? -- yes --> owner-pinned Desktop mailbox
       |                           wait for durable receipt
       no
       v
  public Hermes CLI --resume (same canonical transcript)
```

Each profile has one long-lived network worker. Recommended: `worker.py`, run by launchd. It does not start a Hermes gateway, load a messaging-platform adapter, tick cron or dispatch kanban. Existing gateway hosting remains available with `host: gateway` but do not create a gateway merely for this mirror: gateways can enumerate other profiles' scheduled jobs.

Shared source is immutable deployment code. All mutable configuration/state is selected from the active profile home, never the symlink's resolved source directory. An exclusive per-profile `fcntl` lock permits only one polling/sending leader. Ordinary Desktop and CLI plugin loads cannot start standalone workers.

## Install

1. Clone this repository into a user-owned directory **outside the default profile's plugin discovery path**. Do not copy live deployment files into the checkout.
2. For every intended profile, create a symlink:

   ```sh
   mkdir -p "$PROFILE_HOME/plugins" "$PROFILE_HOME/plugin-data/telegram-mirror"
   ln -s "$CHECKOUT" "$PROFILE_HOME/plugins/telegram-mirror"
   cp "$CHECKOUT/mirror.example.yaml" "$PROFILE_HOME/plugin-data/telegram-mirror/mirror.yaml"
   chmod 700 "$PROFILE_HOME/plugin-data/telegram-mirror"
   chmod 600 "$PROFILE_HOME/plugin-data/telegram-mirror/mirror.yaml"
   ```

   Set `PROFILE_HOME` and `CHECKOUT` to absolute paths first. Back up existing configuration before changing it. Do not overwrite an existing plugin or settings file without a migration plan.
3. Edit the profile-local settings. Use its actual profile name, private Telegram chat ID, allowed sender IDs, labels, bot username, and absolute Hermes executable path. `session_id: null` resolves the unique hidden, unarchived `Bot Chat` and follows compression lineage. Ambiguity fails closed.
4. Add the bot token using macOS Keychain Access. The service/account identifiers must match your settings. Do not place the token in YAML, a `.env`, a command argument, source control or logs. Start your new bot conversation from your phone before sending outbound tests.
5. Enable only this profile:

   ```sh
   hermes -p PROFILE plugins enable telegram-mirror
   ```

   Never enable the mirror in another profile by accident. Existing general plugin allowlists are preserved by Hermes' enable command.
6. Use Hermes' Python and source on the import path to test the dedicated worker:

   ```sh
   PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERMES_SOURCE" \
     "$HERMES_PYTHON" "$CHECKOUT/worker.py" --home "$PROFILE_HOME" --profile PROFILE
   ```

7. For persistence, copy `docs/worker.plist.example` to a uniquely named file under `~/Library/LaunchAgents`. Replace **all** uppercase placeholders with absolute paths and the actual profile name. launchd does not expand shell variables or `~` inside plist strings. Validate with `plutil -lint`, then `launchctl bootstrap gui/$(id -u) PATH_TO_PLIST`. Use one unique Label per profile. Stop the foreground worker first, if running.

The existing Bot Chat tab need not stay open: an unowned chat uses CLI fallback. Keep the Mac awake, network available and login Keychain accessible. The mirror runs with the same OS account permissions as Hermes; it is not a sandbox.

### Hot activation and upgrades

An already-running Desktop backend must activate/reload this profile's plugin before its outbound hooks fire. Current Hermes provides profile-scoped activation through its Desktop plugin UI and `hermes_cli.plugins_activation.activate_plugin_now`. The latter is a private operator API; run it with the intended `HERMES_HOME`, not a globally inherited default. A `restart_required` result can refer to an absent gateway even when Desktop reports hooks active; verify the activation result and a harmless turn.

A shared checkout makes source changes available everywhere, but processes still need reload/restart to use them: hot-activate each profile's Desktop plugin and restart only its standalone worker. Do not restart unrelated gateways. Keep the old checkout as a rollback backup. The v3 settings location differs from v2: migrate `mirror.yaml` out of the plugin folder before activation.

To disable: boot out the standalone LaunchAgent **and** disable the profile plugin. Disabling hooks alone does not stop an independently managed worker. Never remove state or clear an uncertain receipt while a worker is alive.

## Security model

- Runtime Keychain lookup only. The token stays in process memory; network exceptions are replaced with credential-free diagnostics. HTTPS redirects and environment proxies are disabled. No bot token is needed in profile `.env`.
- Require allowlisted `from.id`, the configured destination chat, private-chat type and a non-bot sender. Rejected updates log only a generic warning, never content. Outbound transport refuses any other chat.
- Expected bot username and empty webhook are checked at startup. A competing poller is an error, not something this plugin evicts.
- Tool cards show only name, a conservatively redacted primary argument (120 characters), status and duration. Result/error bodies never enter tool cards. Each turn's interim notes and tool lines share one activity message that is edited in place (first send after 1.5 seconds, rolls over to a new message only near Telegram's size limit). Your own messages get a receipt reaction: 👀 picked up, 👍 delivered into the Bot Chat, 😢 held for inspection (`receipts: false` disables). Arbitrary unlabeled short secrets cannot be recognized reliably; set `show_tools: false` for sensitive workloads.
- Ordinary conversation text is intentionally mirrored, not comprehensively PII-redacted. Treat Telegram and the profile state database as recipients of that content. State directory is 0700; SQLite is 0600. Encryption at rest relies on the host.
- Query files are 0600 and moved to a private Trash folder after CLI use rather than deleted. State and receipts have no automatic retention policy; plan secure cleanup.
- Normal Hermes permissions and approvals are not weakened. There is no Telegram approval UI. A phone command can invoke the Bot's existing capabilities, so protect your Telegram account and allowlist.
- Per-profile credentials and state are isolation boundaries against routing mistakes, not separate OS security principals.

## Delivery, ordering and recovery

Stable turn IDs and persisted inbound receipt IDs dedupe hook/mailbox/CLI replies. New inbound transcript markers use a collision-checked six-character receipt prefix; legacy long markers remain readable. Accepted updates and the next offset commit atomically. Update offsets are per bot, never shared across profiles.

The sender serializes send/edit/typing requests with a persisted minimum 1.05-second interval and honors send-message 429 `retry_after`. Typing refreshes around every four seconds until final, interruption, error or a hard timeout. A very fast tool can already be complete when its batched card first posts. The last retained interim is not sent again as an identical final.

This is **not mathematically exactly-once delivery**. Telegram has no send-message idempotency key. If a send might have succeeded before a connection/process failure, it is held as `uncertain`, not automatically replayed. Likewise uncertain inbound delivery blocks later turns until inspected. Compare transcript, mailbox receipt and Telegram before reconciliation. No alternate route is attempted after mailbox admission or an unknown outcome.

`ctx.inject_message` is process-local in the tested Hermes implementation. A positive gateway result can precede asynchronous route rejection and is not a safe cross-process delivery probe. `compat.py` uses private `tools.bot_live_delivery` ownership/mailbox/receipt APIs for a live canonical owner; without an owner it uses the public CLI. Unsupported live owners fail closed. CLI results write to the same transcript, though a visible Desktop view may need refresh.

## Performance and backpressure

Hooks copy selected scalar references into CPython deques and return `None`; no SQLite, network, formatting, redaction, thread creation or mutex wait occurs in the callback. A persistence worker polls every 50 ms. Graceful exit drains it for up to five seconds, not Telegram network work.

Historical v2 10,000-callback measurement: median **0.792 µs**, p95 **1.417 µs**, maximum **169.291 µs**. A live Desktop turn measured **27.125 µs** maximum. These are plugin callback measurements, not total agent runtime or Hermes dispatcher overhead. Reproduce locally with `python benchmark.py`.

Expendable ingress and pending progress queues default to 256. Old progress is discarded under pressure, with durable tool cards discarded before interim messages. Finals have a separate retention lane exempt from the progress cap: this is not a hard total-memory bound under unlimited final input. Nonblocking ingress, finite memory and guaranteed retention under unlimited load cannot all be guaranteed. Normal canonical turns are serial. Hard termination before asynchronous persistence remains a loss window.

## Tests

In an isolated environment with `pytest`, `PyYAML` and `ruff` installed:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
ruff check --no-cache .
python benchmark.py
```

Unit tests do not contact Telegram, read Keychain or run real agents. They cover filtering, allowlists, chunking, dedupe, offsets, token scrubbing, delivery routing, failure holding, HTML rendering/fallback, typing, tool edits, pressure and shared-code isolation. See `docs/BUILD_LOG.md` for actual historical and current results, including unverified items.

## Limitations and what upstream would need

- Text only; no media transcription, token-by-token streaming, historical backfill or approval relay.
- macOS Keychain and POSIX locking; other credential stores/platforms need adapters.
- Private Hermes mailbox, ownership, compression and gateway-lock dependencies need a stable public canonical-chat delivery API with durable admission/completion receipts.
- Add public cross-process hook activation, API contract tests, reconnect/crash soak tests, bounded retention and safe reconciliation tooling.
- Standalone workers deliberately do not solve existing Hermes cron ownership. Audit every existing gateway/Desktop ticker before adding services; lock contention alone is not proof of one scheduler per profile.
- Phone-side animation/visual rendering and production soak behavior are not claimed solely from Bot API acceptance.

## License

MIT. See `LICENSE`.
