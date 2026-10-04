# Loungefly Monitor — Production Guide

An asynchronous, stateful Loungefly product monitor intended for continuous operation on Linux,
including Raspberry Pi OS (64-bit). It normalizes retailer data, stores a silent first-scan
baseline in SQLite, detects later changes, and sends deduplicated Discord alerts.

## Production readiness and architecture

The composition root in `app/application.py` creates one shared bounded HTTP client, SQLite
connection, notifier, and scheduler. Every enabled storefront receives its own scheduler task and
`MonitorService`; an exception or timeout is logged by that task and does not stop other retailers.
`MonitorService` performs each scan's persistence atomically and keeps parser/network errors from
being treated as stock evidence. SQLite stores retailer health and notification deduplication, so a
restart retains the baseline and does not resend unchanged product notifications.

Important operational safeguards:

- HTTP concurrency, rate, per-request timeout, retries/backoff, and response-body size are bounded.
  An optional full-scan timeout is available for deployments that need it, but is disabled by
  default to accommodate legitimately slow paginated catalogues. Task timeouts protect
  asynchronous operations but cannot pre-empt arbitrary CPU-bound synchronous parser work;
  retailer tasks are isolated, not separate processes.
- `SIGINT`/`SIGTERM` requests shutdown; scheduler jobs are cancelled, then Discord/HTTP and SQLite
  are closed in order. Startup, ready, shutdown-requested, stopping, and stopped events are logged.
- SQLite uses foreign keys, WAL mode, a 5-second busy timeout, serialized writes, schema upgrades,
  and persistent product/alert state.
- A consistent SQLite online backup runs at every configured interval. Every backup is
  integrity checked before atomic publication and only the newest configured number are retained.
- Logs rotate by size. Defaults retain the active 5 MiB file plus three rotated files (about 20 MiB
  maximum). Webhook values are never logged by application code. The service also writes to the
  system journal, whose global retention is controlled by `journald.conf`.
- Secrets are environment-only. `.env`, databases, backups, virtual environments, and logs are
  excluded from Git.

## Requirements

- 64-bit Linux/Raspberry Pi OS with `systemd`
- Python 3.12 or newer, `python3-venv`, Git, `sqlite3`, and CA certificates
- A Raspberry Pi user named `pi`; the packaged units run under this unprivileged account
- Outbound HTTPS access to retailer sites and Discord

## Installation

The packaged services expect the checkout at `/home/pi/LoungeflyPinger` and run as the `pi` user.
The updater discovers the repository containing `update.sh`, while the systemd units use
absolute paths that match this installation location.

```bash
sudo apt update
sudo apt install -y git python3 python3-venv sqlite3 ca-certificates
python3 -c 'import sys; print(sys.version); raise SystemExit(sys.version_info < (3, 12))'
sudo -u pi git clone <YOUR_REPOSITORY_URL> /home/pi/LoungeflyPinger
sudo -u pi python3 -m venv /home/pi/LoungeflyPinger/.venv
sudo -u pi /home/pi/LoungeflyPinger/.venv/bin/python -m pip install --upgrade pip
sudo -u pi /home/pi/LoungeflyPinger/.venv/bin/pip install -r /home/pi/LoungeflyPinger/requirements.txt
sudo -u pi cp /home/pi/LoungeflyPinger/.env.example /home/pi/LoungeflyPinger/.env
sudo chmod 600 /home/pi/LoungeflyPinger/.env
sudo chmod 0755 /home/pi/LoungeflyPinger/update.sh
sudo install -m 0644 /home/pi/LoungeflyPinger/deploy/loungefly-update.service \
  /etc/systemd/system/loungefly-update.service
sudo install -m 0644 /home/pi/LoungeflyPinger/deploy/loungefly-monitor.service \
  /etc/systemd/system/loungefly-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable loungefly-update.service loungefly-monitor.service
sudo systemctl start loungefly-monitor.service
```

The service must not run as root. Root is only used to install the units and manage the service.
Ensure the checkout, including `data/` and `logs/`, remains owned by `pi:pi` after deployments.

At boot, `loungefly-update.service` runs before the monitor. It takes an exclusive lock, retries a
failed fetch, accepts fast-forward updates only, backs up `.env` and stopped SQLite state, builds an
isolated replacement virtual environment for each candidate, and runs compilation plus the full test
suite before activating it. A failed validation restores the previous Git commit; remote outages
or local tracked changes leave the installed version untouched. At boot the monitor is inactive, so
systemd starts it after the updater exits. During an interactive update of an active monitor, the
updater stops it only for activation, restarts it, and performs a health check. By default it updates the checkout containing the script. Its defaults can be
overridden with systemd environment variables such as `APP_DIR`, `BRANCH`, `PYTHON_BIN`,
`FETCH_ATTEMPTS`, and `MAX_BACKUPS`.

The initial installation may create `.venv` as a directory. Each successful update builds
`.venvs/<commit>` and atomically changes `.venv` into a symlink to the active commit environment;
the prior environment is retained until health verification succeeds. The monitor unit must always
execute `/home/pi/LoungeflyPinger/.venv/bin/python`.

The recommended privileged manual workflow does not grant broad passwordless sudo:

```bash
sudo systemctl stop loungefly-monitor.service
sudo -u pi /home/pi/LoungeflyPinger/update.sh
sudo systemctl start loungefly-monitor.service
sudo systemctl is-active loungefly-monitor.service
```

When launched while the service is active, the updater may use `sudo` only for the exact requested
`systemctl stop`/`start` operation. Non-interactive denial is explicit. If unattended active-service
updates are required, grant only those exact service verbs in a locally reviewed sudoers rule—not
`sudo ALL` and not a general passwordless capability probe.

Existing installations whose copied systemd unit still invokes `deploy/update.sh` remain supported:
that compatibility entry point forwards to the root updater. Reinstall the packaged unit and run
`sudo systemctl daemon-reload` to adopt the current `/home/pi/LoungeflyPinger/update.sh` path.

## Configuration

Copy `.env.example` to `.env`; never commit `.env`. `python-dotenv` loads it for manual runs and
systemd loads the same file with `EnvironmentFile`.

| Environment variable | Required | Default/effect |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | No | Product alert webhook; blank disables product delivery |
| `DISCORD_ADMIN_WEBHOOK_URL` | No | Retailer failure/recovery webhook; blank disables admin delivery |
| `DISCORD_PRODUCT_REMOVED_ENABLED` | No | `true`; set to `false` to suppress product-removal embeds |
| `DISCORD_ALERT_MENTION_MODE` | No | `everyone` for compatibility; may be `none`, `role`, or `everyone` |
| `DISCORD_ALERT_ROLE_ID` | In role mode | Positive role ID mentioned by product alerts |
| `DISCORD_BOT_ENABLED` | No | `false`; enables the Discord control gateway when `true` |
| `DISCORD_BOT_TOKEN` | When enabled | Bot token, kept only in the environment |
| `DISCORD_BOT_GUILD_ID` | Recommended | Guild used for immediate, guild-scoped command registration |
| `DISCORD_BOT_ALLOWED_USER_IDS` | For user authorization | Comma-separated Discord user IDs |
| `DISCORD_BOT_ALLOWED_ROLE_IDS` | For role authorization | Comma-separated Discord role IDs |
| `LOUNGEFLY_DATABASE_PATH` | No | `data/loungefly.db` |
| `LOUNGEFLY_BACKUP_DIRECTORY` | No | `data/backups` |
| `LOUNGEFLY_BACKUP_INTERVAL_HOURS` | No | `24`; positive number between later backups |
| `LOUNGEFLY_BACKUP_INITIAL_DELAY_SECONDS` | No | `30`; prompt first backup after startup |
| `LOUNGEFLY_BACKUP_COUNT` | No | `7`; positive integer retained |
| `LOUNGEFLY_LOG_PATH` | No | `logs/loungefly-monitor.log` |
| `LOUNGEFLY_LOG_LEVEL` | No | `INFO`; one of DEBUG/INFO/WARNING/ERROR/CRITICAL |

`config/retailers.yaml` controls global HTTP limits, alert thresholds, prices/releases, log
rotation, retailers, and each `interval_minutes`. Set a retailer's `enabled: false` to disable it.
Unknown retailer settings, non-mapping entries, non-boolean `enabled` values, and non-positive or
non-finite intervals are rejected before scheduler tasks are created. Avoid aggressive intervals:
retailer throttling makes scans less reliable, not more useful. Absolute database/backup/log paths
outside `/home/pi/LoungeflyPinger/data` and `logs` require matching `ReadWritePaths=` overrides in
both installed systemd units; relative paths are resolved from the application working directory.
`config/watchlist.yaml` controls alerts only; all discovered state is still persisted. Fields in a
watch are ANDed, list values match any entry, and separate watches are ORed. Supported constraints
include URL/product ID/SKU/name, required or excluded keywords, franchise, character, retailers,
product types, exclusivity, preorder, and maximum price.

### Discord webhook test

```bash
cd /home/pi/LoungeflyPinger
sudo -u pi .venv/bin/python -m app.tools.test_notification
sudo -u pi .venv/bin/python -m app.tools.test_notification --admin
```

These commands refuse to send when the applicable URL is blank. Treat webhook URLs as passwords;
rotate one in Discord immediately if it is exposed.

Discord notifications mention `@everyone` so subscribers are alerted, except for product-removal
notifications. The webhook's Discord channel permissions must allow everyone mentions for the
mention to notify channel members.

### Discord Control Bot

The optional control bot runs on the monitor's asyncio loop and is separate from outbound webhook
alerts. In the Discord Developer Portal, create an application and bot, keep its token in `.env`,
and invite it with the `bot` and `applications.commands` scopes. It needs only **View Channels** and
**Send Messages** (plus **Attach Files** for `/watch export`); Administrator and Message Content
intent and other privileged intents are not required. Set `DISCORD_BOT_GUILD_ID=<server id>` for
production so commands are registered only in that server and synchronize promptly. Leaving the
guild ID blank registers global application commands, which can take longer to appear. Leaving
`DISCORD_BOT_ENABLED=false` makes no gateway connection and preserves the pre-bot operation.

Mutation access is deny-by-default. A caller must have an ID in
`DISCORD_BOT_ALLOWED_USER_IDS` or a role in `DISCORD_BOT_ALLOWED_ROLE_IDS`; server membership alone
does not authorize changes. Denials and all attempted mutations are written to the SQLite audit
log. Management replies are ephemeral, and deletion uses a one-minute confirmation restricted to
its initiating user.

Commands include `/watch list`, `/watch show`, `/watch add`, `/watch edit`, `/watch delete`,
`/watch enable`, `/watch disable`, `/watch export`, `/alerts list`, `/alerts enable`,
`/alerts disable`, `/status`, and read-only `/retailers`. On the
first startup after upgrade, the validated `config/watchlist.yaml` rules are imported into SQLite
and a durable migration marker is recorded. This happens exactly once—even if every rule is later
deleted. SQLite is then authoritative; edits do not modify tracked YAML. Enabled rules are held in
an immutable in-memory snapshot, replaced immediately after each committed mutation, and included
in the existing online database backups. `/watch export` provides a secret-free YAML copy.

If commands do not appear, verify the application was invited with both the `bot` and
`applications.commands` scopes, the guild ID is correct, and inspect
`sudo journalctl -u loungefly-monitor.service -f`. A healthy startup reports
`discord_control_starting`, `discord_control_ready`, `discord_command_sync_started`, and
`discord_command_sync_complete`. Look for `discord_configured_guild_not_found` when the configured
server is not visible to the bot, or `discord_command_sync_failed` when Discord rejects the sync.
Guild commands normally synchronize as soon as the bot connects; global registration (blank guild
ID) can take longer. A bot authentication, gateway, or synchronization failure is logged but does
not stop retailer jobs. Disable it by setting
`DISCORD_BOT_ENABLED=false` and restarting the existing service. No new daemon, root privilege, or
systemd sandbox change is needed.

## Running

Manual production-equivalent run (stop with Ctrl+C):

```bash
cd /home/pi/LoungeflyPinger
sudo -u pi .venv/bin/python -m app.main
```

Development run and checks:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.main
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app tests
```

### Service management

Service name: **`loungefly-monitor.service`**.

```bash
sudo systemctl start loungefly-monitor.service
sudo systemctl stop loungefly-monitor.service
sudo systemctl restart loungefly-monitor.service
sudo systemctl status loungefly-monitor.service
sudo journalctl -u loungefly-monitor.service -f
sudo journalctl -u loungefly-monitor.service --since today
```

A normal stop sends SIGTERM and allows 45 seconds for graceful closure. Unexpected non-zero exits
restart after 10 seconds. Configuration errors exit non-zero. The unit retains `Restart=on-failure` and a ten-second delay,
but limits starts to five per five minutes. Diagnose and clear a corrected persistent failure with:

```bash
sudo systemctl status loungefly-monitor.service
sudo journalctl -u loungefly-monitor.service
sudo systemctl reset-failed loungefly-monitor.service
```

## Retailers and intervals

“Supported” means an adapter and test fixtures exist. “Limited” describes release-date enrichment,
not core catalogue/stock monitoring: most sites publish release information inconsistently.

| Default interval | Supported retailers |
| --- | --- |
| 5 min | GeekCore; AmyDavidMagic high-priority lane |
| 10 min | Disney Mad UK; Damaged Society UK; Koolaz UK; Cool-Merch UK; LF Lovers; CM POP UK; Geek Garage UK; Razmatazz UK; TruffleShuffle; Loungefly UK/US/Canada; Disney Store UK/US; Modern PinUp; Circle Of Hope Boutique; Merchoid UK; Pink a la Mode; 707 Street; Cordy's Corner; Infinity Collectables; Something Different Gift Shop UK; Forbidden Planet International UK; WORLD 1-1 GAMES; The Bag Dude; AmyDavidMagic incoming lane; EMP Germany/France/Spain/Italy; Large Netherlands |
| 15 min | Get Ready Comics UK; BoxLunch; Hot Topic US; Ozzie Collectables; Pop Pelican; Gwen's Mermaid Cove |
| 30 min | AmyDavidMagic reconciliation lane |

Core stock monitoring is supported for every retailer above. Release metadata is **limited** for
most retailers because only explicit retailer-published values are trusted; EMP region stores have
the strongest date-only support. No adapter exists for retailers absent from this list; they are
**unsupported** until implemented and tested. Retailer HTML/API changes remain an inherent external
dependency.

Magic Madhouse UK, Popcultcha, Entertainment Earth, and Large Netherlands adapters remain implemented and tested but
are disabled by default because those storefronts currently return HTTP 403 to the production
monitor. Re-enable an entry only after confirming that ordinary, policy-compliant HTTP access is
available from the deployment host; the monitor does not attempt to evade storefront access controls.

Use `.venv/bin/python -m app.tools.retailer_health --failed` for sanitized persisted diagnostics.
The endpoint audit, circuit behavior, request-pressure estimates, and more production commands are
documented in [`docs/retailer-resilience.md`](docs/retailer-resilience.md).

## Adding a retailer

1. Implement `RetailerMonitor` from `app/monitors/base.py`; use `AsyncHttpClient`, never a second
   unmanaged session. Discovery must return normalized, stable `Product` identities.
2. Prefer a documented/public structured feed. Bound pagination and requests, respect 403/429 and
   `Retry-After`, and never bypass access controls. Treat malformed or incomplete responses as
   errors rather than out-of-stock evidence.
3. Normalize prices/currency, availability, preorder, URLs, stable IDs, and optional metadata.
   Parse releases only from explicit retailer claims; do not infer them from publication dates.
4. Export the adapter from `app/monitors/__init__.py`, register exactly one independent job and
   retailer health row in `Application.start`, and add a disabled-by-default YAML entry while it is
   being validated.
5. Add captured, secret-free fixtures and tests for parsing, pagination/deduplication, silent first
   sync, changes, failures, restart persistence, and notification behavior. Run the complete suite.
6. Document support level, data source, interval, and limitations here before enabling production.

## SQLite operations

The default live database is `/home/pi/LoungeflyPinger/data/loungefly.db` (relative configuration is
resolved from the service working directory). WAL sidecars may exist while running; do not copy
only the `.db` file with ordinary `cp` during writes.

Automatic backups are consistent snapshots in `/home/pi/LoungeflyPinger/data/backups`. The first
verified online backup is scheduled 30 seconds after startup; later backups default to 24-hour
intervals, with seven retained application-generated `loungefly-<timestamp>.db` files. Automatic
retention manages only that filename pattern. Manual snapshots, updater state (ten generations by
default), restore directories, and `*.failed-update-*` rollback snapshots must be reviewed and pruned
by the operator after validation, for example with a conservative age filter and an inspected list.
Never delete the active database or the most recent rollback set.

Create and verify a stopped manual snapshot:

```bash
sudo systemctl stop loungefly-monitor.service
stamp=$(date -u +%Y%m%dT%H%M%SZ)
sudo -u pi cp -- /home/pi/LoungeflyPinger/data/loungefly.db "/home/pi/LoungeflyPinger/data/backups/manual-$stamp.db"
sudo -u pi sqlite3 "/home/pi/LoungeflyPinger/data/backups/manual-$stamp.db" 'PRAGMA integrity_check;'
sudo systemctl start loungefly-monitor.service
```

Restore with the supplied fail-closed script. It requires the monitor already stopped, immediately
integrity-checks the selected snapshot, preserves the current database and WAL/SHM sidecars in a
unique timestamped directory, verifies a mode-0600 temporary replacement, atomically renames it,
removes stale sidecars only while stopped, restarts, and validates service/database state:

```bash
sudo systemctl stop loungefly-monitor.service
sudo /home/pi/LoungeflyPinger/deploy/restore-backup.sh \
  /home/pi/LoungeflyPinger/data/backups/<BACKUP>.db
sudo systemctl is-active loungefly-monitor.service
sudo -u pi sqlite3 /home/pi/LoungeflyPinger/data/loungefly.db 'PRAGMA integrity_check;'
```

If startup or validation fails, leave the service stopped. Copy the preserved `loungefly.db`,
`loungefly.db-wal`, and `loungefly.db-shm` from the path printed by the script back into `data/`,
restore `pi:pi` ownership and mode `0600`, start the service, then repeat both validations. Each run
uses a unique directory; no fixed `loungefly.db.pre-restore` is reused.

### Log retention

These are separate controls: application file logs use the YAML `max_bytes`/`backup_count`; updater
file logs use `logrotate`; system-journal retention is **not bounded by this project** and depends on
the host's `journald.conf`. Install and verify updater rotation without restarting the monitor:

```bash
sudo install -o root -g root -m 0644 deploy/loungefly-updater.logrotate /etc/logrotate.d/loungefly-updater
sudo logrotate --debug /etc/logrotate.d/loungefly-updater
sudo logrotate --force /etc/logrotate.d/loungefly-updater
sudo stat /home/pi/LoungeflyPinger/logs/updater.log
```

The policy rotates near 5 MiB, retains three compressed generations, tolerates missing/empty logs,
and performs rotation as `pi:pi`.

## Troubleshooting

- **One retailer fails:** inspect its last exception and retailer health row. Confirm DNS/TLS and
  the site manually. Other jobs continue independently. Disable only that YAML entry if it is noisy.
- **Discord webhook fails:** run the standalone test, confirm the URL has no quotes/whitespace and
  still exists, and inspect HTTP status logs. Failed deliveries are not marked successful and can
  retry; admin failure episodes deliberately avoid alert storms.
- **Database problems:** stop the service, preserve all `.db`, `-wal`, and `-shm` files, check disk
  space/ownership, run `PRAGMA integrity_check`, and restore a verified backup. Never edit live
  tables while the monitor runs.
- **Parser errors:** a retailer likely changed markup. Preserve a sanitized response as a fixture,
  update only that adapter, and run its tests plus the full suite. Parser errors do not clear stock.
- **HTTP 403:** verify the configured user agent and reduce request pressure; the site may no longer
  permit automated access. Do not bypass a block. **HTTP 429:** honor `Retry-After`, reduce the
  retailer interval/global rate, and allow exponential retries to settle.
- **Service does not start:** use `systemctl status` and `journalctl`; verify absolute unit paths,
  `.env` syntax and mode, Python 3.12+, installed dependencies, YAML validity, and write ownership
  for `data/` and `logs/`. Run the exact `ExecStart` as the `pi` user.
- **Disk usage:** inspect `du -sh data logs`; application-generated backups and application/updater file logs are bounded by
  their separate policies. Manual and failed-update snapshots need operator pruning. Configure global journal limits in `/etc/systemd/journald.conf` if necessary.
- **Restart notifications:** unchanged state should be silent. If alerts recur, do not delete the
  database; verify the configured path and service working directory point to the persistent file.

## Project structure

```text
.
├── .env.example                 # documented, secret-free environment template
├── app/
│   ├── application.py           # composition root and lifecycle
│   ├── config.py                # YAML/.env validation
│   ├── database.py              # schema, migrations, lifecycle, backups
│   ├── http.py                  # bounded resilient HTTP transport
│   ├── logging_config.py        # console and size-rotating file logs
│   ├── main.py                  # CLI and Unix signal handlers
│   ├── models.py                # normalized domain models
│   ├── monitors/                # retailer adapters and shared bases
│   ├── notifications/           # Discord delivery and deduplication
│   ├── services/                # monitor/product/stock/release/alert workflows
│   └── tools/test_notification.py
├── config/
│   ├── retailers.yaml           # intervals, limits, enabled retailers
│   └── watchlist.yaml           # alert selection
├── data/                        # ignored live DB and bounded backups
├── deploy/loungefly-monitor.service
├── deploy/loungefly-update.service
├── deploy/update.sh              # compatibility launcher for older installed units
├── update.sh
├── logs/                        # ignored rotating logs
├── tests/                       # unit, integration, lifecycle, persistence tests
├── pyproject.toml
└── requirements.txt
```

## Known limitations and future improvements

Retailer endpoints and markup can change without notice; Cloudflare/bot protection can make a
retailer unavailable; release precision is limited to explicit source data; one global HTTP rate
limiter is conservative but not retailer-specific; SQLite is appropriate for one process but not
multiple active monitor replicas; there is no metrics/health HTTP endpoint or automated off-device
backup. Sensible future work is a small health/metrics exporter, per-host circuit breakers, alerts
for low disk space/backup age, off-device encrypted backup replication, and extracting the verbose
adapter registration into a declarative registry. These are deliberately not part of this
production-hardening change.

## Stage 2: live retailer and scan management

The optional Discord control bot now manages retailer scheduling inside the existing
`loungefly-monitor.service` process. All `/retailer` responses are ephemeral. The
same deny-by-default user/role allow-list used by watch management protects
configuration changes and manual scans; no systemd, shell, SQL, or filesystem
permissions are granted to Discord.

* `/retailer list` shows every trusted adapter's effective state, interval, and health.
* `/retailer show geekcore` shows YAML defaults, runtime overrides, persisted health,
  scan timing/status, and whether a scan is active.
* `/retailer enable <key>` immediately enables and schedules an adapter.
* `/retailer disable <key>` asks for a user-bound, 60-second confirmation, then
  removes future executions. An already-running scan is allowed to finish.
* `/retailer interval geekcore 8` safely reschedules the main scan lane.
* `/retailer reset <key>` removes SQLite overrides and restores startup YAML defaults;
  confirmation is required when the enabled state changes.
* `/retailer scan geekcore` executes the normal `MonitorService.synchronize()` path.
  It works while scheduled monitoring is disabled and does not enable it. A second
  scan of the same retailer is rejected rather than queued, while other retailers
  can continue concurrently. Individual network requests always use the configured
  `request_timeout_seconds` deadline. The optional `retailer_job_timeout_seconds`
  setting adds a shared hard deadline for manual and scheduled scans when set to a
  positive number; it defaults to disabled so a healthy paginated scan is not
  cancelled merely for exceeding a fixed wall-clock duration. Configured full-scan
  timeouts update health and scan history as failures and always release the lock.
* `/retailer failures <key>` displays bounded, durable recent failed-scan summaries.

`config/retailers.yaml` is the deployment default and is read only at process startup.
Discord overrides (enabled and the main interval) live in SQLite and survive restarts;
the YAML file is never rewritten. Intervals are limited to **1–1440 minutes**.
Specialized `amy_david_magic` lane intervals and Ozzie's `detail_batch_size` are shown
read-only and retain their startup values; the command changes only the canonical
main schedule. Every scheduled and manual scan records bounded metadata (never HTTP
bodies, headers, tracebacks, or secrets), and retailer actions are audit logged. Scan
history is capped at the newest 4,000 entries per retailer (nearly three days at the
minimum interval, or about four weeks at ten minutes); the cap is also enforced during startup upgrades so
the live database and its backups cannot grow indefinitely from scan history.

For troubleshooting, use `/retailer show`, `/retailer failures`, `/status`, and inspect
`journalctl -u loungefly-monitor.service` or `logs/loungefly-monitor.log`. Do not edit
the database. Deployment remains `/home/pi/LoungeflyPinger` with its `.venv`: update
the checkout and dependencies, run the tests, then restart the existing service once
to deploy this release. No additional daemon or service permission is required.
# Stage 3 product intelligence

The monitor's SQLite observations now have a typed, read-only catalogue layer:
Discord commands create immutable `ProductQuery` filters, `ProductQueryService`
executes parameterized and bounded SQL, and the Discord adapter renders ephemeral
embeds. Catalogue reads use the existing Discord user/role allow-list and are
deny-by-default. Pages contain 12 products (hard maximum 25), use deterministic
ordering, and navigation buttons are bound to the requester and expire after two
minutes.

Available means a non-removed listing whose normalized state is `IN_STOCK` or
`LOW_STOCK`. `PREORDER` is included only with `include_preorders:true`;
`COMING_SOON`, `OUT_OF_STOCK`, `ERROR`, and `UNAVAILABLE` are never considered
available. Recent discovery uses `first_seen` (24 hours by default), not the most
recent scan time.

Commands include:

* `/product available product_type:mini_backpack`
* `/product search query:"Sorcerer's Apprentice"`
* `/product search franchise:Disney character:Stitch`
* `/product recent period:24h`
* `/product show id:123`
* `/product history id:123`
* `/product offers id:123`
* `/product releases franchise:Disney`
* `/product preorders`, `/product sales`, and `/product exclusives`
* `/alerts recent`

State history is written only for the first observation or a change to
availability, price, currency, or preorder state. Release history retains its
existing distinct-release semantics. Detected product changes are stored in
`product_events` independently of webhook delivery; retention is capped at 250
events per product and 20,000 events globally. Notification delivery attempts
remain separately recorded in `alerts`.

Cross-retailer offers never merge listings. Matching is **HIGH** for a normalized
barcode or Loungefly code, **MEDIUM** for a credible SKU plus matching normalized
title and product type, and **POSSIBLE** for matching title, franchise, and type.
Title-only similarity is not considered certain. Cheapest markers are calculated
only among currently orderable offers in the same currency; Stage 3 performs no
currency conversion. These matches are heuristics and should be verified using
the retailer links.

SQLite schema upgrades and indexes are applied automatically during normal
startup. No extra service, port, root access, or change to the Raspberry Pi
systemd deployment is required.

## Rich Discord stock alerts

Product webhooks use adaptive Discord embeds: the product name links to its retailer page, the
primary image is shown as a thumbnail, and only metadata actually supplied by an adapter is shown.
Status, normalized price, prior price/status, SKU or product ID, explicit exclusivity, release/ETA,
watch-match context, first-seen/restock time, and optional adapter-supplied Add to Cart links fit
within Discord's limits. Missing images and optional fields do not prevent delivery.

Alert colours communicate intent: blue identifies new products, coming-soon and informational
updates; green identifies restocks, availability and price drops; purple identifies preorders;
orange identifies backorders, low stock, price increases and warnings; red identifies sold-out and
monitor errors. Supported stock events include **NEW PRODUCT**, **RESTOCK**, **PREORDER**,
**BACKORDER**, **COMING SOON**, **NOW AVAILABLE/RELEASED**, **PRICE DROP**, **PRICE INCREASE**,
**STATUS CHANGE**, **PRODUCT UPDATED**, and **SOLD OUT**, plus existing release and monitor-health
events. Price comparisons use normalized `Decimal` values, so currency symbols or trailing zeroes
do not create false changes.

Mentions are controlled by `DISCORD_ALERT_MENTION_MODE=none|role|everyone`. The backward-compatible
default is `everyone`; `role` additionally requires `DISCORD_ALERT_ROLE_ID`. Payloads explicitly
allow only the selected mention type, so scraped product and retailer text cannot ping users or
roles. Product-removal alerts never mention and can be suppressed entirely with
`DISCORD_PRODUCT_REMOVED_ENABLED=false`. Webhook URLs and role IDs belong only in `.env`.

Authorized bot operators can inspect and change every outbound event category at runtime with
`/alerts list`, `/alerts enable <alert_type>`, and `/alerts disable <alert_type>`. These overrides
are stored in SQLite, take effect immediately, and survive restarts. The environment setting above
is the startup default for `PRODUCT_REMOVED` until a bot override is created. Disabling delivery
does not disable monitoring or product-event history.

Adapters may optionally populate `image_url`, `sku`, `variant_id`, `loungefly_product_code`,
`exclusive` plus explicit exclusivity text, release/ETA metadata, and a validated `cart_url`.
Retailer display name, icon, homepage, currency, and country presentation can be extended in the
central `app/retailers.py` catalogue without retailer branches in the renderer. No capability is
inferred merely because a retailer sells an item.

SQLite stock history remains a change log: unchanged scans update current observation time but do
not append history or send notifications. Notification delivery identities are persisted in
`notification_deliveries`, preventing the same event from being posted again after restart. The
schema initializer only adds missing columns/tables and remains compatible with existing database
files; it never recreates production data.
