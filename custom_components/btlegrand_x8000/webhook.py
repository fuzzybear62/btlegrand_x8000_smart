"""Webhook receiver for the Legrand/Bticino Smarther custom component.

Legrand's C2C service pushes full chronothermostat state to this endpoint.
Each event is validated, tagged with a short trace id, and handed to every
loaded coordinator — the payload carries no config_entry_id, so the coordinator
filters by topology_id on its side.
"""

import logging
import secrets

from aiohttp.web import Request, Response
from homeassistant.components.webhook import async_register as webhook_register
from homeassistant.components.webhook import async_unregister as webhook_unregister
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Top-level keys that mark a payload as a real Smarther push worth forwarding.
_EXPECTED_KEYS = ("chronothermostats", "data", "plant")


class SmartherWebhookHandler:
    """Registers, handles and tears down the Smarther C2C webhook."""

    def __init__(self, hass: HomeAssistant, webhook_id: str) -> None:
        """Store the HA instance and the webhook id to register."""
        self.hass = hass
        self.webhook_id: str = webhook_id

    async def handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: Request
    ) -> Response:
        """Validate an incoming push and fan it out to the coordinators."""
        trace = secrets.token_hex(4)

        try:
            data = await request.json()
        except ValueError as err:
            _LOGGER.error("[%s] webhook: invalid JSON body: %s", trace, err)
            return Response(text="Bad Request", status=400)

        # Guard against non-dict payloads before any key access. Always ack with
        # 200 so the Legrand server does not queue retries for junk it sent us.
        if not isinstance(data, dict):
            _LOGGER.warning(
                "[%s] webhook: payload is %s, not an object; ignoring",
                trace, type(data).__name__,
            )
            return Response(text="OK", status=200)

        if not any(key in data for key in _EXPECTED_KEYS):
            _LOGGER.debug("[%s] webhook: unrecognized structure, ignoring", trace)
            return Response(text="OK", status=200)

        _LOGGER.debug("[%s] webhook received: %s", trace, data)

        self._dispatch_to_coordinators(hass, data, trace)
        return Response(text="OK", status=200)

    def _dispatch_to_coordinators(
        self, hass: HomeAssistant, data: dict, trace: str
    ) -> None:
        """Push the payload to every loaded coordinator that can accept it."""
        entries = hass.data.get(DOMAIN)
        if not entries:
            _LOGGER.warning(
                "[%s] webhook received but %s is not loaded", trace, DOMAIN
            )
            return

        # Duck-type on the capability rather than the concrete class: this stays
        # correct even if hass.data[DOMAIN] ever holds non-coordinator values.
        delivered = 0
        for coordinator in entries.values():
            if hasattr(coordinator, "update_from_webhook"):
                coordinator.update_from_webhook(data)
                delivered += 1

        if not delivered:
            _LOGGER.warning(
                "[%s] webhook received but no coordinator accepted it", trace
            )

    async def async_register_webhook(self) -> None:
        """Register the endpoint with Home Assistant's webhook component."""
        webhook_register(
            self.hass,
            DOMAIN,
            "BtLegrand Smarther",
            self.webhook_id,
            self.handle_webhook,
            local_only=False,
        )

    async def async_remove_webhook(self) -> None:
        """Unregister the endpoint, tolerating an already-removed webhook."""
        _LOGGER.info("Tearing down Smarther webhook %s", self.webhook_id)
        try:
            webhook_unregister(self.hass, self.webhook_id)
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error(
                "[webhook] teardown of %s failed: %s",
                self.webhook_id, err, exc_info=True,
            )
