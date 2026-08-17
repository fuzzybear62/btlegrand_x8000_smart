"""The Bticino X8000 integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
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

        for plant_id in plant_ids:
            try:
                payload = {
                    "EndPointUrl": webhook_url
                }
                
                # We attempt subscription even if the initial refresh failed.
                response = await api.set_subscribe_c2c_notifications(plant_id, payload)
                status = response.get("status_code")
                
                if status in (200, 201):
                    _LOGGER.info("Successfully subscribed C2C for Plant %s", plant_id)
                elif status == 409:
                    _LOGGER.info("C2C Subscription already active (409) for Plant %s. No action needed.", plant_id)
                else:
                    _LOGGER.warning(
                        "Failed to subscribe C2C for Plant %s: %s", 
                        plant_id, 
                        response
                    )
            except Exception as e:
                # Log error but don't stop the setup process
                _LOGGER.error("Error subscribing C2C for Plant %s: %s", plant_id, e)

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
    
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok