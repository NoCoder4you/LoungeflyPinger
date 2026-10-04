# Retailer resilience and endpoint audit

All enabled adapters use the single `AsyncHttpClient`: global concurrency is five, per-host
concurrency is two, and each host is paced independently at five requests/second. Retries are
limited to three for connection/DNS/TLS/timeouts, HTTP 429, and HTTP 500/502/503/504. HTTP 403,
404, and other permanent 4xx responses are attempted once. Backoff is exponential, capped, and
jittered; `Retry-After` is honoured. Scheduler intervals have ±5% jitter and every job has a
120-second runtime cap.

## Endpoint audit

The Shopify adapters consume public `/collections/<handle>/products.json` JSON feeds, with 250
items per page and a default ten-page ceiling. Pop Pelican, Infinity Collectables, GeekCore and
Ozzie Collectables use retailer-specific parsers for the same representation; Ozzie alone performs
at most 20 bounded detail enrichments. A failed required page raises, so no partial result is
reconciled. Schema and variant availability are validated before a scan succeeds.

The other enabled adapters use bounded public HTML, JSON search, WooCommerce Store API, or
Salesforce Commerce Cloud catalogue endpoints. EMP Germany/France/Spain/Italy and Large Netherlands
share the SFCC implementation but have independent health. Large Netherlands is quarantined:
ordinary access from the deployment network returned persistent HTTP 403. Its endpoint remains
`https://www.large.nl`, but it is disabled until a low-volume check from the Pi succeeds without
bypassing access controls. Existing product rows are retained while disabled.

## Request pressure

Configured catalogue lanes produce approximately 70–110 requests/minute in a fully synchronized
theoretical burst, but scheduler jitter and per-host pacing spread those calls. The long-run average
is approximately 8–15 requests/minute. Ozzie's enrichment adds at most 20 requests per 15 minutes
(1.33/minute). A transient request can make at most four total attempts; a conservative transport
worst case is four times normal traffic, still bounded by five global requests/second, two concurrent
requests per hostname, and each job's 120-second deadline. Live totals vary with pagination.

## Operations

```bash
.venv/bin/python -m app.tools.retailer_health
.venv/bin/python -m app.tools.retailer_health --failed
sqlite3 data/loungefly.db "SELECT name,health,circuit_state,error_category,response_status,last_failure FROM retailers ORDER BY name;"
journalctl -u loungefly-monitor.service --since '30 minutes ago' --no-pager
```

Never re-enable a blocked retailer until an ordinary request from the Pi succeeds and its contract
tests pass. Never use browser impersonation, proxy rotation, CAPTCHA solving, cookies, or credentials
to evade a retailer restriction.

## 2026-10-02 low-volume verification

One ordinary GET per affected endpoint from the development network found Infinity Collectables and
GeekCore returning JSON successfully (HTTP 200). Pop Pelican, Ozzie Collectables, Disney Mad, and
Gwen's Mermaid Cove all returned HTTP 429 from otherwise valid Shopify JSON URLs at nearly the same
time. This is direct evidence of shared source-network/storefront throttling rather than four parser
failures. Their adapters remain enabled because 429 is transient and now retains its own category,
honours `Retry-After`, and opens a persisted circuit after repeated scans. The configured Large
Netherlands `/search` endpoint returned HTML HTTP 200 from this network, while the production Pi
evidence remains HTTP 403; that deployment-specific restriction is why it remains disabled.

The prior generic error erased whether each production response was 429 or 5xx, so a more specific
historical root cause cannot be claimed. The new persisted fields preserve that evidence for the
next incident. A 403 opens the circuit immediately as `BLOCKED_BY_RETAILER`; transient failures open
after five failed scans. Open circuits do not run discovery or reconciliation, then permit one
half-open probe after a 15-minute exponentially increasing (24-hour capped) cooldown.
