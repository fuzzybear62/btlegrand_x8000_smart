# Legrand / Bticino Smarther X8000 — Home Assistant Integration

A fault-tolerant Home Assistant integration for **Legrand / Bticino Smarther X8000**
chronothermostats, built for the *non-Netatmo* firmware line (the "**NOT Netatmo**"
variant that talks to the Legrand *Eliot / Works with Legrand* developer cloud, not
the Netatmo Energy API).

The design goal is **stability under a strict daily API quota**. The Legrand
developer cloud enforces a hard daily call limit; exceeding it returns `429` and can
temporarily lock the account. This integration layers an adaptive polling scheduler,
a budget-aware throttle, and a fail-fast cool-down state machine on top of a
cloud-to-cloud (C2C) webhook push so that responsiveness stays high while call
volume stays inside the quota.

> **`iot_class`: `cloud_polling`.** The integration is classified as *cloud polling*.
> It reaches the devices exclusively through the Legrand cloud REST API on an
> adaptive polling schedule. Incoming C2C webhooks are an *optimization on top* of
> polling (they deliver instant state pushes and do **not** count against the API
> quota), but the cloud-polling loop remains the source of truth and the fallback,
> so `cloud_polling` — not `cloud_push` — is the correct classification.

---

## Table of Contents

1. [Features](#features)
2. [Requirements](#requirements)
3. [Installation](#installation)
   - [Via HACS (recommended)](#via-hacs-recommended)
   - [Manual installation](#manual-installation)
4. [Configuration](#configuration)
   - [Setup flow](#setup-flow)
   - [Tunable options](#tunable-options)
   - [Reconfiguring after install](#reconfiguring-after-install)
5. [Architecture](#architecture)
6. [Core Logic & Algorithms](#core-logic--algorithms)
7. [Entities](#entities)
8. [Diagnostics](#diagnostics)
9. [Logging](#logging)
10. [Troubleshooting](#troubleshooting)
11. [Disclaimer](#disclaimer)

---

## Features

- **Full climate control** — set operating mode (Auto / Manual / Boost / Off) and
  target temperature; read ambient temperature, humidity, and heating/idle load
  state.
- **Adaptive "smart" polling** — poll frequency is derived per device from its
  current mode, so idle/unused zones cost far fewer API calls than active ones.
- **Budget safety net** — a stateless throttle that widens polling intervals as the
  remaining daily quota shrinks, so the account reaches midnight without a ban.
- **Fail-fast + cool-down** — an authentication or rate-limit failure on a single
  device aborts the whole cycle immediately and enters a timed cool-down, instead of
  hammering the API into a longer ban.
- **C2C webhook push** — instant updates when the setpoint or mode is changed from
  the physical device or the Legrand app; inbound webhooks are free (they don't
  consume quota).
- **Persistent token storage** — OAuth tokens are refreshed automatically and
  persisted to the config entry, surviving restarts.
- **Granular telemetry** — diagnostic sensors for total and per-device API usage and
  for the number of polls the optimizer skipped.
- **Live retuning** — every tuning parameter is exposed as a Number/Switch entity and
  takes effect immediately, with no reload required.

---

## Requirements

- Home Assistant **2024.4.0** or newer (required by the reconfigure flow).
- A **Legrand developer account** (*Works with Legrand* / Eliot) with a registered
  application, providing:
  - **Client ID**
  - **Client Secret**
  - **Subscription Key**
- A publicly reachable **Home Assistant external URL** (HTTPS). This is required for
  the C2C webhook: the Legrand cloud must be able to `POST` push updates to your
  instance. Configure it under *Settings → System → Network → Home Assistant URL*.

---

## Installation

### Via HACS (recommended)

This repository ships a `hacs.json` and is installable as a **HACS custom
repository**:

1. In Home Assistant, open **HACS → Integrations**.
2. Open the top-right menu (⋮) and choose **Custom repositories**.
3. Add the repository URL
   `https://github.com/fuzzybear62/btlegrand_x8000_smart` and select the
   **Integration** category.
4. The card **"Legrand/Bticino Smarther x8000 NOT Netatmo"** now appears in HACS —
   click **Download**.
5. **Restart Home Assistant.**
6. Add the integration: **Settings → Devices & Services → Add Integration →**
   *Legrand/Bticino Smarther x8000*.

### Manual installation

1. Copy the folder `custom_components/btlegrand_x8000/` into your Home Assistant
   `config/custom_components/` directory. The final path must be
   `config/custom_components/btlegrand_x8000/manifest.json`.
2. **Restart Home Assistant.**
3. Add the integration as in step 6 above.

---

## Configuration

### Setup flow

The config flow is a three-step wizard (single instance only):

1. **Credentials** — enter your **Client ID**, **Client Secret**, **Subscription
   Key**, and confirm the **Home Assistant external URL** (pre-filled from your
   network settings).
2. **Authorization code** — the flow shows a Legrand authorization URL. Open it, log
   in, approve access, then **paste the full redirected browser URL** back into the
   form. The integration extracts the OAuth code and exchanges it for access and
   refresh tokens.
3. **Thermostat selection** — pick which of the discovered chronothermostats to add.

The refresh token is stored in the config entry and used to renew the access token
automatically; you should not need to repeat the authorization step under normal
operation.

### Tunable options

All parameters below are exposed as live **Number** / **Switch** entities under the
integration's *"BtLegrand Service"* device. Changes apply immediately (they mutate
the running coordinator and persist to the config entry) — no reload needed.

| Option | Entity type | Default | Range | Description |
| --- | --- | --- | --- | --- |
| **Update Interval** | Number | `15 min` | 1–120 min | Base polling interval for *active* devices. Lower = more responsive, more calls. |
| **Passive Zone Multiplier** | Number | `4 ×` | 1–12 | How much slower *passive* (Off / anti-frost) zones are polled vs active ones. `1` treats them like active zones; higher = fewer calls on dormant zones. |
| **Smart Energy Saving** | Switch | `ON` | — | Enables adaptive per-device scheduling (passive devices polled less often) and the budget safety net. |
| **Daily API Quota** | Number | `500` | 100–10000 | Your Legrand account's daily call limit. Drives the budget safety net. (Starter Kit default is 500.) |
| **Cool Down Interval** | Number | `60 min` | 15–180 min | Pause duration after a rate-limit / auth failure before the next attempt. |
| **Notify Errors** | Switch | `ON` | — | Raise a persistent notification when the integration pauses due to an API error. |
| **Webhook Debounce** | Number | `1.0 s` | 0.5–5.0 s | Coalescing window for rapid webhook bursts (e.g. sliding the device's touch bar). |

> **Rule of thumb.** *Fewer calls:* raise **Update Interval** and/or **Passive Zone Multiplier**, or lower **Daily API Quota** (throttling engages sooner). *More responsive:* lower both. `Smart Energy Saving = OFF` ignores both axes and polls every device at the fixed **Update Interval**.

### Reconfiguring after install

Three install-time choices can be changed in place — without removing and
re-adding the integration — via **Settings → Devices & Services → (entry) → ⋮ →
Reconfigure**, which opens a menu with three options: **external URL**,
**Legrand credentials**, and **thermostat selection**. None of them touches the
running integration until the change is validated — if you cancel or
authorization fails, the entry keeps working on its current settings, so a
reconfigure can never leave you worse off.

**Change external URL (webhook target).** Useful when your domain / DDNS changes
or you enable/disable a reverse proxy: without it, the Legrand push subscription
would keep pointing at the old URL and push updates would silently stop.
Reconfigure retargets the subscription to the new URL and best-effort removes the
subscription bound to the old one — no re-authentication and no device
re-selection. (Removing the old subscription is non-fatal: if it can't be reached,
it is left as a harmless orphan you can clean up manually.)

**Renew Legrand credentials (re-authenticate).** The `subscription key`,
`client id` and `client secret` from the Legrand developer portal belong to the
same registration and rotate together; a rotation also invalidates the OAuth
tokens. This option pre-fills the current values, lets you edit the ones that
changed, then runs the same browser authorization as first setup to obtain fresh
tokens. Your external URL and selected thermostats (and their history) are
preserved. The integration keeps running on the current credentials until the new
ones are validated against the Legrand cloud.

**Add / remove thermostats.** The exposed thermostats are chosen at install and
are not re-discovered on reload, so a thermostat added or removed in the Legrand
app won't appear/disappear on its own. This option re-scans your plant(s) and
shows the full list with the currently-added ones pre-checked: check new ones to
add them, uncheck ones to remove them. No re-authentication is needed. Removed
thermostats become **unavailable** — you can then delete each one from its device
page (**⋮ → Delete**); still-selected devices and their history are untouched. If
removing a thermostat empties a whole plant, that plant's push subscription is
cleaned up automatically.

---

## Architecture

```
config_flow ──► __init__.async_setup_entry
                     │  builds  BticinoX8000Api  +  BticinoCoordinator
                     │  registers HA webhook + C2C subscription
                     ▼
              BticinoCoordinator  ◄──── push ──── webhook.py  ◄──── Legrand cloud
               (single source of truth)
                     │  adaptive poll loop  (cloud_polling)
                     ▼
        climate · sensor · select · number · switch · button   (CoordinatorEntity)
```

- **`BticinoX8000Api`** — serialized HTTP client (`Semaphore(1)`, one in-flight
  request), OAuth token refresh on `401`, rate-limit handling on `429`, and a
  persistent usage counter (per-device + global, reset at midnight).
- **`BticinoCoordinator`** — a `DataUpdateCoordinator` holding all live tuning state.
  It runs the adaptive poll loop *and* ingests webhook pushes, then fans both out to
  the entities.
- **Entities** are `CoordinatorEntity` subclasses reading
  `coordinator.data[topology_id]`. The tuning entities (`number`/`switch`/`button`)
  write straight into coordinator memory **and** persist to the config entry — there
  is intentionally no separate Options flow.

A fuller `file:line` map lives in [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Core Logic & Algorithms

### 1. Smart polling (state-aware scheduling)

When **Smart Energy Saving** is on, the coordinator classifies each device every
cycle instead of polling them uniformly:

- **Active devices** — in `Manual`, `Boost`, or `Automatic` mode. Polled at the base
  **Update Interval** (default 15 min) even when the boiler is idle, so demand
  changes are captured promptly.
- **Passive devices** — in `Off` or `Protection` (anti-frost) mode. Their interval is
  multiplied by the **Passive Zone Multiplier** (default 4× → 60 min), on the
  assumption they won't change without user action.

For a mix of unused zones (guest rooms, off-season devices) the default 4× avoids
roughly **75%** of their would-be calls. Every skipped poll is counted by the *Smart
Polling Skips* diagnostic sensor.

> **Webhooks are free.** A push from Legrand carries the full device state, so it
> updates the entity **without** an API call *and* resets the device's poll timer —
> i.e. real changes make polling *rarer*, not more frequent. Polling is only the
> safety net for missed pushes.

### 2. Budget safety net (quota-aware throttle)

The coordinator continuously compares `api_call_count` against the **Daily API
Quota**. As the remaining budget falls, it tightens the schedule so the day can be
finished without a ban. The tiers are **derived from the two user knobs** rather than
fixed magic numbers: thresholds scale with the quota, active intervals scale with the
**Update Interval**, and passive intervals are `active × Passive Zone Multiplier`.

| Remaining budget | Mode | Active interval | Passive interval | Example @ defaults |
| --- | --- | --- | --- | --- |
| **≥ 20% quota** | Normal | `base` | `base × M` | 15 / 60 min |
| **< 20% quota** | Economy | `base × 2` | `base × 2 × M` | 30 / 120 min |
| **< 8% quota** | Survival | `base × 4` | `base × 4 × M` | 60 / 240 min |
| **< 2% quota** (min 5 calls) | Frozen | — scheduled polling paused — | | rely on webhooks |

*(`base` = Update Interval, `M` = Passive Zone Multiplier. At the defaults — 15 min,
quota 500, M=4 — the numbers are identical to previous releases.)*

**Frozen** is the end-of-day safety floor: with almost no budget left, scheduled
polls stop entirely and the integration relies on the free webhook push until the
quota resets at local midnight. This prevents a self-inflicted rate-limit ban.

Entering *Economy* logs at `INFO`; entering *Survival* and *Frozen* log at `WARNING`.
The live state is exposed by the **Polling Tier** diagnostic sensor.

### 3. Fail-fast & cool-down

The Legrand API punishes hammering, so failures short-circuit the cycle:

1. **Fail-fast** — an authentication error or a `429` on *any* device aborts the
   entire update cycle at once, rather than repeating the failing call across the
   remaining devices.
2. **Cool-down** — the integration enters a dormant state for the **Cool Down
   Interval** (default 60 min) during which no API calls are made. If *Notify
   Errors* is on, a persistent notification is raised and a `{DOMAIN}_event` fires on
   the bus (`type: rate_limit_exceeded`).
3. **Auto-recovery** — when the timer expires, a single probe call is attempted; on
   success, normal operation resumes automatically.

### 4. Webhook subscriptions (removal & reinstall)

To receive instant push updates, the integration registers a **C2C subscription** on
the Legrand cloud pointing at your Home Assistant webhook URL.

When you *remove* the integration, this cloud-side subscription is **intentionally
left in place** — by design, not an oversight:

- The webhook URL is stable (derived from your external URL + a fixed webhook id), so
  a later **reinstall reuses the existing subscription**: the re-subscribe request
  returns `409 Already Active`, which is treated as success. Nothing is recreated.
- An orphaned subscription is harmless in the meantime: inbound webhooks **don't
  count against your daily quota**, and once the HA webhook is gone the pushes are
  simply answered with `404`.

Note the asymmetry with the **Reconfigure** flows: removing the integration
leaves the subscription in place (above), but reconfiguring *does* clean up the
now-stale subscription automatically — changing the **external URL** deletes the
old-URL subscription, and dropping the last thermostat of a plant via **Add /
remove thermostats** deletes that plant's subscription. Both are best-effort and
reversible (a re-subscribe just returns `409`/`200` on the next reload).

For any leftover stale subscription you still want to tidy up by hand, the API
client exposes `get_subscriptions_c2c_notifications` /
`delete_subscribe_c2c_notifications` for a manual one-off cleanup.

---

## Entities

Per selected thermostat:

| Entity | Platform | Notes |
| --- | --- | --- |
| **Climate** | `climate` | Heat / Cool / Auto / Off, target temperature, preset (Boost). Shows **Unavailable** (not *Off*) when the gateway is unreachable. |
| **Temperature** | `sensor` | Ambient temperature (measurement, long-term statistics). |
| **Humidity** | `sensor` | Relative humidity (measurement, LTS). |
| **Target Temperature** | `sensor` | Current setpoint (measurement, LTS). |
| **Mode** | `sensor` | Raw operating mode (text). |
| **Status** | `sensor` | Load state — heating / idle (text). |
| **Boost Time Remaining** | `sensor` | Minutes left on an active Boost (0 when inactive). |
| **Program** | `sensor` / `select` | Active schedule program; the select lets you change it. |
| **Boost** | `select` | Off / 30 / 60 / 90 min. |
| **API Usage (per device)** | `sensor` | Diagnostic: calls that targeted this device today. |

Under the singleton *"BtLegrand Service"* device:

| Entity | Platform | Notes |
| --- | --- | --- |
| **Update Interval** | `number` | Base active interval. |
| **Passive Zone Multiplier** | `number` | How much slower Off/anti-frost zones are polled. |
| **Cool Down Interval** | `number` | Post-error pause. |
| **Webhook Debounce** | `number` | Push coalescing window. |
| **Daily API Quota** | `number` | Account limit for the budget net. |
| **Notify Errors** | `switch` | Persistent notification on error. |
| **Smart Energy Saving** | `switch` | Adaptive scheduling on/off. |
| **Force Token Refresh** | `button` | Manually renews the OAuth token (works even when auth is broken). |
| **API Call Count (global)** | `sensor` | Diagnostic: total calls today (resets at midnight). |
| **Smart Polling Skips** | `sensor` | Diagnostic: polls avoided by the optimizer. |
| **Polling Tier** | `sensor` | Diagnostic: current throttle tier (disabled/normal/economy/survival/frozen) + enforced intervals in attributes. |
| **Projected Daily Calls** | `sensor` | Diagnostic: calls we are on track to make by midnight; compare to the quota. |

> The tuning entities are always `available` (even while the API is rate-limited), so
> you can retune the integration out of a cool-down.

---

## Diagnostics

- **API Call Count (Global)** — total calls made today; resets at local midnight. The
  count is persisted to `.storage/bticino_x8000.api_usage` so it survives restarts.
- **API Usage (Per Device)** — how many of those calls targeted each thermostat.
- **Smart Polling Skips** — how many polls the optimizer avoided. A higher number
  means higher efficiency.
- **Polling Tier** — the throttle state right now (`disabled` / `normal` / `economy` /
  `survival` / `frozen`). Its attributes expose the exact active/passive intervals
  being enforced and the remaining budget, so the adaptive behaviour is observable.
- **Projected Daily Calls** — today's usage extrapolated to midnight. If it stays
  **below** the Daily API Quota, smart polling is doing its job; a value above the
  quota (attribute `over_budget: true`) means the current pace is too fast. This is
  the single best gauge of real effectiveness. *(Reports the raw count during the
  first hour of the day, when extrapolation would be too noisy.)*

Raw device payloads are exposed via each entity's `extra_state_attributes` **only
when the integration logger is at `DEBUG`** — they are debug aids, not normal state.

---

## Logging

The integration follows Home Assistant's standard level conventions:

| Level | When it is used | Examples |
| --- | --- | --- |
| `DEBUG` | Verbose per-cycle / developer detail. Off by default. | webhook merge results, per-device skip decisions, token-refresh plumbing, raw HTTP status. |
| `INFO` | Sparse operational milestones — setup steps, user-initiated actions, once-per-day/hour events. | setup started, C2C subscribed, user changed a setting, quota reset at midnight, token persisted. |
| `WARNING` | Unexpected but automatically handled. | `401` triggering a refresh, transient `5xx` being retried, entering *Survival* budget mode, a per-device update returning a bad status. |
| `ERROR` | A terminal failure the user may need to act on. | non-retriable `4xx` auth failure (bad credentials / code), token refresh giving up, `429` persistent rate limit, unexpected exceptions. |

A transient server error that is retried and recovers logs only at `WARNING`/`DEBUG`
— it does **not** leave a spurious `ERROR` behind. `ERROR` is reserved for failures
that actually surface to the user.

To enable verbose logs for this integration only, add to `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.btlegrand_x8000: debug
```

---

## Troubleshooting

### "Unavailable" vs "Off"

- **Off** — the thermostat is powered but switched off in software.
- **Unavailable** — the integration cannot reach the Legrand cloud, or the cloud
  cannot reach your physical gateway (gateway offline). Check connectivity.

### API count shows 0 after a restore

The counter is persisted to `.storage/bticino_x8000.api_usage`. Restoring a backup or
deleting the storage folder resets it. The budget safety net adapts to the new
(lower) count automatically and normalizes over the next 24 hours.

### Rate limit (`429`) errors

If you hit `429`, you have exceeded your account's daily limit:

1. Check whether other apps/integrations share the same Legrand account.
2. Ensure **Smart Energy Saving** is on.
3. Increase the **Update Interval** (e.g. 20–30 min).
4. Confirm the **Daily API Quota** matches your actual account limit so the budget
   net throttles correctly.

### No push updates (only periodic polling)

C2C push requires the Legrand cloud to reach your HA webhook. Verify your **external
URL** is a valid, publicly reachable HTTPS address and that any reverse proxy
forwards the webhook path.

---

## Disclaimer

This is a community custom integration and is **not** officially affiliated with,
endorsed by, or supported by Legrand or Bticino. Use at your own risk.
