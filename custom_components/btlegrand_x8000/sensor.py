"""Sensor platform for Bticino X8000 using DataUpdateCoordinator."""

import logging
from functools import reduce
from typing import Any, Sequence

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfTemperature,
    UnitOfTime,
    EntityCategory,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import X8000Coordinator

_LOGGER = logging.getLogger(__name__)

# Standardized mode values.
MODE_MAP = {
    "automatic": "automatic",
    "manual": "manual",
    "boost": "boost",
    "off": "off",
    "protection": "off",  # Mapping protection/antifrost to off or custom
}

# Map for standardized load state values
LOAD_STATE_MAP = {
    "heating": "heating",
    "on": "heating",
    "active": "heating",
    "off": "off",
    "idle": "off",
    "inactive": "off",
    # Fallbacks for potential API variations
    "true": "heating",
    "false": "off",
}

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Bticino X8000 sensors."""
    coordinator: X8000Coordinator = hass.data[DOMAIN][config_entry.entry_id]

    # Debug log to indicate the setup start
    _LOGGER.debug("Setting up %s sensor platform", DOMAIN)

    # Use config data to iterate over selected thermostats.
    data = dict(config_entry.data)
    entities = []
    
    selected_thermostats = data.get("selected_thermostats", [])
    _LOGGER.debug("Found %d selected thermostats in configuration", len(selected_thermostats))

    for plant_data in selected_thermostats:
        plant_id = list(plant_data.keys())[0]
        thermo_data = list(plant_data.values())[0]

        topology_id = thermo_data.get("id")
        thermostat_name = thermo_data.get("name")
        programs = thermo_data.get("programs", [])
        
        _LOGGER.debug("Creating sensors for thermostat: %s (ID: %s)", thermostat_name, topology_id)

        # Create standard sensors
        sensors_classes = [
            X8000TemperatureSensor,
            X8000HumiditySensor,
            X8000TargetTemperatureSensor,
            X8000ModeSensor,
            X8000LoadStateSensor,
            X8000BoostTimeRemainingSensor,
        ]

        for sensor_class in sensors_classes:
            entities.append(
                sensor_class(
                    coordinator, plant_id, topology_id, thermostat_name
                )
            )

        # Create Program sensor (needs extra argument for program list)
        entities.append(
            X8000ProgramSensor(
                coordinator, plant_id, topology_id, thermostat_name, programs
            )
        )

        # Create per-thermostat API usage sensor for granular telemetry
        entities.append(
            X8000DeviceApiCountSensor(
                coordinator, topology_id, thermostat_name
            )
        )
    
    # Singleton diagnostic sensors: total API count and skipped-poll count.
    entities.append(X8000GlobalApiCountSensor(coordinator))
    entities.append(X8000SkippedPollsSensor(coordinator))
    # Smart-polling effectiveness diagnostics.
    entities.append(X8000PollingTierSensor(coordinator))
    entities.append(X8000ProjectedCallsSensor(coordinator))
    # Auth health: when the token expires / next proactive refresh is due.
    entities.append(X8000TokenExpirySensor(coordinator))

    _LOGGER.debug("Total entities to add: %d", len(entities))

    # Entities will start as 'Unavailable' until the first successful Coordinator update.
    async_add_entities(entities)


class X8000BaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for Bticino sensors."""

    def __init__(
        self,
        coordinator: X8000Coordinator,
        plant_id: str,
        topology_id: str,
        thermostat_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._plant_id = plant_id
        self._topology_id = topology_id
        self._thermostat_name = thermostat_name
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._topology_id)},
            name=self._thermostat_name,
            manufacturer="BtLegrand",
            model="X8000",
            via_device=(DOMAIN, self._plant_id),
        )

    @property
    def _thermostat_data(self) -> dict[str, Any]:
        """Retrieve data specific to this thermostat from coordinator."""
        # Safety check for None data during rate limiting.
        if self.coordinator.data is None:
            return {}

        # Coordinator data structure is flat: {topology_id: {data...}}
        return self.coordinator.data.get(self._topology_id, {}) or {}

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Entity %s (%s) received data update from coordinator",
                self.name,
                self.unique_id,
            )
        super()._handle_coordinator_update()

    def _get_nested_value(
        self, 
        path: Sequence[Any], 
        cast_type: type | None = None, 
        default: Any = None
    ) -> Any:
        """
        Extract nested value safely handling both int and str keys.
        Enhanced logic to handle mixed key types (e.g., list indices as strings).
        """
        def get_item(container, key):
            # Handle Dictionary
            if isinstance(container, dict):
                # Try explicit key first, then stringified key
                if key in container:
                    return container[key]
                return container.get(str(key))
            
            # Handle List/Tuple
            if isinstance(container, (list, tuple)):
                try:
                    # Convert key to int if it's a digit string
                    idx = int(key) if isinstance(key, str) and key.isdigit() else key
                    if isinstance(idx, int) and 0 <= idx < len(container):
                        return container[idx]
                except (ValueError, IndexError, TypeError):
                    return None
            return None

        try:
            value = reduce(get_item, path, self._thermostat_data)
            
            if value is not None and cast_type is not None:
                return cast_type(value)
            
            return value if value is not None else default
        except Exception:
            # Catch-all for unexpected structure changes to prevent crash
            return default


class X8000TemperatureSensor(X8000BaseSensor):
    """Sensor for current temperature."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    # Added state_class to enable Long Term Statistics (LTS)
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "Temperature"

    def __init__(self, coordinator, plant_id, topology_id, thermostat_name):
        super().__init__(coordinator, plant_id, topology_id, thermostat_name)
        self._attr_unique_id = f"{DOMAIN}_{topology_id}_temperature"

    @property
    def available(self) -> bool:
        """Explicitly mark unavailable if data is missing."""
        if not super().available:
            return False
        val = self._get_nested_value(["thermometer", "measures", 0, "value"], float)
        return val is not None

    @property
    def native_value(self) -> float | None:
        """Return the temperature."""
        return self._get_nested_value(["thermometer", "measures", 0, "value"], float)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Add raw data attributes for debugging (only if debug enabled)."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return {}
            
        return {
            "_raw_measure_data": self._get_nested_value(["thermometer", "measures", 0]),
            "_last_coordinator_success": self.coordinator.last_update_success,
        }


class X8000HumiditySensor(X8000BaseSensor):
    """Sensor for current humidity."""

    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = PERCENTAGE
    # Added state_class to enable Long Term Statistics (LTS)
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "Humidity"

    def __init__(self, coordinator, plant_id, topology_id, thermostat_name):
        super().__init__(coordinator, plant_id, topology_id, thermostat_name)
        self._attr_unique_id = f"{DOMAIN}_{topology_id}_humidity"

    @property
    def available(self) -> bool:
        """Mark unavailable if data is missing."""
        if not super().available:
            return False
        val = self._get_nested_value(["hygrometer", "measures", 0, "value"], float)
        return val is not None

    @property
    def native_value(self) -> float | None:
        """Return the humidity."""
        return self._get_nested_value(["hygrometer", "measures", 0, "value"], float)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Add raw data attributes for debugging (only if debug enabled)."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return {}
            
        return {
            "_raw_measure_data": self._get_nested_value(["hygrometer", "measures", 0]),
        }


class X8000TargetTemperatureSensor(X8000BaseSensor):
    """Sensor for target temperature."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    # Added state_class to enable Long Term Statistics (LTS)
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "Target Temperature"

    def __init__(self, coordinator, plant_id, topology_id, thermostat_name):
        super().__init__(coordinator, plant_id, topology_id, thermostat_name)
        self._attr_unique_id = f"{DOMAIN}_{topology_id}_target_temperature"

    @property
    def available(self) -> bool:
        """Mark unavailable if data is missing."""
        if not super().available:
            return False
        val = self._get_nested_value(["setPoint", "value"], float)
        return val is not None

    @property
    def native_value(self) -> float | None:
        """Return the target temperature."""
        return self._get_nested_value(["setPoint", "value"], float)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Add raw data attributes for debugging (only if debug enabled)."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return {}
            
        return {
            "_raw_setpoint_data": self._get_nested_value(["setPoint"]),
        }


class X8000ProgramSensor(X8000BaseSensor):
    """Sensor for current program."""

    _attr_icon = "mdi:calendar-clock"
    _attr_name = "Current Program"

    def __init__(
        self,
        coordinator,
        plant_id,
        topology_id,
        thermostat_name,
        programs,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, plant_id, topology_id, thermostat_name)
        self._attr_unique_id = f"{DOMAIN}_{topology_id}_current_program"
        self._programs = programs

    @property
    def available(self) -> bool:
        """
        Mark unavailable if programs list is missing or if the API data is invalid.
        Ensure we have both valid API data AND a valid configuration list.
        """
        if not super().available:
            return False
        
        # We need a program number from the API to display anything useful
        prog_number = self._get_nested_value(["programs", 0, "number"], int)
        
        # Also ensure we actually have programs configured
        return prog_number is not None and len(self._programs) > 0

    @property
    def native_value(self) -> str | None:
        """Return the name of the current program."""
        prog_number = self._get_nested_value(["programs", 0, "number"], int)

        if prog_number is not None:
            for prog in self._programs:
                if int(prog.get("number", -1)) == prog_number:
                    return prog.get("name")
            return f"Program {prog_number}"

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the raw payload as attributes when debug logging is enabled."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return {}
            
        return {
            "_raw_program_data": self._get_nested_value(["programs", 0]),
        }


class X8000ModeSensor(X8000BaseSensor):
    """Sensor for current mode."""

    _attr_icon = "mdi:thermostat"
    _attr_name = "Mode"

    def __init__(self, coordinator, plant_id, topology_id, thermostat_name):
        super().__init__(coordinator, plant_id, topology_id, thermostat_name)
        self._attr_unique_id = f"{DOMAIN}_{topology_id}_mode"

    @property
    def native_value(self) -> str | None:
        """Return the current mode."""
        # Use a map to ensure the state is always a valid/known string.
        raw_mode = self._thermostat_data.get("mode", "").lower()
        return MODE_MAP.get(raw_mode, STATE_UNKNOWN if not raw_mode else raw_mode)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Add raw data attributes for debugging consistency."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return {}
            
        return {
            "_raw_mode": self._thermostat_data.get("mode"),
        }


class X8000LoadStateSensor(X8000BaseSensor):
    """Sensor for current status (active/inactive)."""

    _attr_icon = "mdi:power"
    _attr_name = "Status"

    def __init__(self, coordinator, plant_id, topology_id, thermostat_name):
        super().__init__(coordinator, plant_id, topology_id, thermostat_name)
        self._attr_unique_id = f"{DOMAIN}_{topology_id}_status"

    @property
    def native_value(self) -> str | None:
        """Return the load state."""
        val = self._thermostat_data.get("loadState", "").lower()
        return LOAD_STATE_MAP.get(val, STATE_UNKNOWN if val else None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Add raw data attributes for debugging consistency."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return {}
            
        return {
            "_raw_load_state": self._thermostat_data.get("loadState"),
        }


class X8000BoostTimeRemainingSensor(X8000BaseSensor):
    """Sensor for boost time remaining."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_name = "Boost Time Remaining"

    def __init__(self, coordinator, plant_id, topology_id, thermostat_name):
        super().__init__(coordinator, plant_id, topology_id, thermostat_name)
        self._attr_unique_id = f"{DOMAIN}_{topology_id}_boost_time_remaining"

    @property
    def available(self) -> bool:
        # UX Improvement
        # Always available, so it shows 0 when inactive instead of "Unavailable"
        if not super().available:
            return False
        return True

    @property
    def native_value(self) -> int | None:
        """Return remaining boost time in minutes, or 0 if inactive."""
        data = self._thermostat_data
        
        # Check if we are in boost mode
        mode = data.get("mode", "").lower()
        
        # UX Improvement
        # If not in boost mode, return 0 immediately instead of None.
        if mode != "boost" or "activationTime" not in data:
            return 0

        try:
            activation_time = data["activationTime"]
            if "/" in activation_time:
                end_time_str = activation_time.split("/")[1]
            else:
                end_time_str = activation_time

            end_time = dt_util.parse_datetime(end_time_str)
            if end_time:
                now = dt_util.utcnow()
                end_time = dt_util.as_utc(end_time)
                remaining_seconds = (end_time - now).total_seconds()
                
                # Tolerance (+30s) allows for clock skew between the API server 
                # and Home Assistant, preventing premature zero/negative values.
                val = max(0, int((remaining_seconds + 30) / 60))
                
                return val
        except (ValueError, TypeError, IndexError):
            # In case of parsing error, fallback to 0 is safer than None here
            pass

        return 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Added raw attributes for debugging consistency."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return {}
            
        return {
            "_raw_activation_time": self._thermostat_data.get("activationTime"),
        }


# --- SPECIAL DIAGNOSTIC SENSOR ---

class X8000GlobalApiCountSensor(CoordinatorEntity, SensorEntity):
    """
    Diagnostic sensor for API usage.
    """

    _attr_icon = "mdi:api"
    _attr_name = "API Call Count"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "calls"
    _attr_has_entity_name = True
    _attr_force_update = True 

    def __init__(self, coordinator: X8000Coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_api_count_{coordinator.entry.entry_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Create a Virtual Device for the Cloud Service."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name="BtLegrand Service",
            manufacturer="BtLegrand",
            model="API Gateway",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Always available to show the count."""
        return True

    @property
    def native_value(self) -> int:
        """Return the total number of API calls made since boot."""
        # Direct read from API object singleton
        return self.coordinator.api.call_count

    @callback
    def _async_handle_any_update(self) -> None:
        """Called on EVERY coordinator update attempt (success or fail)."""
        _LOGGER.debug("API Count sensor caught ANY coordinator event")
        try:
            self.async_write_ha_state()
        except Exception as e:
            _LOGGER.error("Failed to update API Count sensor: %s", e)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Standard update handler."""
        self._async_handle_any_update()


class X8000SkippedPollsSensor(CoordinatorEntity, SensorEntity):
    """
    Diagnostic sensor for Smart Polling skipped calls.
    """

    _attr_icon = "mdi:filter-check"
    _attr_name = "Smart Polling Skips"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "skips"
    _attr_has_entity_name = True
    _attr_force_update = True

    def __init__(self, coordinator: X8000Coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_skipped_polls_{coordinator.entry.entry_id}"

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
        """Always available."""
        return True

    @property
    def native_value(self) -> int:
        """Return the total number of calls skipped by Smart Polling."""
        return getattr(self.coordinator, "skipped_count", 0)

    @callback
    def _async_handle_any_update(self) -> None:
        """Called on EVERY coordinator update attempt."""
        try:
            self.async_write_ha_state()
        except Exception:
            pass

    @callback
    def _handle_coordinator_update(self) -> None:
        """Standard update handler."""
        self._async_handle_any_update()


class X8000TokenExpirySensor(CoordinatorEntity, SensorEntity):
    """Diagnostic: absolute expiry of the current OAuth access token.

    Exposes the instant the proactive-refresh logic watches: the token is
    refreshed automatically once ``now`` is within ``TOKEN_REFRESH_MARGIN`` of
    this value. Surfacing it lets the user (and automations) see token health
    and confirm the auto-refresh is keeping the token ahead of expiry. Value is
    ``None`` (Unknown) for legacy entries that predate expiry tracking.
    """

    _attr_icon = "mdi:key-chain"
    _attr_name = "Token Expiry"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True

    def __init__(self, coordinator: X8000Coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_token_expiry_{coordinator.entry.entry_id}"

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
        """Always available; reports Unknown when the expiry is not tracked."""
        return True

    @property
    def native_value(self):
        """Return the tz-aware expiry instant, or None if unknown."""
        return getattr(self.coordinator.api, "token_expires_on", None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the proactive-refresh counter for at-a-glance health."""
        return {
            "proactive_refresh_count": getattr(
                self.coordinator.api, "proactive_refresh_count", 0
            ),
        }

    @callback
    def _async_handle_any_update(self) -> None:
        """Called on EVERY coordinator update attempt."""
        try:
            self.async_write_ha_state()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    @callback
    def _handle_coordinator_update(self) -> None:
        """Standard update handler."""
        self._async_handle_any_update()


class X8000PollingTierSensor(CoordinatorEntity, SensorEntity):
    """
    Diagnostic sensor exposing the current smart-polling throttling tier.

    Values: disabled / normal / economy / survival / frozen. Together with the
    enforced intervals (in the attributes) it makes the adaptive behaviour
    observable, so its real effectiveness can be measured rather than guessed.
    """

    _attr_icon = "mdi:speedometer"
    _attr_name = "Polling Tier"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["disabled", "normal", "economy", "survival", "frozen"]
    _attr_has_entity_name = True

    def __init__(self, coordinator: X8000Coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_polling_tier_{coordinator.entry.entry_id}"

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
        """Always available."""
        return True

    @property
    def native_value(self) -> str:
        """Return the current throttling tier."""
        return getattr(self.coordinator, "current_tier", "normal")

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the intervals actually enforced this cycle."""
        active = getattr(self.coordinator, "current_interval_active", None)
        passive = getattr(self.coordinator, "current_interval_passive", None)
        return {
            "active_interval_min": active.total_seconds() / 60 if active else None,
            "passive_interval_min": passive.total_seconds() / 60 if passive else None,
            "passive_multiplier": getattr(self.coordinator, "passive_multiplier", None),
            "remaining_calls": self.coordinator.daily_api_quota - self.coordinator.api.call_count,
        }

    @callback
    def _async_handle_any_update(self) -> None:
        try:
            self.async_write_ha_state()
        except Exception:
            pass

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_handle_any_update()


class X8000ProjectedCallsSensor(CoordinatorEntity, SensorEntity):
    """
    Diagnostic sensor projecting total API calls by midnight.

    Extrapolates today's usage over the elapsed fraction of the day. Compared to
    the Daily API Quota it answers the core question: are we on track to stay
    within budget? A value above the quota means the current pace is too fast.
    """

    _attr_icon = "mdi:chart-line"
    _attr_name = "Projected Daily Calls"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "calls"
    _attr_has_entity_name = True

    def __init__(self, coordinator: X8000Coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_projected_calls_{coordinator.entry.entry_id}"

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
        """Always available."""
        return True

    @property
    def native_value(self) -> int:
        """Return the projected end-of-day call total."""
        return self.coordinator.projected_daily_calls

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the budget and whether the projection exceeds it."""
        quota = self.coordinator.daily_api_quota
        return {
            "daily_quota": quota,
            "over_budget": self.coordinator.projected_daily_calls > quota,
        }

    @callback
    def _async_handle_any_update(self) -> None:
        try:
            self.async_write_ha_state()
        except Exception:
            pass

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_handle_any_update()


class X8000DeviceApiCountSensor(CoordinatorEntity, SensorEntity):
    """
    Diagnostic sensor for API usage specific to a thermostat.
    """
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "calls"
    _attr_icon = "mdi:chart-bar"

    def __init__(self, coordinator: X8000Coordinator, topology_id: str, thermostat_name: str):
        super().__init__(coordinator)
        self._topology_id = topology_id
        self._thermostat_name = thermostat_name
        # Unique ID: api_count_deviceid
        self._attr_unique_id = f"{DOMAIN}_api_count_{topology_id}"
        self._attr_name = "API Usage"

    @property
    def device_info(self) -> DeviceInfo:
        """Link to the thermostat device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._topology_id)},
            # We don't need to specify name/manufacturer here because they are
            # already defined by the main entities for this device ID.
        )

    @property
    def native_value(self) -> int:
        """Return the number of API calls for this specific device."""
        return self.coordinator.api.usage_stats.get(self._topology_id, 0)