"""The Bticino X8000 integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
# Import the correct exception that blocks the restart loop
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.loader import async_get_integration

from .api import X8000Api
from .const import DOMAIN, WEBHOOK_ID
from .coordinator import X8000Coordinator
from .webhook import X8000WebhookHandler

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.CLIMATE, 
    Platform.SENSOR, 
    Platform.SELECT, 
    Platform.NUMBER, 
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
]

# Backoff schedule (seconds) for retrying C2C subscription when the Legrand
# cloud is transiently failing (e.g. HTTP 500). After the list is exhausted we
# give up (polling stays active; a reload re-arms the attempt).
C2C_RETRY_DELAYS = [60, 120, 300, 600, 600, 600]


async def _async_subscribe_c2c_plants(api, plant_ids, webhook_url):
    """Attempt the C2C subscription for each plant.

    Returns the set of plant_ids that still failed (exception or a status that
    is neither 200/201 nor 409-already-active).
    """
    failed = set()
    for plant_id in plant_ids:
        try:
            response = await api.set_subscribe_c2c_notifications(
                plant_id, {"EndPointUrl": webhook_url}
            )
            status = response.get("status_code")
            if status in (200, 201):
                _LOGGER.info("Successfully subscribed C2C for Plant %s", plant_id)
            elif status == 409:
                _LOGGER.info(
                    "C2C Subscription already active (409) for Plant %s. No action needed.",
                    plant_id,
                )
            else:
                _LOGGER.warning("Failed to subscribe C2C for Plant %s: %s", plant_id, response)
                failed.add(plant_id)
        except Exception as e:
            # Log error but don't stop the setup process
            _LOGGER.error("Error subscribing C2C for Plant %s: %s", plant_id, e)
            failed.add(plant_id)
    return failed


def _schedule_c2c_retry(hass, entry, api, coordinator, webhook_url, pending, attempt):
    """Schedule a delayed retry of the C2C subscription for the failed plants.

    Self-reschedules with backoff until no plant is left or the schedule is
    exhausted. While the coordinator is in Cool Down (rate-limited) the retry is
    deferred without consuming an attempt, to avoid feeding the ban counter.
    The pending timer is stored on the coordinator so unload can cancel it.
    """
    if attempt >= len(C2C_RETRY_DELAYS):
        _LOGGER.error(
            "Giving up C2C subscription for %s plant(s) after %s retries. Push "
            "notifications disabled (polling still active); reload the integration to retry.",
            len(pending),
            attempt,
        )
        return

    delay = C2C_RETRY_DELAYS[attempt]

    async def _retry(_now=None):
        coordinator.c2c_retry_unsub = None
        if getattr(coordinator, "in_cool_down", False):
            _LOGGER.warning(
                "C2C retry deferred: Rate Limit (Cool Down) active for %s plant(s).",
                len(pending),
            )
            _schedule_c2c_retry(hass, entry, api, coordinator, webhook_url, pending, attempt)
            return
        _LOGGER.info(
            "Retrying C2C subscription (attempt %s/%s) for %s plant(s)...",
            attempt + 1,
            len(C2C_RETRY_DELAYS),
            len(pending),
        )
        still_failed = await _async_subscribe_c2c_plants(api, pending, webhook_url)
        if still_failed:
            _schedule_c2c_retry(
                hass, entry, api, coordinator, webhook_url, still_failed, attempt + 1
            )
        else:
            _LOGGER.info("All C2C subscriptions registered successfully after retry.")

    coordinator.c2c_retry_unsub = async_call_later(hass, delay, _retry)
    _LOGGER.info(
        "Scheduled C2C subscription retry in %ss for %s plant(s).", delay, len(pending)
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bticino X8000 from a config entry."""
    
    integration = await async_get_integration(hass, DOMAIN)
    _LOGGER.info(
        "Setting up Legrand/Bticino Smarther integration (version %s)",
        integration.version,
    )

    # 1. Initialize API
    api = X8000Api(hass, dict(entry.data))

    # 2. Initialize Coordinator
    coordinator = X8000Coordinator(hass, api, entry)

    # 2b. Load persisted API usage stats BEFORE the first refresh.
    # Doing this synchronously (awaited) prevents a startup race where the
    # fire-and-forget load overwrote counter increments made by the first
    # update cycle (the load replaces self.usage_stats wholesale).
    await api.async_load_usage_data()

    # 3. First Refresh (Sequential) with Fault Tolerance
    # We must catch ConfigEntryNotReady.
    # The method async_config_entry_first_refresh() raises ConfigEntryNotReady 
    # if the update fails. If we don't catch it, HA will retry setup endlessly.
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady as ex:
        # We allow the setup to finish even if the API is down/banned.
        # This keeps the coordinator alive (with its 60 min timer) and prevents
        # Home Assistant from restarting the integration every minute.
        _LOGGER.warning(
            "Initial setup failed (Rate Limit active). "
            "Integration forced to load in 'Unavailable' state to maintain Cool Down timer. "
            "Error: %s", 
            ex
        )

    # 4. Store coordinator
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    # Holder for a pending C2C-subscription retry timer (see _schedule_c2c_retry).
    coordinator.c2c_retry_unsub = None

    # 5. Register Webhook Handler (Home Assistant Side)
    # This is local, so we can do it even if banned.
    webhook_handler = X8000WebhookHandler(hass, WEBHOOK_ID)
    await webhook_handler.async_register_webhook()

    # 6. Subscribe to C2C Notifications (Legrand Side)
    # Check if we are already banned (Cool Down Mode).
    # If the initial refresh failed with 429, these calls will definitely fail too.
    # We skip them to avoid increasing the ban counter on the server.
    if getattr(coordinator, "in_cool_down", False):
        _LOGGER.warning("Skipping C2C Subscription due to active Rate Limit (Cool Down Mode).")
    
    elif "selected_thermostats" in entry.data:
        plant_ids = set()
        for plant_data in entry.data["selected_thermostats"]:
            p_id = list(plant_data.keys())[0]
            plant_ids.add(p_id)
        
        base_url = entry.data.get("external_url", "").rstrip("/")
        # WEBHOOK_ID is now dynamic from const.py (f"{DOMAIN}_webhook")
        webhook_url = f"{base_url}/api/webhook/{WEBHOOK_ID}"

        _LOGGER.info("Registering C2C subscriptions for %s plants to URL: %s", len(plant_ids), webhook_url)

        # We attempt subscription even if the initial refresh failed.
        failed = await _async_subscribe_c2c_plants(api, plant_ids, webhook_url)
        if failed:
            # Transient cloud failure (e.g. HTTP 500): retry in the background
            # with backoff instead of leaving the plant unsubscribed until the
            # next manual reload.
            _schedule_c2c_retry(hass, entry, api, coordinator, webhook_url, failed, 0)

    # 7. Forward setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow deleting a device from the UI only if it is orphaned.

    After a device re-selection (reconfigure_devices) the de-selected
    thermostats are no longer recreated and show as "unavailable". This lets
    the user remove such a device with one click. Devices that are still
    selected - and the shared service device (identified by the entry id) -
    are never removable, so an accidental deletion of an active device is
    rejected.
    """
    selected_ids = {
        list(plant_data.values())[0].get("id")
        for plant_data in config_entry.data.get("selected_thermostats", [])
        if list(plant_data.values())[0].get("id")
    }
    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        # Shared service device (DOMAIN, entry_id): never removable.
        if identifier == config_entry.entry_id:
            return False
        # Active thermostat (DOMAIN, topology_id): still selected -> keep.
        if identifier in selected_ids:
            return False
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    webhook_handler = X8000WebhookHandler(hass, WEBHOOK_ID)
    await webhook_handler.async_remove_webhook()

    # Cancel any pending C2C-subscription retry timer.
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and getattr(coordinator, "c2c_retry_unsub", None):
        coordinator.c2c_retry_unsub()
        coordinator.c2c_retry_unsub = None

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok