"""Api with strict rate limiting, concurrency control and token persistence."""

import asyncio
import json
import logging
from typing import Any
from datetime import datetime

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .auth import refresh_access_token
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

# STORAGE CONSTANTS
# 
STORAGE_KEY = f"{DOMAIN}.api_usage"
STORAGE_VERSION = 1
SAVE_DELAY = 60  # Seconds to wait before writing to disk (Debounce)

# --- Custom Exceptions Definition ---
class BticinoApiError(Exception):
    """Base class for all API errors."""

class RateLimitError(BticinoApiError):
    """Raised when the API returns a 429 error."""

class AuthError(BticinoApiError):
    """Raised when authentication fails or token cannot be refreshed."""

class BticinoX8000Api:
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
        
        # Persistence: Initialize Store and trigger load
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.hass.async_create_task(self._load_usage_data())

        # Use Home Assistant's shared session
        self._session = async_get_clientsession(hass)

    @property
    def call_count(self) -> int:
        """Backward compatibility property for total calls."""
        return self.usage_stats.get("total", 0)

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
            raise AuthError("Authentication is broken")

        # ACQUIRE SEMAPHORE (Serialize requests)
        async with self._api_semaphore:
            # Minimal pacing to avoid bursting even before the first request
            await asyncio.sleep(0.5)
            
            attempts = 0
            max_attempts = 3
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
                                        if await self._handle_token_refresh():
                                            _LOGGER.info("Token refreshed and SAVED. Retrying request.")
                                        else:
                                            _LOGGER.error("Token refresh FAILED. Marking auth as broken.")
                                            self.auth_broken = True
                                            self.api_auth_fail_count += 1
                                            raise AuthError("Token refresh failed")
                                continue
                            else:
                                _LOGGER.error("401 Loop detected. Stop retrying.")
                                self.api_auth_fail_count += 1
                                raise AuthError("Unauthorized - Retry limit reached")
                        
                        # CASE 3: RATE LIMIT (429) - FATAL IMMEDIATE STOP
                        if status_code == 429:
                            _LOGGER.error("429 Rate Limit Detected on attempt %s. ABORTING RETRIES.", attempts)
                            self.api_rate_limit_count += 1
                            raise RateLimitError("Persistent Rate Limit (429) detected")

                        # CASE 4: SERVER ERROR (5xx)
                        if status_code >= 500:
                            if attempts < max_attempts:
                                _LOGGER.warning(
                                    "Server Error %s detected. Sleeping for %s seconds before retry...", 
                                    status_code, current_delay
                                )
                                await asyncio.sleep(current_delay)
                                current_delay *= 2
                                continue
                            else:
                                self.api_other_fail_count += 1
                                raise BticinoApiError(f"Persistent Server Error {status_code} after {max_attempts} attempts")

                        # CASE 5: CLIENT ERRORS (4xx)
                        _LOGGER.error("HTTP Client Error %s: %s", status_code, content)
                        self.api_other_fail_count += 1
                        raise BticinoApiError(f"HTTP Client Error {status_code}: {content}")

                except aiohttp.ClientError as e:
                    _LOGGER.error("Network error during request to %s: %s", url, e)
                    if attempts < max_attempts:
                        _LOGGER.debug("Network error. Sleeping %s seconds...", current_delay)
                        await asyncio.sleep(current_delay)
                        current_delay *= 2
                    else:
                        self.api_other_fail_count += 1
                        raise BticinoApiError(f"Network error: {e}")
                except Exception as e:
                    if isinstance(e, BticinoApiError):
                        raise e
                    _LOGGER.exception("Unexpected error during request to %s", url)
                    self.api_other_fail_count += 1
                    raise e
            
            self.api_other_fail_count += 1
            raise BticinoApiError("Request failed - Unknown loop exit")

    async def _handle_token_refresh(self) -> bool:
        """Handle token refresh and PERSIST it to disk."""
        try:
            # Pass self.hass to use the shared session in auth.py
            access_token, refresh_token, _ = await refresh_access_token(self.hass, self.data)
            
            self.data["access_token"] = access_token
            self.data["refresh_token"] = refresh_token
            self.header["Authorization"] = access_token
            
            entries = self.hass.config_entries.async_entries(DOMAIN)
            for entry in entries:
                if entry.data.get("client_id") == self.data.get("client_id"):
                    self.hass.config_entries.async_update_entry(
                        entry, 
                        data={**entry.data, "access_token": access_token, "refresh_token": refresh_token}
                    )
                    _LOGGER.debug("Persisted refreshed token to ConfigEntry storage.")
                    break
            
            return True
        except Exception as e:
            _LOGGER.error("Fatal error refreshing token: %s", e)
            return False

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