"""Api with strict rate limiting, concurrency control and token persistence."""

import asyncio
import json
import logging
from enum import Enum
from typing import Any
from datetime import datetime, timedelta

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .auth import (
    TokenInvalidGrantError,
    TokenTransientError,
    refresh_access_token,
)
from .const import (
    DEFAULT_API_BASE_URL,
    PLANTS,
    THERMOSTAT_API_ENDPOINT,
    TOPOLOGY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Base delay between calls (starting point for exponential backoff)
API_DELAY_SECONDS = 2.0
MAX_CONCURRENT_REQUESTS = 1

# Granular timeouts
HTTP_TIMEOUT_TOTAL = 20
HTTP_TIMEOUT_CONNECT = 10

# Proactive token refresh: renew the access token this many seconds before it
# actually expires. Absorbs host-clock skew and in-flight latency, and — because
# the token TTL is ~1h — avoids the counted 401 a lazy (reactive) refresh pays
# on the first request after every expiry.
TOKEN_REFRESH_MARGIN = 120


def _coerce_datetime(value):
    """Return a tz-aware datetime from a datetime or ISO string, else None.

    The stored ``access_token_expires_on`` is a datetime on a fresh setup but an
    ISO string once reloaded from the config-entry storage; accept both.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return dt_util.parse_datetime(value)

# STORAGE CONSTANTS
# 
STORAGE_KEY = f"{DOMAIN}.api_usage"
STORAGE_VERSION = 1
SAVE_DELAY = 60  # Seconds to wait before writing to disk (Debounce)

# --- Custom Exceptions Definition ---
class X8000ApiError(Exception):
    """Base class for all API errors."""

class RateLimitError(X8000ApiError):
    """Raised when the API returns a 429 error."""

class AuthError(X8000ApiError):
    """Raised when authentication fails or token cannot be refreshed."""


class AuthBrokenError(AuthError):
    """Auth is permanently broken (dead refresh token): re-auth required.

    The coordinator maps this to ``ConfigEntryAuthFailed`` so Home Assistant
    prompts the user to re-authenticate instead of retrying forever.
    """


class AuthRetryableError(AuthError):
    """Auth failed transiently (auth-server blip): retry on the next cycle.

    The coordinator maps this to ``UpdateFailed`` and does NOT mark auth broken.
    """


class _RefreshResult(Enum):
    """Outcome of a token-refresh attempt, so the 401 handler can react.

    REFRESHED -> retry the request with the new token.
    TRANSIENT -> auth server hiccup; credentials still presumed valid, fail this
                 request only and let the next scheduled poll try again.
    BROKEN    -> credentials are dead; mark auth broken and require re-auth.
    """

    REFRESHED = "refreshed"
    TRANSIENT = "transient"
    BROKEN = "broken"


class X8000Api:
    """Legrand API class with Rate Limiting, Backoff, Shared Session and Persistence."""

    def __init__(self, hass: HomeAssistant, data: dict[str, Any]) -> None:
        """Init function."""
        self.hass = hass
        self.data = data
        self.header = {
            "Authorization": self.data.get("access_token"),
            "Ocp-Apim-Subscription-Key": self.data.get("subscription_key"),
            "Content-Type": "application/json",
        }
        self._token_refresh_lock = asyncio.Lock()
        self._api_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self.auth_broken = False

        # Proactive refresh state: when the current access token expires, so we
        # can renew it before a request would 401. May start as a datetime (fresh
        # setup) or an ISO string (reloaded from storage) -> coerce to datetime.
        self.token_expires_on = _coerce_datetime(
            self.data.get("access_token_expires_on")
        )
        # Diagnostic counter: proactive renewals that pre-empted a 401.
        self.proactive_refresh_count = 0
        
        # Diagnostic: Granular Stats for Telemetry
        # We now track a dictionary instead of a single integer.
        # "total": Global sum of calls.
        # "thermostat_id": Specific calls for that device.
        self.usage_stats = {"total": 0}
        
        # Tracks the date currently associated with stats to handle midnight reset
        self._current_tracking_date = dt_util.now().date() 

        self.api_success_count = 0
        self.api_rate_limit_count = 0
        self.api_auth_fail_count = 0
        self.api_other_fail_count = 0
        self.last_call_time = None
        
        # Persistence: Initialize Store. The stored stats are loaded by
        # __init__.py (await async_load_usage_data()) BEFORE the first refresh,
        # so the load cannot race with - and overwrite - counter increments made
        # by the first update cycle.
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

        # Use Home Assistant's shared session
        self._session = async_get_clientsession(hass)

    @property
    def call_count(self) -> int:
        """Backward compatibility property for total calls."""
        return self.usage_stats.get("total", 0)

    @property
    def skip_count(self) -> int:
        """Polls skipped by Smart Polling today (persisted, resets at midnight)."""
        return self.usage_stats.get("skips", 0)

    def record_skip(self) -> None:
        """Record a poll skipped by Smart Polling.

        Kept in the same store as ``call_count`` and reset at local midnight, so
        the live diagnostic sensor and the statistics-based daily chart agree.
        The previous in-memory counter reset to 0 on every reload/restart, which
        made ``statistics: change`` sum each post-reload climb and inflate the
        daily total (e.g. 382 charted vs 77 live after several reloads).
        """
        self._check_midnight_reset()
        self.usage_stats["skips"] = self.usage_stats.get("skips", 0) + 1
        self._save_usage_data()

    async def async_load_usage_data(self) -> None:
        """Public entry point: load persisted usage stats before first refresh."""
        await self._load_usage_data()

    async def _load_usage_data(self) -> None:
        """Load API usage data from disk and reset if new day."""
        try:
            data = await self._store.async_load()
            if data:
                stored_date_str = data.get("date")
                # Handle migration from old integer format to new dict format
                # Use None as default to detect missing key vs empty dict
                stored_stats = data.get("stats")
                if not isinstance(stored_stats, dict):
                    # Fallback for legacy data
                    stored_stats = {"total": data.get("count", 0)}

                today = dt_util.now().date()
                
                # Convert stored string back to date object for comparison
                try:
                    stored_date = datetime.strptime(stored_date_str, "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    stored_date = None

                if stored_date == today:
                    self.usage_stats = stored_stats
                    # Ensure 'total' key exists
                    if "total" not in self.usage_stats:
                        self.usage_stats["total"] = 0
                    self._current_tracking_date = today
                    _LOGGER.debug("Restored daily API stats: %s", self.usage_stats["total"])
                else:
                    self.usage_stats = {"total": 0}
                    self._current_tracking_date = today
                    _LOGGER.debug("New day detected (Stored: %s, Today: %s). Resetting API stats.", stored_date, today)
        except Exception as e:
            _LOGGER.warning("Failed to load API usage data: %s", e)

    def _save_usage_data(self) -> None:
        """Schedule a save of the API usage data (Debounced)."""
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    def _data_to_save(self) -> dict:
        """Return data to save to disk."""
        return {
            "date": self._current_tracking_date.isoformat(),
            "stats": self.usage_stats,
        }

    def _check_midnight_reset(self):
        """Check if date changed during runtime and reset counters."""
        today = dt_util.now().date()
        if today != self._current_tracking_date:
            _LOGGER.info(
                "Midnight crossover detected (Old: %s, New: %s). Resetting Daily Quota.", 
                self._current_tracking_date, today
            )
            self.usage_stats = {"total": 0}
            self._current_tracking_date = today
            # Trigger immediate save to update the date on disk
            self._save_usage_data()

    def _increment_usage_counter(self, url: str):
        """Parse URL to identify target device and increment specific counters."""
        # Always increment global total
        self.usage_stats["total"] = self.usage_stats.get("total", 0) + 1
        
        # Attempt to extract Thermostat ID from URL
        # Pattern: .../modules/parameter/id/value/{module_id}
        # or .../value/{module_id}/programlist
        try:
            if "/value/" in url:
                parts = url.split("/value/")
                if len(parts) > 1:
                    # The ID is the next segment after 'value/'
                    # Handle potential trailing paths like '/programlist'
                    remainder = parts[1]
                    device_id = remainder.split("/")[0]
                    
                    if device_id:
                        self.usage_stats[device_id] = self.usage_stats.get(device_id, 0) + 1
        except Exception:
            # Parsing errors should never block the request
            pass

    async def _async_request(
        self, method: str, url: str, payload: dict | None = None
    ) -> dict[str, Any]:
        """
        Wrapper for all API calls.
        Handles Rate Limiting, Session reuse, Retries, and Stats.
        """
        if self.auth_broken:
            _LOGGER.warning("Authentication previously broken. Skipping request to %s", url)
            self.api_auth_fail_count += 1
            raise AuthBrokenError("Authentication is broken")

        # ACQUIRE SEMAPHORE (Serialize requests)
        async with self._api_semaphore:
            # Minimal pacing to avoid bursting even before the first request
            await asyncio.sleep(0.5)

            # Renew a near-expired token before spending a counted request on a
            # guaranteed 401 (token TTL ~1h). The reactive 401 path below stays
            # as the safety net for early / unexpected expiries.
            await self._ensure_token_fresh()

            attempts = 0
            max_attempts = 3
            # 408 is the Legrand cloud's own gateway->module timeout: transient and
            # per-module, so it is retriable like a 5xx, but with a tighter cap
            # (a single retry) to avoid burning budget/latency on a genuinely slow
            # module. Each attempt still counts one call against the daily quota.
            timeout_max_attempts = 2
            current_delay = API_DELAY_SECONDS
            
            while attempts < max_attempts:
                attempts += 1
                
                # Runtime Midnight Check
                self._check_midnight_reset()

                # Count physical requests (including retries) for accurate Rate Limit tracking
                # Now using the detailed incrementer
                self._increment_usage_counter(url)
                
                self.last_call_time = dt_util.utcnow()
                
                # PERSISTENCE: Schedule save on every increment
                self._save_usage_data()
                
                try:
                    request_args = {
                        "headers": self.header,
                        "timeout": aiohttp.ClientTimeout(
                            total=HTTP_TIMEOUT_TOTAL, 
                            connect=HTTP_TIMEOUT_CONNECT
                        )
                    }
                    if payload:
                        request_args["json"] = payload

                    _LOGGER.debug(
                        "API Call #%s | Attempt %s/%s: %s %s",
                        self.usage_stats["total"], attempts, max_attempts, method, url
                    )

                    # Snapshot the token this request is being sent with, so that on
                    # a 401 we can tell whether another task refreshed it while we
                    # were waiting on the refresh lock (see CASE 2 below).
                    sent_token = self.header["Authorization"]

                    async with self._session.request(method, url, **request_args) as response:
                        status_code = response.status
                        content = await response.text()

                        # CASE 1: SUCCESS (200 OK, 201 Created, 409 Conflict)
                        if status_code in (200, 201, 409):
                            self.api_success_count += 1
                            try:
                                return {"status_code": status_code, "data": json.loads(content)}
                            except json.JSONDecodeError:
                                return {"status_code": status_code, "data": {}}

                        # CASE 2: TOKEN EXPIRED (401)
                        if status_code == 401:
                            if attempts < max_attempts:
                                _LOGGER.warning("401 Unauthorized. Acquiring lock to refresh token...")
                                
                                async with self._token_refresh_lock:
                                    # If the live token no longer matches the one we
                                    # sent, another task already refreshed it while we
                                    # waited on the lock -> just retry with the new one.
                                    if self.header["Authorization"] != sent_token:
                                        _LOGGER.debug("Token refreshed by another task. Retrying request.")
                                    else:
                                        result = await self._handle_token_refresh()
                                        if result is _RefreshResult.TRANSIENT:
                                            # Auth server blip: token/refresh_token
                                            # still presumed valid, so do NOT brick
                                            # the client. Fail just this request; the
                                            # next scheduled poll retries.
                                            self.api_auth_fail_count += 1
                                            raise AuthRetryableError("Token refresh temporarily unavailable")
                                        if result is _RefreshResult.BROKEN:
                                            _LOGGER.error("Token refresh failed. Marking auth as broken.")
                                            self.auth_broken = True
                                            self.api_auth_fail_count += 1
                                            raise AuthBrokenError("Token refresh failed")
                                        _LOGGER.info("Token refreshed and SAVED. Retrying request.")
                                continue
                            else:
                                _LOGGER.error("401 Loop detected. Stop retrying.")
                                self.api_auth_fail_count += 1
                                raise AuthBrokenError("Unauthorized - Retry limit reached")
                        
                        # CASE 3: RATE LIMIT (429) - FATAL IMMEDIATE STOP
                        if status_code == 429:
                            _LOGGER.error("429 Rate Limit Detected on attempt %s. ABORTING RETRIES.", attempts)
                            self.api_rate_limit_count += 1
                            raise RateLimitError("Persistent Rate Limit (429) detected")

                        # CASE 4: SERVER ERROR (5xx) or REQUEST TIMEOUT (408) - RETRIABLE
                        # 5xx retries up to max_attempts; 408 (cloud->module timeout)
                        # gets a single retry (timeout_max_attempts) to catch the blip
                        # in the SAME cycle instead of leaving the device unavailable
                        # until the next scheduled poll.
                        if status_code >= 500 or status_code == 408:
                            is_timeout = (status_code == 408)
                            retry_cap = timeout_max_attempts if is_timeout else max_attempts
                            if attempts < retry_cap:
                                _LOGGER.warning(
                                    "%s %s detected. Sleeping for %s seconds before retry...",
                                    "Request Timeout" if is_timeout else "Server Error",
                                    status_code, current_delay
                                )
                                await asyncio.sleep(current_delay)
                                current_delay *= 2
                                continue
                            else:
                                self.api_other_fail_count += 1
                                raise X8000ApiError(
                                    f"Persistent {'Timeout' if is_timeout else 'Server'} Error "
                                    f"{status_code} after {attempts} attempts"
                                )

                        # CASE 5: CLIENT ERRORS (4xx)
                        _LOGGER.error("HTTP Client Error %s: %s", status_code, content)
                        self.api_other_fail_count += 1
                        raise X8000ApiError(f"HTTP Client Error {status_code}: {content}")

                except aiohttp.ClientError as e:
                    _LOGGER.error("Network error during request to %s: %s", url, e)
                    if attempts < max_attempts:
                        _LOGGER.debug("Network error. Sleeping %s seconds...", current_delay)
                        await asyncio.sleep(current_delay)
                        current_delay *= 2
                    else:
                        self.api_other_fail_count += 1
                        raise X8000ApiError(f"Network error: {e}")
                except Exception as e:
                    if isinstance(e, X8000ApiError):
                        raise e
                    _LOGGER.exception("Unexpected error during request to %s", url)
                    self.api_other_fail_count += 1
                    raise e
            
            self.api_other_fail_count += 1
            raise X8000ApiError("Request failed - Unknown loop exit")

    async def _ensure_token_fresh(self) -> None:
        """Proactively refresh the access token if it is within the expiry margin.

        Runs before a request is sent so we don't spend a counted 401 on the
        first call after the ~1h token expiry. Uncounted (hits the auth endpoint,
        not the API). No-op when the expiry is unknown (legacy entry) — the
        reactive 401 path then handles it as before.
        """
        margin = timedelta(seconds=TOKEN_REFRESH_MARGIN)

        if self.token_expires_on is None:
            return
        if dt_util.utcnow() < self.token_expires_on - margin:
            return

        async with self._token_refresh_lock:
            # Re-check under the lock: another task may have refreshed while we
            # were waiting for it.
            if (
                self.token_expires_on is not None
                and dt_util.utcnow() < self.token_expires_on - margin
            ):
                return

            _LOGGER.debug(
                "Access token near expiry (%s); refreshing proactively.",
                self.token_expires_on,
            )
            result = await self._handle_token_refresh()
            if result is _RefreshResult.REFRESHED:
                self.proactive_refresh_count += 1
            elif result is _RefreshResult.BROKEN:
                self.auth_broken = True
                self.api_auth_fail_count += 1
                raise AuthBrokenError("Proactive token refresh failed permanently")
            # TRANSIENT: keep the current token; the request proceeds and, if the
            # token really is expired, the reactive 401 path retries next cycle.

    async def _handle_token_refresh(self) -> _RefreshResult:
        """Refresh the token and persist it, classifying any failure.

        A 4xx (dead refresh token) is permanent -> BROKEN. A transient auth-server
        outage (5xx / network) is retriable -> TRANSIENT, so a passing blip no
        longer bricks the client until the integration is reloaded.
        """
        try:
            # Pass self.hass to use the shared session in auth.py
            access_token, refresh_token, expires_on = await refresh_access_token(
                self.hass, self.data
            )

            self.data["access_token"] = access_token
            self.data["refresh_token"] = refresh_token
            self.data["access_token_expires_on"] = expires_on
            self.header["Authorization"] = access_token
            # Feed the proactive-refresh scheduler with the new expiry.
            self.token_expires_on = expires_on

            entries = self.hass.config_entries.async_entries(DOMAIN)
            for entry in entries:
                if entry.data.get("client_id") == self.data.get("client_id"):
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={
                            **entry.data,
                            "access_token": access_token,
                            "refresh_token": refresh_token,
                            "access_token_expires_on": expires_on,
                        }
                    )
                    _LOGGER.debug("Persisted refreshed token to ConfigEntry storage.")
                    break

            return _RefreshResult.REFRESHED
        except TokenTransientError as e:
            _LOGGER.warning(
                "Token refresh hit a transient auth-server error; will retry "
                "next cycle without re-auth: %s", e
            )
            return _RefreshResult.TRANSIENT
        except TokenInvalidGrantError as e:
            _LOGGER.error("Token refresh rejected (refresh token dead?): %s", e)
            return _RefreshResult.BROKEN
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Unexpected (e.g. corrupt config): safest to force re-auth rather
            # than loop forever on a bug.
            _LOGGER.error("Unexpected error refreshing token: %s", e)
            return _RefreshResult.BROKEN

    # --- Public Methods ---

    async def check_api_endpoint_health(self) -> bool:
        url = f"{DEFAULT_API_BASE_URL}{THERMOSTAT_API_ENDPOINT}{PLANTS}"
        try:
            response = await self._async_request("GET", url)
            return response["status_code"] == 200
        except Exception:
            return False

    async def get_plants(self) -> dict[str, Any]:
        url = f"{DEFAULT_API_BASE_URL}{THERMOSTAT_API_ENDPOINT}{PLANTS}"
        return await self._async_request("GET", url)

    async def get_topology(self, plant_id: str) -> dict[str, Any]:
        url = f"{DEFAULT_API_BASE_URL}{THERMOSTAT_API_ENDPOINT}{PLANTS}/{plant_id}{TOPOLOGY}"
        return await self._async_request("GET", url)

    async def get_chronothermostat_status(self, plant_id: str, module_id: str) -> dict[str, Any]:
        url = (
            f"{DEFAULT_API_BASE_URL}"
            f"{THERMOSTAT_API_ENDPOINT}/chronothermostat/thermoregulation/"
            f"addressLocation{PLANTS}/{plant_id}/modules/parameter/id/value/{module_id}"
        )
        return await self._async_request("GET", url)

    async def set_chronothermostat_status(
        self, plant_id: str, module_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        url = (
            f"{DEFAULT_API_BASE_URL}"
            f"{THERMOSTAT_API_ENDPOINT}/chronothermostat/thermoregulation/"
            f"addressLocation{PLANTS}/{plant_id}/modules/parameter/id/value/{module_id}"
        )
        return await self._async_request("POST", url, payload=data)

    async def get_chronothermostat_programlist(self, plant_id: str, module_id: str) -> dict[str, Any]:
        url = (
            f"{DEFAULT_API_BASE_URL}"
            f"{THERMOSTAT_API_ENDPOINT}/chronothermostat/thermoregulation/"
            f"addressLocation{PLANTS}/{plant_id}/modules/parameter/id/value/{module_id}/programlist"
        )
        return await self._async_request("GET", url)

    # Subscription GET/DELETE are intentionally NOT wired into unload/removal.
    # The C2C subscription is deliberately left on the Legrand side when the
    # integration is removed, so a later reinstall reuses it (re-subscribe returns
    # 409 "already active" -> handled as success in __init__). Orphans are
    # harmless: inbound webhooks don't count against the quota. These two helpers
    # exist only for an optional manual cleanup (e.g. if the external URL changes
    # across installs and old subscriptions accumulate). Do not auto-call them.
    async def get_subscriptions_c2c_notifications(self) -> dict[str, Any]:
        url = f"{DEFAULT_API_BASE_URL}{THERMOSTAT_API_ENDPOINT}/subscription"
        return await self._async_request("GET", url)

    async def set_subscribe_c2c_notifications(self, plant_id: str, data: dict[str, Any]) -> dict[str, Any]:
        url = f"{DEFAULT_API_BASE_URL}{THERMOSTAT_API_ENDPOINT}{PLANTS}/{plant_id}/subscription"
        return await self._async_request("POST", url, payload=data)

    # Manual cleanup only (see note above get_subscriptions_c2c_notifications).
    async def delete_subscribe_c2c_notifications(self, plant_id: str, subscription_id: str) -> dict[str, Any]:
        url = (
            f"{DEFAULT_API_BASE_URL}"
            f"{THERMOSTAT_API_ENDPOINT}"
            f"{PLANTS}/{plant_id}/subscription/{subscription_id}"
        )
        return await self._async_request("DELETE", url)