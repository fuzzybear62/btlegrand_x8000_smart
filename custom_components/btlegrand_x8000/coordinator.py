"""DataUpdateCoordinator for Bticino X8000."""

import logging
from datetime import timedelta
from typing import Any
from time import monotonic  # Added for debounce logic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed
# Import for direct notifications to ensure visibility during boot
from homeassistant.components import persistent_notification
from homeassistant.util import dt as dt_util  # Added for diagnostic timestamps

from .api import (
    BticinoX8000Api,
    RateLimitError,
    AuthError,
    AuthBrokenError,
    AuthRetryableError,
    BticinoApiError,
    _RefreshResult,
)
from .const import (
    DOMAIN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    CONF_COOL_DOWN,
    DEFAULT_COOL_DOWN,
    CONF_DEBOUNCE,
    DEFAULT_DEBOUNCE,
    CONF_NOTIFY_ERRORS,
    DEFAULT_NOTIFY_ERRORS,
    CONF_BTLG_DAILY_QUOTA,
    DEFAULT_BTLG_DAILY_QUOTA,
    CONF_SMART_POLLING,
    DEFAULT_SMART_POLLING,
    CONF_PASSIVE_MULTIPLIER,
    DEFAULT_PASSIVE_MULTIPLIER,
    # Adaptive API-budget throttling tiers (all derived from the two user knobs)
    ECONOMY_BUDGET_FRACTION,
    SURVIVAL_BUDGET_FRACTION,
    FROZEN_BUDGET_FRACTION,
    FROZEN_BUDGET_MIN,
    ECONOMY_INTERVAL_FACTOR,
    SURVIVAL_INTERVAL_FACTOR,
    TIER_OFF,
    TIER_NORMAL,
    TIER_ECONOMY,
    TIER_SURVIVAL,
    TIER_FROZEN,
    PROJECTION_MIN_DAY_FRACTION,
)

_LOGGER = logging.getLogger(__name__)

# Global ID for persistent notifications to avoid stacking multiple alerts.
# If a new error occurs, the existing notification is updated/overwritten.
NOTIFICATION_ID = "bticino_rate_limit_alert"

class BticinoCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Bticino data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: BticinoX8000Api,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        self.api = api
        self.entry = entry
        self.plant_map: dict[str, list[str]] = {}
        
        # Load tuning configuration from entry.options (falling back to defaults).

        # 1. Update Interval (Normal polling)
        # Read from Options (user settings), fallback to Default (15 min)
        update_minutes = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        self.normal_interval = timedelta(minutes=update_minutes)

        # 2. Cool Down Interval (Wait time after Ban)
        # Read from Options, fallback to Default (60 min)
        cool_down_minutes = entry.options.get(CONF_COOL_DOWN, DEFAULT_COOL_DOWN)
        self.cool_down_interval = timedelta(minutes=cool_down_minutes)

        # 3. Webhook Debounce (Traffic Control)
        # Read from Options, fallback to Default (1.0 sec)
        self.debounce_time = entry.options.get(CONF_DEBOUNCE, DEFAULT_DEBOUNCE)

        # 4. Error Notifications (User Experience)
        # Read from Options, fallback to Default (True/ON)
        self.notify_errors = entry.options.get(CONF_NOTIFY_ERRORS, DEFAULT_NOTIFY_ERRORS)

        # 5. Daily API Quota (Smart Scheduling)
        # Read from Options, fallback to Default (500 calls)
        # This enables the 'Daily API Quota' number entity to work.
        self.daily_api_quota = entry.options.get(CONF_BTLG_DAILY_QUOTA, DEFAULT_BTLG_DAILY_QUOTA)

        # 6. Smart Energy Saving (Optimized Polling)
        # Default: True (ON)
        self.smart_polling_enabled = entry.options.get(CONF_SMART_POLLING, DEFAULT_SMART_POLLING)

        # 7. Passive Zone Multiplier (Smart Scheduling)
        # OFF / antifrost zones are polled this many times slower than active ones.
        # Read from Options, fallback to Default (4x).
        self.passive_multiplier = entry.options.get(
            CONF_PASSIVE_MULTIPLIER, DEFAULT_PASSIVE_MULTIPLIER
        )

        # ---------------------------------------------------------------
        
        # Build a map of Plant IDs to Topology IDs (Thermostats) 
        # based on the user selection stored in the config entry.
        # This allows us to iterate specifically over the devices the user cares about.
        if "selected_thermostats" in entry.data:
            for plant_data in entry.data["selected_thermostats"]:
                plant_id = list(plant_data.keys())[0]
                thermo_data = list(plant_data.values())[0]
                topology_id = thermo_data.get("id")
                
                if plant_id not in self.plant_map:
                    self.plant_map[plant_id] = []
                self.plant_map[plant_id].append(topology_id)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=self.normal_interval,
        )

        # Debounce and Diagnostics initialization
        self._last_webhook_mono = 0.0
        self._last_webhook_time = None
        # Trailing-edge debounce: the latest payload seen during a burst and the
        # pending flush handle. Guarantees the FINAL state of a burst is applied
        # (leading-edge-only dropping left the coordinator on stale intermediate
        # state until the next scheduled poll).
        self._pending_webhook_data: dict[str, Any] | None = None
        self._webhook_flush_handle = None
        
        # Track when each specific thermostat was last updated (for Smart Polling)
        self._last_update_time: dict[str, Any] = {}
        
        # Diagnostic counter for Smart Polling efficiency lives in the API client
        # (persisted + reset at local midnight, exactly like call_count) and is
        # surfaced through the ``skipped_count`` property below.

        # Smart-polling diagnostics: expose the current throttling state and the
        # intervals actually being enforced, so effectiveness is measurable.
        self.current_tier: str = TIER_OFF if not self.smart_polling_enabled else TIER_NORMAL
        self.current_interval_active: timedelta = self.normal_interval
        self.current_interval_passive: timedelta = self.normal_interval * self.passive_multiplier

        # Explicit flag for Rate Limit state.
        # This allows other components (like number.py) to know if we are currently
        # in a "Ban" state without guessing based on interval times.
        self.in_cool_down = False

    async def _async_update_data(self) -> dict[str, Any]:
        """
        Fetch data from API sequentially.
        
        This method implements a 'Fail-Fast' logic: if a critical error (like Auth or Rate Limit)
        occurs on ONE device, the entire update cycle is aborted immediately.
        This prevents 'hammering' the API with further requests that would guaranteed fail.
        """
        data = {}
        now = dt_util.utcnow()  # Capture time at start of cycle
        
        # FAIL FAST CHECK 1:
        # Check if authentication is known to be broken from previous attempts.
        if self.api.auth_broken:
            _LOGGER.error("Auth is broken. Aborting update cycle.")
            # auth_broken is only set on a permanent failure (dead refresh token),
            # so surface it as a reauth request rather than a transient UpdateFailed.
            raise ConfigEntryAuthFailed("Authentication broken")

        # --- BUDGET SAFETY NET (Stateless Budget Awareness) ---
        # Derive dynamic intervals from the remaining daily quota. The tiers and
        # their thresholds are computed from the two user knobs (normal_interval
        # and daily_api_quota) so they scale coherently with any user setting.
        # Passive (OFF / antifrost) zones are always self.passive_multiplier x
        # slower than active ones, mirroring normal mode.
        #
        # 'frozen' is the end-of-day safety floor: when almost no budget is left
        # we stop scheduled polling entirely and rely on the (free) webhook push,
        # which prevents a self-inflicted rate-limit ban before midnight.

        # Default: use the user-configured base interval (Normal / smart off).
        current_interval_active = self.normal_interval
        current_interval_passive = self.normal_interval * self.passive_multiplier
        self.current_tier = TIER_OFF if not self.smart_polling_enabled else TIER_NORMAL
        frozen = False

        if self.smart_polling_enabled:
            remaining = self.daily_api_quota - self.api.call_count

            # Derived tier thresholds (calls) and the frozen floor.
            thr_economy = self.daily_api_quota * ECONOMY_BUDGET_FRACTION
            thr_survival = self.daily_api_quota * SURVIVAL_BUDGET_FRACTION
            thr_frozen = min(
                FROZEN_BUDGET_MIN, self.daily_api_quota * FROZEN_BUDGET_FRACTION
            )

            # Time-aware pacing veto (RELAXATION ONLY). If, at the current pace,
            # we are projected to finish the day within the quota, we are
            # demonstrably on track: do NOT throttle (economy/survival) even
            # though the absolute remaining budget looks low. This removes the
            # main failure mode of a purely-absolute model (needless throttling
            # late in the day, e.g. 90 calls left at 23:00). It can only relax,
            # never tighten, so a one-off burst (which raises the projection)
            # simply falls through to the absolute tiers. The frozen floor is
            # NOT subject to the veto: it is the absolute last-resort guard
            # against actually hitting zero.
            on_track = self.projected_daily_calls < self.daily_api_quota

            if remaining < thr_frozen:
                # FROZEN: skip all scheduled polls this cycle; webhooks keep data fresh.
                frozen = True
                self.current_tier = TIER_FROZEN
                if _LOGGER.isEnabledFor(logging.WARNING):
                    _LOGGER.warning(
                        "API BUDGET EXHAUSTED (%d left). Freezing scheduled polling "
                        "until midnight; relying on webhook push.",
                        remaining,
                    )
            elif on_track:
                # Pacing says we are within budget -> stay at Normal cadence.
                if (remaining < thr_economy) and _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "Budget low (%d left) but on track (projected %d <= quota %d); "
                        "pacing veto keeps Normal cadence.",
                        remaining,
                        self.projected_daily_calls,
                        self.daily_api_quota,
                    )
            elif remaining < thr_survival:
                current_interval_active = self.normal_interval * SURVIVAL_INTERVAL_FACTOR
                current_interval_passive = current_interval_active * self.passive_multiplier
                self.current_tier = TIER_SURVIVAL
                if _LOGGER.isEnabledFor(logging.WARNING):
                    _LOGGER.warning(
                        "CRITICAL API BUDGET (%d left). Enforcing survival intervals (%dm/%dm).",
                        remaining,
                        current_interval_active.total_seconds() / 60,
                        current_interval_passive.total_seconds() / 60,
                    )
            elif remaining < thr_economy:
                current_interval_active = self.normal_interval * ECONOMY_INTERVAL_FACTOR
                current_interval_passive = current_interval_active * self.passive_multiplier
                self.current_tier = TIER_ECONOMY
                if _LOGGER.isEnabledFor(logging.INFO):
                    _LOGGER.info(
                        "Low API Budget (%d left). Enforcing economy intervals (%dm/%dm).",
                        remaining,
                        current_interval_active.total_seconds() / 60,
                        current_interval_passive.total_seconds() / 60,
                    )

        # Publish the enforced intervals for the diagnostic sensors.
        self.current_interval_active = current_interval_active
        self.current_interval_passive = current_interval_passive

        # --------------------------------------------------------

        _LOGGER.debug("Starting sequential update for %s plants", len(self.plant_map))

        for plant_id, topology_ids in self.plant_map.items():
            for topology_id in topology_ids:
                
                # FAIL FAST CHECK 2:
                # Double check inside the loop in case auth broke during the previous iteration.
                if self.api.auth_broken:
                    raise ConfigEntryAuthFailed("Authentication broke during update")

                # --- SMART ENERGY SAVING LOGIC ---
                should_update = True

                # FROZEN tier: budget almost exhausted. Skip every scheduled poll
                # this cycle and lean on the free webhook push. We still require
                # prior data so entities keep their last known value.
                if frozen and self.data and topology_id in self.data:
                    should_update = False
                    self.api.record_skip()
                    _LOGGER.debug("Smart Polling: FROZEN, skipping scheduled poll for %s", topology_id)

                # Only apply logic if enabled AND we have previous data AND not in emergency cool down
                elif self.smart_polling_enabled and self.data and not self.in_cool_down:
                    last_data = self.data.get(topology_id)
                    last_update = self._last_update_time.get(topology_id)

                    if last_data and last_update:
                        # Check status: "Active" (Heating/Cooling) vs "Passive" (Off)
                        mode = last_data.get("mode", "").lower()
                        # Treat 'off' and 'protection' (antifrost) as passive
                        is_passive = (mode in ["off", "protection"])
                        
                        # Calculate specific interval for this device using DYNAMIC intervals
                        if is_passive:
                            required_interval = current_interval_passive
                        else:
                            required_interval = current_interval_active
                        
                        # Check if it's time to update
                        if now - last_update < required_interval:
                            should_update = False
                            # Increment diagnostic counter
                            self.api.record_skip()
                            _LOGGER.debug(
                                "Smart Polling: Skipping %s (Mode: %s). Next update in %.1f min.", 
                                topology_id, mode, 
                                (required_interval - (now - last_update)).total_seconds() / 60
                            )

                if not should_update:
                    # IMPORTANT: Even if we skip the API call, we must copy the OLD data
                    # to the new dataset, otherwise the entity will become 'Unavailable'.
                    if self.data and topology_id in self.data:
                        data[topology_id] = self.data[topology_id]
                    continue

                try:
                    # Request status from API
                    response = await self.api.get_chronothermostat_status(plant_id, topology_id)
                    
                    status_code = response.get("status_code")
                    
                    # 1. Handle Rate Limit returned as status code (Legacy/Fallback check)
                    # This catches cases where the API returns 429 without raising an exception.
                    if status_code == 429:
                        self._trigger_rate_limit_abort(plant_id, topology_id, "HTTP 429 Status Code returned")

                    if status_code == 200:
                        # SUCCESS: Check if we need to recover from Cool Down mode.
                        # Check the explicit flag instead of comparing intervals.
                        if self.in_cool_down:
                            _LOGGER.info("API request successful. Resetting update interval.")
                            
                            # Reset flag and restore user-configured normal interval
                            self.in_cool_down = False
                            self.update_interval = self.normal_interval
                            
                            # Dismiss the global persistent notification automatically if we recovered
                            persistent_notification.dismiss(self.hass, notification_id=NOTIFICATION_ID)

                        chrono_list = response.get("data", {}).get("chronothermostats", [])
                        if chrono_list:
                            # Save data using the flat topology_id key
                            data[topology_id] = chrono_list[0]
                            # Update timestamp for Smart Polling
                            self._last_update_time[topology_id] = now
                    else:
                        _LOGGER.warning(
                            "Update failed for %s. Status code: %s", 
                            topology_id, 
                            status_code
                        )
                
                # 2. Handle Custom Exceptions from api.py (Robust & Typed handling)
                except RateLimitError as err:
                    # Dedicated handler for our custom RateLimitError exception
                    self._trigger_rate_limit_abort(plant_id, topology_id, str(err))

                except AuthRetryableError as err:
                    # Transient auth-server blip. The stored token/refresh_token are
                    # presumed still valid, so do NOT set auth_broken and do NOT ask
                    # for reauth: fail just this cycle and let the next poll retry.
                    _LOGGER.warning(
                        "Transient auth error on %s: %s. Retrying next cycle.",
                        topology_id, err,
                    )
                    raise UpdateFailed(f"Auth temporarily unavailable: {err}")

                except AuthBrokenError as err:
                    # Permanent failure (dead refresh token). api.py already set
                    # auth_broken. Surface a reauth request so HA prompts the user
                    # to re-link the account instead of silently retrying forever.
                    _LOGGER.error("Authentication broken on %s: %s. Requesting reauth.", topology_id, err)
                    self.api.auth_broken = True
                    raise ConfigEntryAuthFailed(f"Auth broken: {err}")

                except AuthError as err:
                    # Defensive fallback for any untyped AuthError: treat as broken
                    # (safer to prompt reauth than to loop on a bad token).
                    _LOGGER.error("Authentication error on %s: %s. Requesting reauth.", topology_id, err)
                    self.api.auth_broken = True
                    raise ConfigEntryAuthFailed(f"Auth Error: {err}")

                except BticinoApiError as err:
                    _LOGGER.error("API Error on %s: %s", topology_id, err)
                    # For generic API errors (e.g. 500, timeout), we log but continue
                    # to the next device, as it might be a temporary single-device glitch.
                
                except UpdateFailed:
                    # Our own abort (inline 429 at the top of this try, or the
                    # AuthError handler) already fired the event / notification and
                    # updated the listeners. Propagate it directly so the generic
                    # net below does NOT re-run _trigger_rate_limit_abort and
                    # double-fire the {DOMAIN}_event bus event.
                    raise

                except Exception as err:
                    # 3. SAFETY NET: Catch generic exceptions that look like Rate Limits.
                    # This fixes the issue where the loop continued if the exception type wasn't perfect
                    # but the message clearly indicated a 429 error.
                    err_msg = str(err)
                    if "429" in err_msg or "Rate Limit" in err_msg:
                         self._trigger_rate_limit_abort(plant_id, topology_id, f"Generic Exception: {err_msg}")
                    
                    # Catch-all for truly unexpected crashes to prevent the loop from dying silently
                    _LOGGER.exception("Unexpected exception updating %s", topology_id)

                # PRESERVE LAST-KNOWN STATE: if this device produced no fresh data
                # this cycle (a per-device BticinoApiError or an unexpected
                # exception that logged-and-continued), fall back to the previous
                # value so the entity keeps its last reading instead of flipping to
                # 'Unavailable' over a transient single-device glitch. The skip and
                # success paths already populate data[topology_id]; this only fills
                # the gap left by the log-and-continue error branches.
                if topology_id not in data and self.data and topology_id in self.data:
                    data[topology_id] = self.data[topology_id]

        if not data:
            # Debug level to avoid noise during known outages
            _LOGGER.debug("Update cycle finished but no data was retrieved.")
            
        return data

    def _trigger_rate_limit_abort(self, plant_id: str, topology_id: str, message: str):
        """
        Helper to handle Rate Limit logic: Log, Fire Event, Notify, Set Interval, Raise Exception.
        
        This method is responsible for 'killing' the update loop efficiently and notifying the user.
        """
        _LOGGER.warning(
            "Rate Limit detected on %s: %s. Switching to Cool Down interval and aborting.", 
            topology_id, message
        )
        
        # Set explicit state flag
        self.in_cool_down = True
        
        # 1. Fire Event for User Notification (Home Assistant Bus)
        # This is useful for advanced automations.
        self.hass.bus.async_fire(
            f"{DOMAIN}_event",
            {
                "type": "rate_limit_exceeded",
                "plant_id": plant_id,
                "topology_id": topology_id,
                "message": message,
                "cooldown_minutes": self.cool_down_interval.total_seconds() / 60
            }
        )

        # 2. Create Persistent Notification (Directly visible in Dashboard)
        # Only show if enabled in configuration
        if self.notify_errors:
            # We use a GLOBAL ID (NOTIFICATION_ID) so we don't spam the user with multiple alerts.
            # This ensures the user sees the alert even if automations haven't loaded yet during boot.
            persistent_notification.async_create(
                self.hass,
                title="⛔ Bticino/Legrand API Paused",
                message=(
                    f"Rate Limit (429) detected.\n"
                    f"Integration paused for Cool Down period.\n\n"
                    f"Device: {topology_id}\n"
                    f"Error: {message}"
                ),
                notification_id=NOTIFICATION_ID
            )

        # 3. Set Cooldown Interval (Dynamic)
        # The next update will not happen for X minutes, letting the ban expire.
        self.update_interval = self.cool_down_interval
        
        # Update Diagnostic Sensors BEFORE raising exception.
        # This ensures the API Call Count in the UI reflects the failed attempt immediately,
        # instead of waiting for a successful cycle (which might be hours away).
        self.notify_listeners_only()

        # 4. RAISE EXCEPTION TO KILL THE LOOP IMMEDIATELY
        # Raising UpdateFailed tells Home Assistant that the update failed.
        # This stops the 'for' loop in _async_update_data and marks entities as Unavailable.
        raise UpdateFailed(f"Rate Limit Abort: {message}")

    async def async_force_token_refresh(self) -> None:
        """
        Public method to force a token refresh manually (e.g. via Button entity).
        This is useful if the token seems valid but the API is rejecting requests.
        """
        _LOGGER.info("Forcing manual token refresh requested by user.")
        # _handle_token_refresh returns a _RefreshResult enum, NOT a bool: every
        # enum member is truthy, so `if result:` would report success even on a
        # BROKEN/TRANSIENT outcome. Compare explicitly against REFRESHED.
        result = await self.api._handle_token_refresh()
        if result is _RefreshResult.REFRESHED:
            _LOGGER.info("Manual token refresh successful.")
            # A genuine refresh proves auth is healthy again; clear the flag.
            self.api.auth_broken = False
        elif result is _RefreshResult.BROKEN:
            _LOGGER.error("Manual token refresh failed permanently (reauth needed).")
            self.api.auth_broken = True
        else:  # TRANSIENT
            _LOGGER.warning("Manual token refresh temporarily unavailable; try again later.")

    def notify_listeners_only(self) -> None:
        """
        Notify listeners (sensors) that internal state/counters changed without fetching API.

        This is useful for updating diagnostic sensors (like API call counters)
        immediately after a write operation, without waiting for the next poll.
        """
        # Triggers the _handle_coordinator_update method on all registered entities
        self.async_update_listeners()

    def apply_optimistic(self, topology_id: str, patch: dict[str, Any]) -> None:
        """Merge an optimistic patch into the cached device state.

        After a successful command the cached cloud state we hold is still the
        PRE-command state. Without this, the immediate ``notify_listeners_only()``
        re-parse would read that stale ``setPoint``/``mode`` and overwrite the
        entity's optimistic value, so the UI snaps back to the old temperature
        until a real poll/webhook lands. Merging the command's own fields keeps
        the cache consistent until the next poll or the free webhook reconciles
        it against the cloud.
        """
        if not self.data or topology_id not in self.data:
            return
        self.data[topology_id].update(patch)

    @property
    def skipped_count(self) -> int:
        """Polls skipped by Smart Polling today.

        Delegates to the API client, which persists the count across reloads and
        resets it at local midnight (like call_count), so the live sensor matches
        the statistics-based daily chart.
        """
        return self.api.skip_count

    @property
    def projected_daily_calls(self) -> int:
        """
        Estimate the total API calls we are on track to make by midnight.

        This is the headline effectiveness metric: extrapolate the calls used so
        far over the fraction of the (local) day already elapsed. Compared against
        the daily quota it tells at a glance whether smart polling is keeping us
        under budget. call_count resets at local midnight (see api.py), so the day
        boundary here is intentionally local time too.
        """
        used = self.api.call_count
        now = dt_util.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_fraction = (now - start_of_day).total_seconds() / 86400

        # Too early in the day: extrapolation would be wildly noisy, so just
        # report the raw count instead of a meaningless projection.
        if day_fraction < PROJECTION_MIN_DAY_FRACTION:
            return used
        return round(used / day_fraction)

    def update_from_webhook(self, webhook_data: dict[str, Any]) -> None:
        """
        Update internal data from webhook payload with leading+trailing debounce.

        Bursts (e.g. a slider dragged, or a rapid sequence of C2C pushes) are
        coalesced: the FIRST payload is applied immediately (leading edge, keeps
        the UI responsive) and the LAST payload of the burst is applied once the
        quiet window elapses (trailing edge). A leading-edge-only filter dropped
        every payload after the first - including the final one carrying the
        settled state - leaving the coordinator on stale intermediate data until
        the next scheduled poll.
        """
        now = monotonic()
        if now - self._last_webhook_mono >= self.debounce_time:
            # Leading edge: quiet long enough, apply right away.
            self._last_webhook_mono = now
            self._cancel_webhook_flush()
            self._pending_webhook_data = None
            self._process_webhook(webhook_data)
            return

        # Inside the quiet window: remember the latest payload and (re)arm a
        # single trailing flush so the burst's final state still lands.
        self._pending_webhook_data = webhook_data
        _LOGGER.debug("Webhook debounced; trailing flush pending")
        if self._webhook_flush_handle is None:
            delay = max(0.0, self.debounce_time - (now - self._last_webhook_mono))
            self._webhook_flush_handle = self.hass.loop.call_later(
                delay, self._flush_pending_webhook
            )

    def _cancel_webhook_flush(self) -> None:
        """Cancel a pending trailing-edge flush, if any."""
        if self._webhook_flush_handle is not None:
            self._webhook_flush_handle.cancel()
            self._webhook_flush_handle = None

    @callback
    def _flush_pending_webhook(self) -> None:
        """Apply the last payload buffered during a debounce burst (trailing edge)."""
        self._webhook_flush_handle = None
        pending = self._pending_webhook_data
        self._pending_webhook_data = None
        if pending is None:
            return
        self._last_webhook_mono = monotonic()
        self._process_webhook(pending)

    def _process_webhook(self, webhook_data: dict[str, Any]) -> None:
        """
        Apply a single webhook payload to internal data defensively.

        Improvements:
        1. Hybrid Lookup: Flattens plant_map for O(1) checks.
        2. Diagnostics: Tracks last successful update time.
        """
        # 2. Validation
        if not isinstance(webhook_data, dict):
            _LOGGER.warning("Webhook payload is not a dictionary: %s", type(webhook_data))
            return

        # 3. Optimization: Hybrid Lookup
        # Flatten plant_map {plant_id: [ids]} into a simple set {id1, id2} for instant O(1) lookup.
        # This avoids nested loops for every webhook received.
        watched_topologies = set()
        for ids in self.plant_map.values():
            watched_topologies.update(ids)

        # 4. Extraction
        chronothermostats = self._extract_chronothermostats(webhook_data)
        if not chronothermostats:
             _LOGGER.debug("No valid chronothermostats list found in webhook")
             return

        # 5. Initialization
        # Ensure internal data store is ready before the loop
        if self.data is None:
            self.data = {}

        updated_count = 0
        
        # 6. Processing Loop
        for chrono in chronothermostats:
            if not isinstance(chrono, dict):
                continue

            # Refactored: Use helper to extract ID cleanly
            topology_id = self._get_topology_id(chrono)

            if not topology_id:
                _LOGGER.debug("Could not extract valid topology_id from item")
                continue

            # Filter: Only update if this device is in our configuration
            if topology_id not in watched_topologies:
                _LOGGER.debug("Ignoring webhook for unmonitored topology_id: %s", topology_id)
                continue

            # Update Data
            self.data[topology_id] = chrono
            updated_count += 1
            
            # CRITICAL: Reset the Smart Polling timer on Webhook event
            # This ensures that if the user turns ON a thermostat via App/Physical,
            # we immediately treat it as "Fresh" and subsequent logic will check its new state.
            self._last_update_time[topology_id] = dt_util.utcnow()
            
            _LOGGER.debug("Updated via webhook -> %s", topology_id)

        # 7. Finalize
        if updated_count > 0:
            # Update diagnostic timestamp
            self._last_webhook_time = dt_util.utcnow()
            _LOGGER.debug("Webhook updated %d entities", updated_count)
            # Notify listeners
            self.async_set_updated_data(self.data)
        else:
            _LOGGER.debug("Webhook received but no relevant updates applied")

    def _get_topology_id(self, chrono: dict) -> str | None:
        """Helper to extract topology_id from a chrono object safely."""
        # Standard path: sender -> plant -> module -> id
        path = chrono.get("sender", {}).get("plant", {}).get("module", {}).get("id")
        if path and isinstance(path, str):
            return path
            
        # Legacy/Fallback path: receiver -> oid
        oid = chrono.get("receiver", {}).get("oid")
        return oid if isinstance(oid, str) else None

    def _extract_chronothermostats(self, payload: Any) -> list[dict]:
        """
        Defensive helper to extract the chronothermostats list.
        It handles different nesting levels often seen in Legrand APIs.
        """
        if not isinstance(payload, dict):
            return []

        # Scenario A: Standard structure { "data": { "chronothermostats": [...] } }
        data = payload.get("data", {})
        if "chronothermostats" in data:
            ch = data["chronothermostats"]
            return ch if isinstance(ch, list) else [ch] if isinstance(ch, dict) else []

        # Scenario B: Direct structure { "chronothermostats": [...] }
        if "chronothermostats" in payload:
            ch = payload["chronothermostats"]
            return ch if isinstance(ch, list) else [ch] if isinstance(ch, dict) else []

        return []