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
| **Smart Energy Saving** | Switch | `ON` | — | Enables adaptive per-device scheduling (passive devices polled less often) and the budget safety net. |
| **Daily API Quota** | Number | `500` | 100–10000 | Your Legrand account's daily call limit. Drives the budget safety net. (Starter Kit default is 500.) |
| **Cool Down Interval** | Number | `60 min` | 15–180 min | Pause duration after a rate-limit / auth failure before the next attempt. |
| **Notify Errors** | Switch | `ON` | — | Raise a persistent notification when the integration pauses due to an API error. |
| **Webhook Debounce** | Number | `1.0 s` | 0.5–5.0 s | Coalescing window for rapid webhook bursts (e.g. sliding the device's touch bar). |

### Reconfiguring after install

The thermostat selection is fixed at install time, but two things can be changed
in place via **Settings → Devices & Services → (entry) → ⋮ → Reconfigure**, which
opens a menu with two options. Neither touches the running integration until the
change is validated — if you cancel or authorization fails, the entry keeps
working on its current settings.

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
  multiplied by **4×** (default 60 min), on the assumption they won't change without
  user action.

For a mix of unused zones (guest rooms, off-season devices) this avoids roughly
**75%** of their would-be calls. Every skipped poll is counted by the *Smart Polling
Skips* diagnostic sensor.

### 2. Budget safety net (quota-aware throttle)

The coordinator continuously compares `api_call_count` against the **Daily API
Quota**. As the remaining budget falls, it tightens the schedule so the day can be
finished without a ban. The tiers are named constants (no magic numbers), and each
tier's passive interval is derived as `active × 4`:

| Remaining calls | Mode | Active / Passive interval |
| --- | --- | --- |
| **> 100** | Normal | 15 min / 60 min |
| **≤ 100** | Economy | 30 min / 120 min |
| **≤ 40** | Survival | 60 min / 240 min |

Entering *Economy* logs at `INFO`; entering *Survival* logs at `WARNING`.

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

If you change your external URL across installs and want to tidy up stale
subscriptions on the Legrand side, the API client exposes
`get_subscriptions_c2c_notifications` / `delete_subscribe_c2c_notifications` for a
manual one-off cleanup. These are deliberately never called automatically.

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
| **Cool Down Interval** | `number` | Post-error pause. |
| **Webhook Debounce** | `number` | Push coalescing window. |
| **Daily API Quota** | `number` | Account limit for the budget net. |
| **Notify Errors** | `switch` | Persistent notification on error. |
| **Smart Energy Saving** | `switch` | Adaptive scheduling on/off. |
| **Force Token Refresh** | `button` | Manually renews the OAuth token (works even when auth is broken). |
| **API Call Count (global)** | `sensor` | Diagnostic: total calls today (resets at midnight). |
| **Smart Polling Skips** | `sensor` | Diagnostic: polls avoided by the optimizer. |

> The tuning entities are always `available` (even while the API is rate-limited), so
> you can retune the integration out of a cool-down.

---

## Diagnostics

- **API Call Count (Global)** — total calls made today; resets at local midnight. The
  count is persisted to `.storage/bticino_x8000.api_usage` so it survives restarts.
- **API Usage (Per Device)** — how many of those calls targeted each thermostat.
- **Smart Polling Skips** — how many polls the optimizer avoided. A higher number
  means higher efficiency.

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
