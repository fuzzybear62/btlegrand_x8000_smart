# ARCHITECTURE — btlegrand_x8000 (index)

Custom Home Assistant integration for **BTicino / Legrand Smarther X8000**
thermostats (a fork rebranded away from the Netatmo path). `iot_class:
cloud_polling` with an adaptive/"smart" polling budget layered on top of a C2C
(cloud-to-cloud) webhook push.

This file is the map. Read it instead of re-reading the modules; jump to the
`file:line` anchors when you need detail. Keep it in sync when the code moves.

Package root: `custom_components/btlegrand_x8000/`

---

## 1. Data & control flow (one paragraph)

`config_flow` collects Legrand OAuth app credentials + selected thermostats →
`__init__.async_setup_entry` builds the shared `BticinoX8000Api`, the
`BticinoCoordinator`, registers the webhook + C2C subscriptions, then forwards
to the 6 platforms. The **coordinator** is the single source of truth: it holds
live tuning state (intervals, flags, counters), polls the API on an adaptive
schedule (`_async_update_data`), and also ingests **push** updates from the
webhook (`update_from_webhook`). Entities are `CoordinatorEntity` subclasses
reading `coordinator.data[topology_id]`. The `number`/`switch`/`button` config
entities write straight into coordinator memory **and** persist to
`entry.options` (there is no OptionsFlow — this is deliberate).

Coordinator data shape (flat): `{ topology_id: <chronothermostat dict>, ... }`.

---

## 2. File map

| File | Lines | Role |
|------|------:|------|
| `manifest.json` | 14 | domain `btlegrand_x8000`, v1.0.0, cloud_polling, no deps |
| `const.py` | 93 | DOMAIN, endpoints, `CONF_*` option keys, defaults, tuning bounds, derived tier policy |
| `__init__.py` | 121 | setup/unload, C2C subscription, webhook registration |
| `auth.py` | 230 | OAuth code exchange + token refresh (shared aiohttp session) |
| `api.py` | 403 | `BticinoX8000Api` — HTTP, rate-limit/401 handling, usage Store |
| `coordinator.py` | 578 | `BticinoCoordinator` — adaptive polling, cool-down, webhook merge |
| `webhook.py` | 99 | `BticinoX8000WebhookHandler` — validates push, fans out to coordinators |
| `config_flow.py` | 598 | 3-step user flow (creds → auth code → select thermostats) + reconfigure menu (external URL / renew credentials) |
| `climate.py` | 452 | `BticinoX8000Climate` — the thermostat entity |
| `sensor.py` | 803 | 7 per-device sensors + 4 singleton diagnostics (API count, skips, polling tier, projected calls) |
| `select.py` | 358 | Program + Boost select entities |
| `number.py` | 338 | 5 CONFIG numbers (interval, cool-down, debounce, quota, passive multiplier) |
| `switch.py` | 215 | 2 CONFIG switches (notify errors, smart polling) |
| `button.py` | 81 | Force-token-refresh button |
| `services.yaml` | 2 | empty (services removed; use entities) |
| `translations/` | — | en, it, de, es, fr, pt |

`PLATFORMS` (`__init__.py`): climate, sensor, select, number, switch, button.

---

## 3. const.py — keys & tuning (`const.py`)

- Option keys: `CONF_UPDATE_INTERVAL="update_interval"`,
  `CONF_COOL_DOWN="cool_down_interval"`, `CONF_DEBOUNCE="webhook_debounce"`,
  `CONF_NOTIFY_ERRORS="notify_errors"`,
  `CONF_BTLG_DAILY_QUOTA="btlg_api_daily_quota"`,
  `CONF_SMART_POLLING="smart_polling_enabled"`,
  `CONF_PASSIVE_MULTIPLIER="passive_multiplier"`.
- Bounds used by `number.py`: `MIN/MAX_UPDATE_INTERVAL`, `MIN/MAX_COOL_DOWN`,
  `MIN/MAX_DEBOUNCE` (0.5–5.0s), `MIN/MAX_BTLG_DAILY_QUOTA` (100–10000),
  `MIN/MAX_PASSIVE_MULTIPLIER` (1–12).
- `DEFAULT_PASSIVE_MULTIPLIER` (4) — now a user knob (`passive_multiplier`),
  read into `coordinator.passive_multiplier`.
- Adaptive-throttle tiers are **derived** from the two user knobs, not fixed:
  thresholds are fractions of the quota (`ECONOMY_BUDGET_FRACTION`=0.20,
  `SURVIVAL_BUDGET_FRACTION`=0.08, `FROZEN_BUDGET_FRACTION`=0.02 /
  `FROZEN_BUDGET_MIN`=5); active intervals are multiples of `update_interval`
  (`ECONOMY_INTERVAL_FACTOR`=2, `SURVIVAL_INTERVAL_FACTOR`=4); passive =
  `active × passive_multiplier`. Tier labels `TIER_*`. At defaults (15 min /
  quota 500 / 4×) the enforced values equal the old constants (100/40, 30/60).
- `PROJECTION_MIN_DAY_FRACTION` (0.04) — below this the projected-calls sensor
  reports the raw count instead of a noisy extrapolation.
- `CLIENT_ID` / `CLIENT_SECRET` / `SUBSCRIPTION_KEY` are empty strings (supplied
  by the user at config-flow time).
- Release cleanup removed the dead scaffolding (`BTICINO_DAILY_RESERVE`,
  `WEIGHT_*`, `COORDINATOR_HEARTBEAT`; and the unused `SERVICE_SET_*` / `ATTR_*` /
  `CONF_CLIENT_ID`-style / `AUTH_CALLBACK_*` / `AUTH_CHECK_ENDPOINT` constants).
  `INTEGRATION_VERSION` / `MIN_REQUIRED_HA_VERSION` are kept as release-workflow
  placeholders (consumed by tooling, not the Python code).

## 4. __init__.py (`__init__.py`)

- `async_setup_entry:` builds api + coordinator; `await
  coordinator.async_config_entry_first_refresh()` guarded so a boot-time rate
  limit does **not** raise `ConfigEntryNotReady` into a retry loop.
- C2C subscription block `:67–108` — skipped while in cool-down (`:72`); treats
  HTTP 409 as "already subscribed" (`:99`).
- `async_unload_entry:116` — unloads platforms; **does not** unregister the
  webhook or remove C2C subscriptions (see REVIEW).

## 5. auth.py (`auth.py`)

- `exchange_code_for_tokens`, `refresh_access_token` (retry/backoff, shared
  session). Refresh-token fallback preserved. 5xx raises `ValueError` caught by a
  generic `except` (minor intent mismatch — not retried).

## 6. api.py — `BticinoX8000Api` (`api.py`)

- Serialization: `Semaphore(1)` — one in-flight request at a time.
- Header auth: `header["Authorization"] = data["access_token"]` (**raw token, no
  "Bearer " prefix**) set at `:58`, re-set on refresh at `:313`.
- `call_count` property (`:89`) → `usage_stats["total"]`.
- `usage_stats` (`:70`) — `{"total": n, <device_id>: n, ...}`; per-device counter
  parsed from URL in `_increment_usage_counter:152` (`/value/{id}`); global bump
  `:155`, per-device `:170`. Persisted via HA `Store` (`_save_usage_data`),
  restored with midnight/date reset (`:110–147`).
- Request loop status handling: 401 → locked refresh (`:241`), 429 →
  `RateLimitError` (`:262`), 5xx → retry.
- Custom exceptions: `BticinoApiError`, `RateLimitError`, `AuthError`.
- Command write: `set_chronothermostat_status(plant_id, topology_id, payload)`
  (used by climate/select). `set_subscribe_c2c_notifications` (used by __init__).
  `get_subscriptions_c2c_notifications` / `delete_subscribe_c2c_notifications`
  are still NOT called on removal — the C2C subscription is intentionally left in
  place so a reinstall reuses it (409 = success). Their one real caller is the
  **reconfigure flow** (`config_flow._async_cleanup_old_subscriptions`), which
  best-effort deletes the *old-URL* subscriptions when the external URL changes.
  See REVIEW "Decision — C2C subscription".
- M1/L2 fixed: the double-checked-lock now compares against a `sent_token`
  snapshot; the needless f-string is gone.

## 7. coordinator.py — `BticinoCoordinator` (`coordinator.py`)

Live state initialized in `__init__` from `entry.options`:
`normal_interval`, `cool_down_interval`, `debounce_time`, `notify_errors`,
`daily_api_quota`, `smart_polling_enabled`, `passive_multiplier`,
`skipped_count`, `in_cool_down`. Smart-polling diagnostics also live here:
`current_tier`, `current_interval_active`, `current_interval_passive`.

Key methods:
- `projected_daily_calls` (property) — `call_count / day_fraction` (local day);
  returns the raw count before `PROJECTION_MIN_DAY_FRACTION`. Backs the
  *Projected Daily Calls* diagnostic sensor.
- `_async_update_data:` (adaptive poll loop):
  - budget net — if smart polling, derive tier from
    `remaining = daily_api_quota - call_count`: **frozen** (< `FROZEN` floor →
    skip all scheduled polls, rely on free webhooks), then a **pacing veto**
    (`on_track = projected_daily_calls < quota` → stay Normal; relaxation only,
    never applied to frozen), then **survival**, **economy**, **normal**.
    Thresholds are quota fractions, active intervals are
    `normal_interval × factor`, passive = `active × passive_multiplier`.
    Publishes `current_tier` + enforced intervals for the diagnostics.
  - per-device skip logic (frozen short-circuit, then active vs passive by
    `mode`), copies old data on skip.
  - success/cool-down recovery `:228–246`; inline 429 abort `:225–226`.
  - typed excepts `:255–268`; generic safety-net `:270–279`.
- `_trigger_rate_limit_abort:287` — sets `in_cool_down`, fires `{DOMAIN}_event`,
  persistent notification (if `notify_errors`), sets cool-down interval,
  `notify_listeners_only()`, **raises `UpdateFailed`**.
- `async_force_token_refresh:346` (button target).
- `notify_listeners_only:361` → `async_update_listeners()`.
- `update_from_webhook:371` — debounce via monotonic (`:384`), hybrid plant_map
  lookup, `_extract_chronothermostats`, `_get_topology_id`.
- Inline 429 abort at `:225` is shielded from the generic net by an
  `except UpdateFailed: raise` clause (was REVIEW H1, fixed).

## 8. webhook.py (`webhook.py`)

- `BticinoX8000WebhookHandler.handle_webhook` — validates dict, filters keys,
  pushes to every coordinator via `update_from_webhook`. Registered with
  `local_only=False`.

## 9. config_flow.py (`config_flow.py`)

- Steps: `user` (client_id/secret/subscription_key/external_url) → auth-code
  (paste redirected `browser_url`) → `select_thermostats`.
- `async_step_reconfigure`: **menu** with three self-contained sub-flows —
  `reconfigure_url`, `reconfigure_credentials`, `reconfigure_devices`. None
  mutates the running entry until it completes and validates; an aborted/failed
  dialog leaves the production entry untouched.
  - `reconfigure_url`: edit `external_url` (the C2C webhook target) in place.
    Best-effort deletes the old-URL subscriptions via
    `_async_cleanup_old_subscriptions` (uses the loaded `coordinator.api`), then
    `async_update_reload_and_abort` — reload re-subscribes the new URL (409/200).
  - `reconfigure_credentials`: renew `client_id`/`client_secret`/`subscription_key`
    (old values pre-filled) and re-run OAuth. New secrets live only in flow-local
    `self.data` (+ `_reconfig_entry_id`); the OAuth dance reuses
    `async_step_get_authorize_code`, which — when `_reconfig_entry_id` is set —
    finalizes by `async_update_reload_and_abort` **only after** health-check +
    token exchange + `get_plants()==200` validate the new creds. Preserves
    `external_url` and `selected_thermostats` (entity_ids/history kept); plant
    re-selection is skipped.
  - `reconfigure_devices`: re-select exposed thermostats (add/remove). Re-scans
    live topology via the loaded `coordinator.api` (current tokens, no OAuth),
    pre-checks the currently-selected (keyed on `topology_id`), rebuilds
    `selected_thermostats` preserving unchanged entries. Plants that drop out
    entirely get their C2C subscription deleted (`_async_delete_plant_subscriptions`,
    reversible). De-selected devices are **not** force-removed — they go
    "unavailable" and are user-removable via `async_remove_config_entry_device`
    (`__init__.py`), which only permits deleting orphaned thermostats (never a
    still-selected one nor the shared service device).
  - Requires HA ≥ 2024.4 (reconfigure flow); `MIN_REQUIRED_HA_VERSION` bumped to match.
- `single_instance_allowed`. Stores `access_token_expires_on` (datetime) in
  `entry.data`. **No `async_get_options_flow`** — intentional; config numbers/
  switches own the tuning options. All install-time `entry.data` (credentials,
  external_url, device selection) is now user-editable via the reconfigure menu.

## 10. Entities

### climate.py — `BticinoX8000Climate` (`climate.py`)
- `_update_state_from_coordinator` maps thermometer/setPoint/mode/function/
  loadState → hvac_mode/action/preset.
- Optimistic set + revert-on-failure for hvac_mode/temperature/preset.
- `in_cool_down` gates commands; `available` requires `topology_id` in
  `coordinator.data`. `unique_id = {DOMAIN}_{topology_id}_climate`.
- `extra_state_attributes` only when logger DEBUG.

### sensor.py (`sensor.py`)
- `BticinoBaseSensor` + `_get_nested_value` safe path walker (`:176`).
- Per device: Temperature, Humidity, TargetTemperature (all `MEASUREMENT`,
  LTS-enabled), Mode (`MODE_MAP`), Status (`LOAD_STATE_MAP`),
  BoostTimeRemaining (0 when inactive), Program, ThermostatApiCount.
- Singletons: `BticinoApiCountSensor` (`:522`, `TOTAL_INCREASING`, DIAGNOSTIC,
  listens to every coordinator tick), `BticinoSkippedPollsSensor` (`:586`).
- `BticinoThermostatApiCountSensor.native_value:676` →
  `api.usage_stats.get(topology_id, 0)`.

### select.py (`select.py`)
- `BticinoBoostSelect` options `["off","30","60","90"]`; heuristically infers the
  current option from `activationTime` (`_update_state_from_coordinator:163`).
- `BticinoProgramSelect` options = program names; optimistic write + revert.

### number.py (`number.py`)
- `BticinoBaseNumber._update_config_entry:111` — writes `entry.options` (no
  `await` on `async_update_entry`, correct), then conditional refresh.
- `UpdateInterval` (shows `normal_interval`, not the cool-down penalty),
  `CoolDown` (force_refresh=True), `Debounce`, `DailyQuota`.

### switch.py (`switch.py`)
- `NotifyErrors`, `SmartPolling` — mutate coordinator + persist to options.

### button.py (`button.py`)
- `ForceTokenButton` → `coordinator.async_force_token_refresh()`. Always
  available (must work when API is broken).

---

## 11. Cross-cutting conventions
- Every `extra_state_attributes` returns `{}` unless `_LOGGER.isEnabledFor(DEBUG)`
  — raw payloads are debug-only.
- Config entities (`number`/`switch`/`button`) are always `available` so the user
  can retune while the API is down.
- `unique_id` pattern: `{DOMAIN}_{topology_id}_{role}` for device entities,
  `{DOMAIN}_{role}_{entry_id}` for service-level ones.
- Device grouping: real thermostats under `(DOMAIN, topology_id)`; the singleton
  diagnostics/config entities under a virtual "BtLegrand Service"
  `(DOMAIN, entry_id)` device.
