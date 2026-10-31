# Internal API

A small JSON API that hands other bots the Vitality / Zero ELD facts this bot
already polls, so they need no DriveHOS keys of their own. The first client is
the driver-alerts bot: it uses the API to find out who is driving a truck, the
driver's duty status, name and phone, and where the truck is.

The server is aiohttp and runs in the bot's own event loop (`app.py` →
`utils/internal_api/server.py`). It starts only when `INTERNAL_API_KEY` is set.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `INTERNAL_API_KEY` | empty | Shared secret. Callers send it in the `X-Api-Key` header. **Empty = the server does not start**, and every `/api/internal/...` route answers 404. |
| `PORT` | `INTERNAL_API_PORT`, else `8080` | Port to listen on. Railway injects `PORT`. |
| `INTERNAL_API_HOST` | empty (all interfaces, IPv4 and IPv6) | Bind address. Leave it empty on Railway, because private networking can use IPv6. |
| `INTERNAL_API_COMPANY_ALIASES` | empty | Extra company names, for example `jrd=7,acme=Acme Freight LLC`. Each entry maps a caller's slug to this bot's company id or exact company name. See [Company mapping](#company-mapping). |
| `INTERNAL_API_UPSTREAM_TIMEOUT` | `6` | Seconds a request waits for DriveHOS when the data on hand is too old. |

The API uses the bot's existing Vitality settings: `VITALITY_PROVIDER_KEY`,
`VITALITY_PLUS_PROVIDER_KEY`, `ZERO_PROVIDER_KEY`, the base URLs and each
company's `vitality_company_key` / `zero_company_key`. It never returns any of
those keys.

## Authentication

- `GET /health` needs no key.
- Every `/api/internal/vitality/...` request needs `X-Api-Key: <INTERNAL_API_KEY>`.
  The key is compared in constant time. A missing or wrong key gets 401.

## Company mapping

Each request names its company in the `company` query parameter. The API
resolves it in this order:

1. **An alias** from `INTERNAL_API_COMPANY_ALIASES`. The alias is compared
   ignoring case and punctuation. Its target is a company id (digits) or a
   company name.
2. **A company id** (`company=7`).
3. **The company name**, compared ignoring case and anything that is not a
   letter or digit. For example, `jrd` matches a company named `JRD` or `J.R.D.`.

No match returns 404 `unknown_company`. More than one match returns 409
`company_ambiguous`, and you then need to set an alias.

The driver-alerts bot sends its own company slug (for example `jrd`). Check
`GET /api/internal/vitality/companies` to see how this bot names its companies
and which aliases apply. If a slug does not match a name, add an alias such as
`jrd=<company id>`.

**Provider.** The `provider` parameter takes `auto` (the default), `vitality`
or `zero`:

- `auto` uses Vitality when the company has a Vitality company key, else Zero.
- Vitality Plus needs nothing from the caller. Carriers that moved to Plus keep
  their company key, and the API uses whichever provider key last worked, just
  as the poller does.
- The `provider` field in each answer says which one served the data:
  `vitality`, `vitality_plus` or `zero`.

## Freshness and load

The API adds as few DriveHOS requests as it can.

| Fact | Source | Reuse |
|---|---|---|
| Fleet: position, speed, status, last report, `driver_id` per truck | `/v2/latest-vehicle-status` | Uses the poller's latest answer when it is no older than `max_age`. Otherwise one on-demand fetch, shared by every request that arrives while it runs. That fetch then becomes the latest data. |
| Active or deactivated truck | `/v2/vehicles` | Cached 10 min, shared with the poller. |
| Driver name and phone | `/v2/drivers` | Cached 1 h, shared with the poller's name cache. A driver missing from the roster is fetched once (`/v2/driver/{id}`) and remembered for 1 h. |
| Duty status | `/v2/latest-driver-status?driver_id=` | Cached 45 s per driver (errors 15 s). Fetched only for trucks a caller asks about, at most 3 per unit request. |

- **`max_age`** (seconds, default 120, at least 30, at most 3600) is how old a
  fleet snapshot the caller accepts. A value below 30 is raised to 30, so no
  caller can make the bot fetch a fleet more than twice a minute.
- The poller fetches each company's fleet every `POLL_INTERVAL` (5 min). It
  only does so when the company's movement provider shows a moving truck.
- **Failed fetches are remembered**, so a burst of requests does not hammer a
  dead feed:
  - a refused key or an empty fleet for 5 minutes;
  - any other error for 45 seconds.
  While one is remembered, requests get 503 with `retry_after_seconds`.
- **Timeouts.** If DriveHOS does not answer within
  `INTERNAL_API_UPSTREAM_TIMEOUT`, the request gets 503 `upstream_timeout`. The
  fetch keeps running, and the next request gets its result.
- **Ages in every answer.** Every answer says how old its data is, so the caller
  decides whether it is fresh enough:
  - `snapshot.age_seconds`: when the fleet was fetched;
  - each vehicle's `report_age_seconds`: when the truck last reported;
  - `duty.age_seconds`: when the duty status was fetched.
- **Data is current only.** DriveHOS has no history. The driver on a truck is
  whoever is assigned now. Compare `last_report_at` with your event time.

## Endpoints

Every endpoint is a `GET` and every answer is JSON. Times are UTC ISO 8601,
for example `2026-11-01T10:15:00Z`. The examples below use made-up data.

### `GET /health`

```json
{"status": "ok"}
```

### `GET /api/internal/vitality/companies`

This bot's companies and how callers can name them. No keys are included.

```json
{
  "api_version": 1,
  "companies": [
    {"id": 7, "name": "JRD", "active": true, "aliases": ["jrd"], "vitality": true, "zero": false}
  ]
}
```

### `GET /api/internal/vitality/fleet?company=jrd[&provider=auto][&max_age=120]`

Every vehicle in the latest-vehicle-status feed. Deactivated trucks are
included, with `active: false`. Duty statuses are not included.

```json
{
  "api_version": 1,
  "company": {"id": 7, "name": "JRD", "active": true},
  "provider": "vitality",
  "snapshot": {"fetched_at": "2026-11-01T10:15:00Z", "age_seconds": 42,
               "source": "poller", "vehicle_count": 118},
  "vehicles": [
    {
      "number": "4063",
      "vehicle_id": "a1b2c3",
      "vin": "TESTVIN0000000001",
      "active": true,
      "status": "IN_MOTION",
      "online": true,
      "position": {"lat": 35.1, "lon": -111.6},
      "location": "1.0mi E of Sometown, AZ",
      "speed": 61.0,
      "heading": null,
      "odometer": 123456.0,
      "last_report_at": "2026-11-01T10:14:30Z",
      "report_age_seconds": 72,
      "driver": {"id": "d-123", "name": "Test Driver", "phone": "+15550100001"}
    }
  ]
}
```

Vehicle fields:

- `status`: the value DriveHOS sends (`IN_MOTION`, `STATIONARY` or `OFFLINE`),
  or null.
- `online`: true when `status` is not `OFFLINE` and the last report is no older
  than `ELD_STALE_THRESHOLD` (600 s).
- `active`: true or false from `/v2/vehicles`. It is null when that list was
  unavailable.
- `position`: null when DriveHOS has no fix. A `0,0` position counts as no fix.
- `speed`: as DriveHOS reports it. The poller treats it as mph.
- `heading`: passed through when the feed carries a `heading`, `bearing`,
  `course` or `direction` field. DriveHOS is not known to send one, so expect
  null.
- `driver`: null when no driver is assigned. Its `name` and `phone` are null
  when the roster has none. The name is "First Last" in the roster's own
  letter case, with spaces collapsed, falling back to the username.
- `source`: `poller` when the poller fetched this data, `on_demand` when the
  API did.

### `GET /api/internal/vitality/unit?company=jrd&unit=4063[&max_age=120][&duty=1][&provider=auto]`

Every vehicle that could be this truck, plus the duty status of each
vehicle's driver.

Matching follows the driver-alerts bot's previous rules exactly:

1. **The exact match.** The unit is compared with letters and digits only,
   upper-cased. If that fails, the API tries the bare unit before a suffix
   (`4063 O/O` → `4063`). Each attempt is also tried without leading zeros
   (`942` → `0942`).
2. **The variants.** Every other vehicle with the same base unit, meaning the
   first token without trailing letters (`4063z` and `4063 O/O` for `4063`).

`ambiguous` is true when more than one vehicle matches. The caller must tell
them apart, for example by distance to the event, or name nobody. The API never
picks a vehicle for you.

```json
{
  "api_version": 1,
  "company": {"id": 7, "name": "JRD", "active": true},
  "provider": "vitality",
  "snapshot": {"fetched_at": "2026-11-01T10:15:00Z", "age_seconds": 42,
               "source": "on_demand", "vehicle_count": 118},
  "query": "4063",
  "ambiguous": true,
  "matches": [
    {
      "number": "4063", "match": "exact",
      "vehicle_id": "a1b2c3", "vin": null, "active": true,
      "status": "STATIONARY", "online": true,
      "position": {"lat": 35.1, "lon": -111.6}, "location": null,
      "speed": 0.0, "heading": null, "odometer": null,
      "last_report_at": "2026-11-01T10:14:30Z", "report_age_seconds": 72,
      "driver": {"id": "d-123", "name": "Test Driver", "phone": "+15550100001"},
      "duty": {"status": "DS_OFF", "on_duty": false, "personal_conveyance": false,
               "yard_move": false, "username": "testdriver",
               "fetched_at": "2026-11-01T10:15:40Z", "age_seconds": 2},
      "duty_error": null
    },
    {
      "number": "4063z", "match": "variant",
      "...": "same fields",
      "duty": {"status": "DS_D", "on_duty": true, "...": "..."},
      "duty_error": null
    }
  ]
}
```

`duty` holds the driver's latest-driver-status:

- `status`: the raw `current_status` code, such as `DS_D`, `DS_ON`, `DS_OFF`,
  `DS_SB`, `DR_IND_PC` or `DR_IND_YM`. It is null when DriveHOS has none.
- `on_duty`:
  - true for `DS_D`, `DS_ON` and `DR_IND_YM`;
  - false for `DS_OFF`, `DS_SB` and `DR_IND_PC` (Personal Conveyance);
  - null for anything else.
  Personal Conveyance is also flagged in `personal_conveyance`. The caller
  decides whether a PC driver who is moving counts as driving.
- `duty` is null when there is no driver, when `duty=0` was passed, or when the
  lookup failed. `duty_error` then says why:
  - `upstream_unavailable` or `upstream_auth_rejected`: the lookup failed;
  - `skipped`: more than 3 drivers would have been looked up;
  - null: nothing was asked.

A unit that is not in the fleet returns 404 `unit_not_found`, and the body
includes `snapshot`.

### `GET /api/internal/vitality/driver?company=jrd&driver_id=d-123[&duty=1][&provider=auto]`

```json
{
  "api_version": 1,
  "company": {"id": 7, "name": "JRD", "active": true},
  "provider": "vitality",
  "driver": {"id": "d-123", "name": "Test Driver", "phone": "+15550100001"},
  "duty": {"status": "DS_D", "on_duty": true, "personal_conveyance": false,
           "yard_move": false, "username": "testdriver",
           "fetched_at": "2026-11-01T10:15:40Z", "age_seconds": 0},
  "duty_error": null
}
```

`driver_id` may contain only letters, digits, `-` and `_` (at most 64
characters). A driver DriveHOS does not know returns 404 `driver_not_found`.

## Errors

Every error body looks like
`{"error": "<code>", "message": "<English text>", ...extra}`.

| HTTP | `error` | When |
|---|---|---|
| 400 | `bad_request` | A parameter is missing, too long or malformed (`company`, `unit`, `driver_id`, `max_age`, `duty`, `provider`). |
| 401 | `unauthorized` | `X-Api-Key` is missing or wrong. |
| 404 | `not_found` | Unknown route, or `INTERNAL_API_KEY` is not set. |
| 404 | `unknown_company` | No company matches `company`. |
| 404 | `not_configured` | The company has no key for that provider. |
| 404 | `unit_not_found` | No vehicle could be this unit (`snapshot` is included). |
| 404 | `driver_not_found` | DriveHOS knows no such driver for this company. |
| 409 | `company_ambiguous` | Several companies match (`company_ids` is included). Set an alias. |
| 503 | `provider_not_configured` | This bot has no provider key for that provider. |
| 503 | `upstream_unavailable` | DriveHOS failed (HTTP error, network). May include `retry_after_seconds`. |
| 503 | `upstream_auth_rejected` | DriveHOS refused the carrier key with both provider keys. |
| 503 | `upstream_empty` | DriveHOS returned no vehicles for the carrier (its feed stopped). |
| 503 | `upstream_timeout` | DriveHOS did not answer within `INTERNAL_API_UPSTREAM_TIMEOUT`. Retry shortly. |
| 500 | `internal_error` | A bug. The details are in the bot's log. |

## Railway: what the owner has to do

Nothing changes until `INTERNAL_API_KEY` is set. Without it, no port is opened.

1. On the nexus-hos service, set `INTERNAL_API_KEY` to a long random value.
   Put the same value in the driver-alerts service's settings, under the name
   its client will read.
2. Pick how driver-alerts reaches nexus-hos:
   - **Private networking (preferred).** Both services must be in the same
     Railway project. If they are, driver-alerts calls
     `http://<nexus-hos service>.railway.internal:<PORT>/api/internal/vitality/...`.
     Railway injects `PORT`; you can also pin it by setting `PORT` (for
     example `8080`) on nexus-hos. This bot binds every interface, IPv6
     included.
   - **Public domain.** Use this if the two services are in different
     projects. Generate a domain for nexus-hos under Settings → Networking,
     with its target port set to `PORT`. The API key is then the only
     protection.
3. Optional: set `INTERNAL_API_COMPANY_ALIASES` (for example `jrd=<company id>`)
   if a driver-alerts slug does not match a nexus-hos company name. Check with
   `GET /api/internal/vitality/companies`.
4. Optional: set a healthcheck path of `/health`. Leave it unset if you would
   rather not tie the worker's deploys to the web server.
5. The Procfile stays `worker: python app.py`. The web server runs inside that
   same process.
