"""Binary sensor platform for Bticino X8000 (auth health)."""

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import X8000Coordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the auth-health binary sensor."""
    coordinator: X8000Coordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([X8000AuthProblemBinarySensor(coordinator)])


class X8000AuthProblemBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Diagnostic: ON when authentication is permanently broken (reauth needed).

    This mirrors the flag the coordinator acts on: once ``api.auth_broken`` is
    set (a dead refresh token that could not be renewed), every update cycle
    raises ``ConfigEntryAuthFailed`` and Home Assistant prompts for
    re-authentication. Exposing it as a PROBLEM binary sensor lets automations
    react (notify, etc.) instead of relying only on the HA reauth notification.
    A transient auth-server blip does NOT turn this on - only a permanent
    failure does.
    """

    _attr_icon = "mdi:shield-key"
    _attr_name = "Authentication Problem"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_has_entity_name = True

    def __init__(self, coordinator: X8000Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_auth_problem_{coordinator.entry.entry_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Link to the Cloud Service device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name="BtLegrand Service",
            manufacturer="BtLegrand",
            model="API Gateway",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Always available so the auth state is visible even while polling fails."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True when auth is permanently broken."""
        return bool(getattr(self.coordinator.api, "auth_broken", False))

    async def async_added_to_hass(self) -> None:
        """Register for ALL coordinator updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._async_handle_any_update)
        )

    @callback
    def _async_handle_any_update(self) -> None:
        """Called on EVERY coordinator update attempt (success or fail)."""
        try:
            self.async_write_ha_state()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    @callback
    def _handle_coordinator_update(self) -> None:
        """Standard update handler."""
        self._async_handle_any_update()
