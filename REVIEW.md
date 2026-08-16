# Code review — btlegrand_x8000

Line-by-line pass over every module in `custom_components/btlegrand_x8000/`.
Findings are ordered by severity. Each is verified against the source (not
inferred). "By design" items are listed at the end so they are not re-flagged.

Severity key: **H** = behavioural bug that fires in normal operation ·
**M** = latent/edge bug or real waste · **L** = cosmetic / hygiene.

### Status (fixes applied)

| # | Finding | Status |
|---|---------|--------|
| H1 | 429 abort double-fires | **fixed** — `except UpdateFailed: raise` before the generic net (coordinator.py) |
| M1 | dead double-checked-lock guard | **fixed** — compare live token vs `sent_token` snapshot (api.py) |
| M2 | teardown on unload | **corrected** — webhook *is* removed; only the server-side C2C sub persists (by design). Downgraded to L, no code change |
| M3 | unused budget constants | **fixed** — dead constants removed; the live throttling tiers are now **named constants** and the hardcoded 40/100 + 60/240/30/120 magic numbers eliminated (const.py + coordinator.py) |
| L1 | manifest unbalanced `(` | **fixed** (manifest.json) |
| L2 | needless f-string | **fixed** (api.py) |
| L3 | stale "to be added" comment | **fixed** (number.py) |
| L4 | auth 5xx not retried | **fixed** — 5xx now retries with backoff like a network error (auth.py) |

---

## H1 (fixed) — Rate-limit abort fires **twice** on an HTTP-429 status code
`coordinator.py:225` + `coordinator.py:270-276`

Inside the poll loop, a 429 returned as a *status code* (not raised) does:

```python
if status_code == 429:
    self._trigger_rate_limit_abort(plant_id, topology_id, "HTTP 429 ...")   # :225-226
```

`_trigger_rate_limit_abort` **raises `UpdateFailed`** (`:344`). But that call sits
inside the `try:` block, so the raise is caught by the generic safety net:

```python
except Exception as err:                          # :270
    err_msg = str(err)
    if "429" in err_msg or "Rate Limit" in err_msg:
        self._trigger_rate_limit_abort(...)       # :276  ← second time
```

The message is `"Rate Limit Abort: HTTP 429 Status Code returned"` → matches both
substrings → **the abort runs a second time**: a second `{DOMAIN}_event`
(`type: rate_limit_exceeded`) is fired on the bus and `notify_listeners_only()`
runs again before the second `UpdateFailed` finally propagates.

Impact: any automation listening on `{DOMAIN}_event` triggers twice per rate-limit
hit. (The persistent notification is idempotent by `NOTIFICATION_ID`, so that part
is masked.) The **`RateLimitError` exception path** (`:255-257`) does *not* have
this problem — a raise inside one `except` clause is not caught by a sibling
`except`. Only the inline status-code path is affected.

Fix: after the inline abort, don't let it fall into the generic handler — e.g.
`continue`/re-raise as a distinct type, or drop the inline `:225-226` branch and
rely solely on `RateLimitError` (api.py already raises it at `:262`).

---

## M1 (fixed) — Post-lock "already refreshed by another thread" guard is dead code
`api.py:245`

```python
async with self._token_refresh_lock:
    if self.header["Authorization"] != self.data["access_token"]:   # :245
        _LOGGER.info("Token refreshed by another thread. Retrying request.")
    else:
        ... self._handle_token_refresh() ...
```

`header["Authorization"]` is assigned **the raw `access_token`** (`:58`, and again
on every refresh at `:313`) — the two values are two copies of the same string,
always mutated together. So the condition is *tautologically false*; the "another
thread already refreshed" branch can never run, and the `else` always refreshes.

The classic double-checked-lock wants to compare the token **seen before**
acquiring the lock against the current token:

```python
token_before = self.header["Authorization"]
async with self._token_refresh_lock:
    if self.header["Authorization"] != token_before:   # someone refreshed while we waited
        ...retry with new token...
    else:
        ...refresh...
```

Currently harmless only because `Semaphore(1)` (`api.py`) serializes all requests,
so two coroutines never contend for this lock. If the semaphore is ever widened,
this becomes a real redundant-refresh / token-thrash bug. Latent.

---

## M2 → L — Only the server-side C2C subscription persists on unload (by design)
`__init__.py:116`

**Correction:** the HA webhook **is** torn down — `async_unload_entry` calls
`async_remove_webhook()` (`:119` → `webhook.py:95` `webhook_unregister`). The
original H/M framing was wrong on that half.

What remains: the **Legrand-side C2C subscription** (`__init__.py:84-108`) is not
deleted on unload, even though `api.py:382 delete_subscribe_c2c_notifications`
exists. This is defensible, not a bug:
- setup treats a re-subscribe `409` as "already active" (`:98-99`), so keeping
  the subscription across a reload is correct and free;
- deleting it on every unload would require a `GET subscriptions` +
  `DELETE`, i.e. **two extra API calls per reload** against the very daily quota
  this integration is built to conserve.

The only real downside is an **orphaned subscription when the integration is
permanently removed** (the Legrand side keeps POSTing to a dead webhook id).
Low severity. If desired, delete C2C subs *only* in `async_remove_entry`
(permanent removal), not in `async_unload_entry`. **No code change made.**

---

## M3 (fixed) — Budget-weighting design scaffolded but never wired + magic numbers
`const.py` / `coordinator.py`

**Fix applied in two parts:**
1. The four dead weighted-model constants were removed (replaced by a note).
2. The *live* throttling logic used hardcoded magic numbers — thresholds
   `remaining < 40 / < 100` and intervals `60/240/30/120` min. These are now
   named constants in `const.py`: `BUDGET_SURVIVAL_THRESHOLD`,
   `BUDGET_ECONOMY_THRESHOLD`, `SURVIVAL_ACTIVE_INTERVAL_MIN`,
   `ECONOMY_ACTIVE_INTERVAL_MIN`. The passive interval of each tier is now
   *derived* as `active * PASSIVE_POLLING_MULTIPLIER` (was a duplicated literal),
   so the 4x relationship lives in exactly one place and matches normal mode.

Original description below.

`const.py:96-99` (pre-fix)

`BTICINO_DAILY_RESERVE`, `WEIGHT_ACTIVE`, `WEIGHT_PASSIVE`,
`COORDINATOR_HEARTBEAT` are defined and documented as the API-budget model, but
**referenced nowhere** (grep: only `const.py`). The coordinator instead uses
**hardcoded** thresholds (`remaining < 40` / `< 100`, `coordinator.py:155,161`)
and **hardcoded** intervals (60/240/30/120 min, `:156-163`). Either wire the
constants in or delete them — right now the intent (weighted reserve budgeting)
and the implementation (two magic thresholds) disagree, which will mislead the
next reader.

---

## L1 (fixed) — manifest `name` has an unbalanced parenthesis
`manifest.json:3`

```json
"name": "Legrand/Bticino Smarther x8000 NOT Netatmo (Smart Adaptive API Calls",
```

Open `(` with no closing `)`. Cosmetic (shown in the integrations list). Also the
name is long/awkward; consider trimming.

---

## L2 (fixed) — f-string with no placeholder
`api.py:265`

```python
raise RateLimitError(f"Persistent Rate Limit (429) detected")
```

No interpolation — drop the `f`. (Ruff `F541`.) Harmless.

---

## L3 (fixed) — Stale/misleading comment: quota "to be added"
`number.py:294`

```python
# This attribute must be initialized in the Coordinator (to be added)
return self.coordinator.daily_api_quota
```

`daily_api_quota` **is** initialized (`coordinator.py:79`). The comment predates
the wiring and now reads as a bug that isn't there. Remove it.

---

## L4 (fixed) — `auth.py` 5xx path was not retried
`auth.py`

Both `exchange_code_for_tokens` and `refresh_access_token` retried only
`aiohttp.ClientError` / `TimeoutError` (network) with backoff; a **5xx** raised
`ValueError` immediately and was re-raised by the generic `except` — a transient
server error became a hard failure. Fixed: 4xx still fails fast (bad code /
invalid refresh token), but 5xx now `await asyncio.sleep(2 ** attempt); continue`
through the existing retry loop, giving up (with a clear message) only after
`MAX_RETRIES` is exhausted.

---

## Not defects (verified — do not re-flag)

- **No `OptionsFlow` in `config_flow.py`.** Intentional: the `number`/`switch`
  entities write tuning directly to `entry.options` *and* mutate coordinator
  memory live (`number.py:111`, `switch.py:136`), so no options dialog and no
  update-listener/reload is needed. Values survive restart because the
  coordinator re-reads `entry.options` in `__init__` (`:61-83`).
- **`async_update_entry` called without `await`** (`number.py:124`,
  `switch.py:141`). Correct — it returns `bool`, not a coroutine. The inline
  comments already document this.
- **`daily_api_quota` present** (see L3) — the *value* is fine; only the comment
  is stale.
- **`extra_state_attributes` returning `{}` unless DEBUG** — deliberate across all
  platforms; raw payloads are debug-only.
- **Config entities always `available`** — deliberate, so the user can retune
  while the API is rate-limited/down.

---

## Log-level audit (release)

Every `_LOGGER.<level>` call was reviewed for correct level and coherence
(DEBUG = per-cycle/dev detail · INFO = sparse milestones · WARNING = handled
anomaly · ERROR = terminal, user-actionable). Four incoherences fixed; all other
calls verified coherent and left as-is.

- **coordinator.py** — per-webhook-event `INFO` ("Webhook updated N entities")
  fired on *every* push → **DEBUG** (it's per-event chatter, the merge is already
  logged at DEBUG per device).
- **api.py** — "Persisted refreshed token to ConfigEntry storage" and "Token
  refreshed by another task" were `INFO` internal plumbing that repeats on every
  hourly refresh → **DEBUG**. The one meaningful refresh milestone ("Token
  refreshed and SAVED") stays `INFO`.
- **auth.py** — the non-200 handler logged `ERROR` up-front for *any* status,
  including a `5xx` that is then retried and recovers (leaving a spurious ERROR).
  Restructured in both `exchange_code_for_tokens` and `refresh_access_token`:
  `ERROR` now fires only on the terminal paths (non-retriable `4xx`, or `5xx`
  after retries are exhausted); the transient-retry path stays `WARNING`.

Documented for users in README ("Logging" — level table + how to enable DEBUG
via `logger:`). `iot_class` (`cloud_polling`) is stated and justified in the
README intro. `py_compile` passes on all touched modules.

## Release cleanup pass

A release-readiness sweep followed the fixes above. Result: `python3 -m py_compile`
passes on all modules and `pyflakes` reports **0 issues**.

- **Italian comments** — removed (auth.py had 3: shared-session/symmetric-check
  notes; a broken `"one o more"` docstring in config_flow.py was corrected).
- **Dead code** — removed unused imports (`climate.py` `timedelta`/`dt_util`,
  `webhook.py` `TYPE_CHECKING`→`BticinoCoordinator`) and an unused `except`
  binding (`config_flow.py` `as e`). Removed ~23 unused `const.py` constants
  (`SERVICE_SET_*`, `ATTR_*`, dead `CONF_*`, `AUTH_CALLBACK_*`,
  `AUTH_CHECK_ENDPOINT`). Kept `INTEGRATION_VERSION` / `MIN_REQUIRED_HA_VERSION`
  (release-workflow placeholders).
- **Historical / misleading comments** — stripped ~90 marker prefixes
  (`# UPDATED:/FIX:/IMPROVEMENT:/CRITICAL FIX:/BUGFIX:/OPTIMIZATION:/NEW:`) and
  deleted the pure fork/rebrand-history lines ("Renamed to distinguish from
  original integration", "Removed hardcoded 'bticino_' prefix", etc.). The
  remaining comments are present-tense and explain intent, not past changes.
- **Naming** — the boot log no longer carries the "Fix Boot Loop" history
  string. **"NOT Netatmo" is kept on purpose** in the addon name and the
  config-entry title: it tells the user this is the X8000 (non-Netatmo) variant.
  The only fix here is the balanced closing parenthesis (L1).

## Kept by design (not dead code)
- `api.get_subscriptions_c2c_notifications` / `delete_subscribe_c2c_notifications`
  are unused in normal operation and kept **on purpose** for optional manual
  cleanup only. See the decision below.

## Decision — C2C subscription is intentionally NOT torn down (closes M2/L)
Leaving the Legrand-side C2C subscription in place on removal is a **deliberate
design choice**, not a leak to fix:
- The webhook URL is stable (external URL + fixed webhook id), so a **reinstall
  reuses the existing subscription**: re-subscribe returns `409 Already Active`,
  handled as success (`__init__.py:96-97`). Deleting on removal would throw this
  free reuse away and force a recreate.
- An orphan is **harmless**: inbound webhooks **don't count against the API
  quota**, and once the HA webhook is gone the pushes just get a `404`.
- Wiring a delete into `async_remove_entry` would add an auth-dependent,
  hard-to-test teardown path (it needs a valid token at removal time) for a
  marginal benefit.

The only scenario where cleanup helps is a user who **changes their external URL**,
accumulating stale subscriptions. This is now handled *in place* by the
**reconfigure flow** (see below) rather than requiring a remove/re-add — the
GET/DELETE helpers finally have a real caller there. They are still never called
on unload/removal. Documented in README ("Webhook Subscriptions (Removal &
Reinstall)") and in a code comment above the API methods. **No `async_remove_entry`
added.**

## Feature — Reconfigure external URL (v1.1.0)

`entry.data` (credentials, tokens, device selection) is fixed at install and has
no OptionsFlow (tuning lives in the number/switch entities). The one setup
parameter worth editing post-install is **`external_url`** — the C2C webhook
target. If it changes and can't be updated, the Legrand push subscription keeps
pointing at the old URL and push silently stops; the only pre-1.1.0 remedy was a
full remove/re-add (losing history + re-OAuth + re-select devices).

Added `async_step_reconfigure` (`config_flow.py`): edits `external_url`,
best-effort deletes the old-URL C2C subscriptions
(`_async_cleanup_old_subscriptions`, non-fatal), then
`async_update_reload_and_abort` — the reload re-subscribes the new URL (409/200).
The `GET /subscription` schema was validated against the live Legrand cloud —
a flat array of `{plantId, subscriptionId, EndPointUrl}` — so the parser keys on
those exact field names (the earlier defensive multi-key guessing was removed).
Credentials-reauth and device re-selection were analysed and **deliberately
deferred** (separate, higher-complexity steps). Requires HA ≥ 2024.4
(`MIN_REQUIRED_HA_VERSION` and `hacs.json` bumped); manifest `version` → 1.1.0.

## Feature — Reconfigure credentials (v1.2.0)

`async_step_reconfigure` became a **menu** with two sub-flows: `reconfigure_url`
(the v1.1.0 logic, unchanged) and the new `reconfigure_credentials`.

Rationale: the Legrand `subscription_key`, `client_id` and `client_secret` all
belong to the same developer-portal registration, so in practice they rotate
together and a rotation invalidates the OAuth tokens. A single-field editor was
therefore rejected as useless; the coherent unit of work is "renew the whole
credential set + re-authenticate". `reconfigure_credentials` pre-fills the three
current secrets, then hands off to the existing manual OAuth step
(`async_step_get_authorize_code`) to mint fresh tokens.

**Safety design (the entry must never break, as this ships without a staging
test):** the sub-flow keeps all new state in flow-local `self.data` and records
`_reconfig_entry_id`. The running coordinator/API client is never touched. The
live `entry.data` is mutated exactly once — a final `async_update_reload_and_abort`
inside `get_authorize_code` — and only **after** the new credentials are proven
end-to-end (`check_api_endpoint_health` → `exchange_code_for_tokens` →
`get_plants()==200`). Any abort/failure leaves production running on the old
credentials. The merge preserves `external_url` and `selected_thermostats`, so
`entity_id`s and history survive; plant re-selection is skipped.

Naming coherence re-checked across the addon: `DOMAIN`/`WEBHOOK_ID`
(`btlegrand_x8000_*`), the display name ("Legrand/Bticino Smarther x8000 NOT
Netatmo") and the `BtLegrand` manufacturer/device naming are consistent
everywhere user-visible; the `Bticino*` prefixes are internal Python class names
only (left as-is — a rename would be untestable churn with no functional effect).
Added a `config.abort` translation block (en/it). Manifest `version` → 1.2.0.

## Feature — Reconfigure devices, add & remove (v1.3.0)

Third menu sub-flow: `reconfigure_devices`. `selected_thermostats` is frozen at
install and never re-discovered on reload (`get_topology` runs only in the config
flow), so a thermostat added/removed in the Legrand app is invisible to the
integration. This step re-scans the live topology using the **loaded**
`coordinator.api` (current tokens — no OAuth), pre-checks the currently-selected
ones (keyed on `topology_id`, so an app-side rename doesn't un-check them), and
rebuilds `selected_thermostats` preserving unchanged entries verbatim (keeps their
`webhook_id`, no churn).

Add is trivial and mostly automatic: new entities on reload, and a brand-new
plant is auto-subscribed by `async_setup_entry`. Removal is the asymmetric,
delicate case and was split into two halves by risk:

* **Cloud subscription (safe/reversible → automated):** a plant that drops out
  *entirely* has its per-plant C2C subscription deleted
  (`_async_delete_plant_subscriptions`; a plant still holding ≥1 thermostat is
  left alone). Re-selecting re-subscribes on the next reload, so this is
  reversible.
* **HA registry (irreversible → left to the user, HA-native):** de-selected
  devices are **not** force-removed from the registry (that would drop history
  with no way to test the removal path safely). They simply go "unavailable";
  `async_remove_config_entry_device` (`__init__.py`) then lets the user delete
  each one from the UI, and rejects deletion of a still-selected thermostat or
  the shared service device. This choice (over auto-cleanup) was made explicitly
  because the addon ships without a staging test.

Manifest `version` → 1.3.0.
