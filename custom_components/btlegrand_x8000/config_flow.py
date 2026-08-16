"""Config Flow."""

# mypy: disable-error-code=no-any-return
# pylint: disable=W0223
import logging
import secrets
import asyncio  # Added for timeouts
from typing import Any
from urllib.parse import parse_qs, urlparse

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .api import BticinoX8000Api
from .auth import exchange_code_for_tokens
from .const import (
    AUTH_URL_ENDPOINT,
    CLIENT_ID,
    CLIENT_SECRET,
    DEFAULT_AUTH_BASE_URL,
    DEFAULT_REDIRECT_URI,
    DOMAIN,
    SUBSCRIPTION_KEY,
    WEBHOOK_ID,
)

_LOGGER = logging.getLogger(__name__)

# Timeout for API calls during config flow (seconds)
API_TIMEOUT = 20

class BticinoX8000ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Bticino ConfigFlow."""

    def __init__(self) -> None:
        """Init."""
        self.data: dict[str, Any] = {}
        # Map: "Display Name (Plant ID)" -> Thermostat Data Object
        self._selection_map: dict[str, Any] = {}
        self.bticino_api: BticinoX8000Api | None = None
        # Set only while re-authenticating an existing entry (credentials
        # reconfigure). When set, the OAuth step finalizes by updating this
        # entry in place instead of creating a new one.
        self._reconfig_entry_id: str | None = None

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Entry point for post-install reconfiguration.

        Presents a menu with two independent, self-contained sub-flows:

        * ``reconfigure_url`` - retarget the C2C webhook (external URL).
        * ``reconfigure_credentials`` - renew the Legrand credentials
          (client id/secret + subscription key) and re-run OAuth.

        Crucially, neither sub-flow mutates the running ConfigEntry until it
        has completed and validated successfully. Aborting the dialog or a
        failed authorization leaves the production entry exactly as it was.
        """
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=[
                "reconfigure_url",
                "reconfigure_credentials",
                "reconfigure_devices",
            ],
        )

    async def async_step_reconfigure_url(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Let the user change the Home Assistant external URL after install.

        The external URL is the C2C webhook target. When it changes, the
        Legrand-side subscription keeps pointing at the old URL and push
        updates stop. This step retargets the subscription without a full
        remove/re-add: it best-effort deletes the subscriptions bound to the
        old URL, persists the new URL, and reloads (setup re-subscribes it).
        """
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")
        current_url = entry.data.get("external_url", "")
        errors: dict[str, str] = {}

        if user_input is not None:
            new_url = user_input["external_url"].strip().rstrip("/")
            old_url = current_url.rstrip("/")

            if not new_url.startswith(("http://", "https://")):
                errors["external_url"] = "invalid_url"
            elif new_url == old_url:
                # Nothing to change.
                return self.async_abort(reason="reconfigure_successful")
            else:
                # Drop the old-URL subscriptions before overwriting the URL,
                # while the loaded API client (and old URL) are still available.
                await self._async_cleanup_old_subscriptions(entry, old_url)
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, "external_url": new_url},
                    reason="reconfigure_successful",
                )

        return self.async_show_form(
            step_id="reconfigure_url",
            data_schema=vol.Schema(
                {vol.Required("external_url", default=current_url): str}
            ),
            errors=errors,
            description_placeholders={"current_url": current_url or "-"},
        )

    async def async_step_reconfigure_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Renew the Legrand credentials, then re-run OAuth for fresh tokens.

        The Legrand subscription key, client id and client secret all belong
        to the same developer-portal registration, so in practice they rotate
        together and a rotation invalidates the existing OAuth tokens. This
        step therefore edits all three at once (old values pre-filled) and
        hands off to the same manual OAuth authorization used at first setup.

        Safety: on submit we only stash the new values in the *flow-local*
        ``self.data`` and record the entry id. The live ConfigEntry - and the
        running coordinator/API client - are left untouched. The entry is
        updated atomically (``async_update_reload_and_abort``) only after the
        new credentials are proven working by a successful token exchange and
        plants fetch inside ``async_step_get_authorize_code``. Any failure or
        an aborted dialog leaves production running on the old credentials.
        """
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")

        errors: dict[str, str] = {}
        if user_input is not None:
            for key, value in user_input.items():
                if isinstance(value, str) and not value.strip():
                    errors[key] = "value_empty"

            if not errors:
                # Flow-local only. Seed from the existing entry so external_url,
                # selected_thermostats, etc. are preserved through the OAuth
                # dance, then overlay the three renewed secrets.
                self._reconfig_entry_id = entry.entry_id
                self.data = {
                    **entry.data,
                    "client_id": user_input["client_id"],
                    "client_secret": user_input["client_secret"],
                    "subscription_key": user_input["subscription_key"],
                }
                return self.async_show_form(
                    step_id="get_authorize_code",
                    data_schema=vol.Schema(
                        {vol.Required("browser_url", default=""): str}
                    ),
                    description_placeholders={
                        "auth_url": self.get_authorization_url(self.data),
                    },
                )

        return self.async_show_form(
            step_id="reconfigure_credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "client_id", default=entry.data.get("client_id", "")
                    ): str,
                    vol.Required(
                        "client_secret", default=entry.data.get("client_secret", "")
                    ): str,
                    vol.Required(
                        "subscription_key",
                        default=entry.data.get("subscription_key", ""),
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Entry point for HA-triggered re-authentication.

        Home Assistant calls this when the coordinator raises
        ``ConfigEntryAuthFailed`` (permanent auth failure: the stored refresh
        token is dead). It presents the persistent "Reconfigure"/"Re-authenticate"
        notification and, on user action, routes to ``reauth_confirm``.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Renew the Legrand credentials and re-run OAuth for the failing entry.

        Mirrors ``reconfigure_credentials`` but is reached via HA's reauth
        machinery. The credentials are pre-filled from the failing entry (a dead
        refresh token often does not mean the client id/secret changed, so the
        user can just re-authorize), and the finalization in
        ``async_step_get_authorize_code`` updates the entry in place via
        ``_reconfig_entry_id`` and reloads - which clears the reauth state.
        """
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")

        errors: dict[str, str] = {}
        if user_input is not None:
            for key, value in user_input.items():
                if isinstance(value, str) and not value.strip():
                    errors[key] = "value_empty"

            if not errors:
                # Flow-local only until validated; the live entry is untouched.
                self._reconfig_entry_id = entry.entry_id
                self.data = {
                    **entry.data,
                    "client_id": user_input["client_id"],
                    "client_secret": user_input["client_secret"],
                    "subscription_key": user_input["subscription_key"],
                }
                return self.async_show_form(
                    step_id="get_authorize_code",
                    data_schema=vol.Schema(
                        {vol.Required("browser_url", default=""): str}
                    ),
                    description_placeholders={
                        "auth_url": self.get_authorization_url(self.data),
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "client_id", default=entry.data.get("client_id", "")
                    ): str,
                    vol.Required(
                        "client_secret", default=entry.data.get("client_secret", "")
                    ): str,
                    vol.Required(
                        "subscription_key",
                        default=entry.data.get("subscription_key", ""),
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Re-select which thermostats are exposed, adding and/or removing.

        Thermostats are frozen in ``entry.data["selected_thermostats"]`` at
        install, so a device added or removed in the Legrand app is not picked
        up on reload. This step re-discovers the live topology (reusing the
        already-loaded API client and its current tokens - no OAuth needed) and
        lets the user re-pick, with the currently-selected ones pre-checked.

        Add is automatic on reload (new entities; a new plant is auto-subscribed
        by setup). For removals we only touch what is safe without a test: the
        C2C subscription of any plant that drops out *entirely* is deleted
        (reversible - re-selecting re-subscribes on reload). The now-orphaned
        HA device/entities are left as "unavailable" and can be removed by the
        user from the UI (see ``async_remove_config_entry_device``); nothing is
        force-deleted from the registry here.
        """
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")

        coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        api = getattr(coordinator, "api", None)
        if api is None:
            # Integration not loaded -> no fresh API client to discover with.
            return self.async_abort(reason="unknown")

        if user_input is None:
            # Discover the live topology across every plant on the account.
            self.bticino_api = api
            self._selection_map = {}
            try:
                async with asyncio.timeout(API_TIMEOUT):
                    plants_data = await api.get_plants()
            except asyncio.TimeoutError:
                return self.async_abort(reason="unknown")

            if plants_data.get("status_code") != 200:
                _LOGGER.error("Reconfigure devices: get_plants failed: %s", plants_data)
                return self.async_abort(reason="unknown")

            plants_payload = plants_data.get("data", {})
            if isinstance(plants_payload, dict):
                plants_list = plants_payload.get("plants", [])
            elif isinstance(plants_payload, list):
                plants_list = plants_payload
            else:
                plants_list = []

            plant_ids = list({p.get("id") for p in plants_list if p.get("id")})
            for plant_id in plant_ids:
                try:
                    async with asyncio.timeout(API_TIMEOUT):
                        topologies = await api.get_topology(plant_id)
                    if topologies.get("status_code") != 200:
                        continue
                    topo_data = topologies.get("data", {})
                    modules = []
                    if "plant" in topo_data and isinstance(topo_data["plant"], dict):
                        modules = topo_data["plant"].get("modules", [])
                    elif "modules" in topo_data:
                        modules = topo_data.get("modules", [])
                    elif isinstance(topo_data, list):
                        modules = topo_data

                    for thermo in modules:
                        if not isinstance(thermo, dict) or "id" not in thermo:
                            continue
                        thermo_name = thermo.get("name", "Unknown")
                        try:
                            programs = await self.get_programs_from_api(
                                plant_id, thermo["id"]
                            )
                        except Exception:  # noqa: BLE001
                            programs = []
                        display_name = f"{thermo_name} (Plant {plant_id})"
                        self._selection_map[display_name] = {
                            "id": thermo["id"],
                            "name": thermo_name,
                            "programs": programs,
                            "plant_id": plant_id,
                        }
                except asyncio.TimeoutError:
                    _LOGGER.warning(
                        "Reconfigure devices: timeout on plant %s", plant_id
                    )
                    continue
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "Reconfigure devices: error on plant %s: %s", plant_id, err
                    )
                    continue

            if not self._selection_map:
                return self.async_abort(reason="No compatible thermostats found.")

            # Pre-check the currently-selected thermostats, keyed on topology id
            # (so a rename in the Legrand app does not un-check them).
            current_ids = {
                list(pd.values())[0].get("id")
                for pd in entry.data.get("selected_thermostats", [])
                if list(pd.values())[0].get("id")
            }
            default_selection = [
                dn for dn, d in self._selection_map.items() if d["id"] in current_ids
            ]

            return self.async_show_form(
                step_id="reconfigure_devices",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            "selected_thermostats", default=default_selection
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=list(self._selection_map.keys()),
                                multiple=True,
                                mode=selector.SelectSelectorMode.LIST,
                            )
                        ),
                    }
                ),
            )

        # Submit: rebuild the selection, preserving unchanged entries verbatim
        # (keeps their webhook_id and avoids churn) and minting new ones only
        # for newly-added thermostats.
        existing_by_id: dict[str, Any] = {}
        for plant_data in entry.data.get("selected_thermostats", []):
            thermo = list(plant_data.values())[0]
            if thermo.get("id"):
                existing_by_id[thermo["id"]] = plant_data

        final_selection: list[dict[str, Any]] = []
        for display_name in user_input["selected_thermostats"]:
            data = self._selection_map.get(display_name)
            if not data:
                continue
            tid = data["id"]
            if tid in existing_by_id:
                final_selection.append(existing_by_id[tid])
            else:
                plant_id = data["plant_id"]
                final_selection.append(
                    {
                        plant_id: {
                            "id": data["id"],
                            "name": data["name"],
                            "programs": data["programs"],
                        }
                    }
                )

        # Delete C2C subscriptions for plants that no longer have any selected
        # thermostat (best-effort, reversible). Plants that still keep at least
        # one thermostat are left untouched (the subscription is per-plant).
        old_plants = {
            list(pd.keys())[0] for pd in entry.data.get("selected_thermostats", [])
        }
        new_plants = {list(pd.keys())[0] for pd in final_selection}
        dropped_plants = old_plants - new_plants
        if dropped_plants:
            await self._async_delete_plant_subscriptions(
                api, dropped_plants, entry.data.get("external_url", "")
            )

        return self.async_update_reload_and_abort(
            entry,
            data={**entry.data, "selected_thermostats": final_selection},
            reason="reconfigure_successful",
        )

    async def _async_delete_plant_subscriptions(
        self, api: BticinoX8000Api, plant_ids: set[str], external_url: str
    ) -> None:
        """Best-effort delete of our C2C subscriptions for the given plants.

        Only removes subscriptions that both belong to one of ``plant_ids`` and
        point at *our* webhook URL. Non-fatal: a failure just leaves a harmless
        orphan the user can clean up manually.
        """
        webhook_url = f"{external_url.rstrip('/')}/api/webhook/{WEBHOOK_ID}"
        try:
            resp = await api.get_subscriptions_c2c_notifications()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Reconfigure devices: could not list C2C subscriptions (%s); "
                "dropped-plant subscriptions may need manual cleanup.", err,
            )
            return

        if resp.get("status_code") != 200:
            _LOGGER.warning(
                "Reconfigure devices: listing C2C subscriptions returned %s.",
                resp.get("status_code"),
            )
            return

        subs = resp.get("data")
        if not isinstance(subs, list):
            return

        for sub in subs:
            if not isinstance(sub, dict):
                continue
            plant_id = sub.get("plantId")
            if plant_id not in plant_ids:
                continue
            if (sub.get("EndPointUrl") or "").rstrip("/") != webhook_url:
                continue
            sub_id = sub.get("subscriptionId")
            if not sub_id:
                continue
            try:
                await api.delete_subscribe_c2c_notifications(plant_id, sub_id)
                _LOGGER.info(
                    "Reconfigure devices: removed C2C subscription for dropped "
                    "plant %s", plant_id,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Reconfigure devices: failed to delete C2C subscription %s: %s",
                    sub_id, err,
                )

    async def _async_cleanup_old_subscriptions(
        self, entry: config_entries.ConfigEntry, old_url: str
    ) -> None:
        """Best-effort removal of C2C subscriptions pointing at the old URL.

        Non-fatal: any failure only leaves an orphaned (harmless) subscription
        the user can clean up manually. Never blocks the reconfigure.
        """
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        api = getattr(coordinator, "api", None)
        if api is None:
            _LOGGER.warning(
                "Reconfigure: integration not loaded; old C2C subscription for "
                "%s left for manual cleanup.", old_url,
            )
            return

        old_webhook_url = f"{old_url}/api/webhook/{WEBHOOK_ID}"
        try:
            resp = await api.get_subscriptions_c2c_notifications()
        except Exception as err:  # noqa: BLE001 - best-effort, never fatal
            _LOGGER.warning(
                "Reconfigure: could not list C2C subscriptions (%s); old "
                "subscription for %s may need manual cleanup.", err, old_webhook_url,
            )
            return

        if resp.get("status_code") != 200:
            _LOGGER.warning(
                "Reconfigure: listing C2C subscriptions returned %s; old "
                "subscription for %s may need manual cleanup.",
                resp.get("status_code"), old_webhook_url,
            )
            return

        # GET /subscription returns a flat JSON array of objects shaped
        # {"plantId": ..., "subscriptionId": ..., "EndPointUrl": ...}
        # (schema confirmed against the live Legrand cloud).
        subs = resp.get("data")
        if not isinstance(subs, list):
            _LOGGER.warning(
                "Reconfigure: unexpected subscription-list shape (%s); old "
                "subscription for %s may need manual cleanup.",
                type(subs).__name__, old_webhook_url,
            )
            return

        deleted = 0
        for sub in subs:
            if not isinstance(sub, dict):
                continue
            if (sub.get("EndPointUrl") or "").rstrip("/") != old_webhook_url:
                continue
            sub_id = sub.get("subscriptionId")
            plant_id = sub.get("plantId")
            if not sub_id or not plant_id:
                continue
            try:
                await api.delete_subscribe_c2c_notifications(plant_id, sub_id)
                deleted += 1
            except Exception as err:  # noqa: BLE001 - best-effort, never fatal
                _LOGGER.warning(
                    "Reconfigure: failed to delete old C2C subscription %s: %s",
                    sub_id, err,
                )

        _LOGGER.info(
            "Reconfigure: removed %d old C2C subscription(s) for %s",
            deleted, old_webhook_url,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """User configuration."""
        errors = {}
        
        if self.hass.config.external_url is not None:
            external_url = self.hass.config.external_url
        else:
            external_url = (
                "My HA external url ex: "
                "https://example.com:8123 "
                "(specify the port if is not standard 443)"
            )

        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            # Basic validation for empty fields
            for key, value in user_input.items():
                if isinstance(value, str) and not value.strip():
                    errors[key] = "value_empty"
            
            if not errors:
                self.data = user_input
                authorization_url = self.get_authorization_url(user_input)
                
                # Move Auth URL to description_placeholders
                # instead of showing it as an error message.
                return self.async_show_form(
                    step_id="get_authorize_code",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                "browser_url",
                                description="Paste here the browser URL",
                                default="", 
                            ): str,
                        }
                    ),
                    description_placeholders={
                        "auth_url": authorization_url,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "client_id",
                        description="Client ID",
                        default=CLIENT_ID,
                    ): str,
                    vol.Required(
                        "client_secret",
                        description="Client Secret",
                        default=CLIENT_SECRET,
                    ): str,
                    vol.Required(
                        "subscription_key",
                        description="Subscription Key",
                        default=SUBSCRIPTION_KEY,
                    ): str,
                    vol.Required(
                        "external_url",
                        description="HA external_url",
                        default=external_url,
                    ): str,
                }
            ),
            errors=errors,
        )

    def get_authorization_url(self, user_input: dict[str, Any]) -> str:
        """Compose the auth url."""
        state = secrets.token_hex(16)
        return (
            f"{DEFAULT_AUTH_BASE_URL}{AUTH_URL_ENDPOINT}?"
            + f"client_id={user_input['client_id']}"
            + "&response_type=code"
            + f"&state={state}"
            + f"&redirect_uri={DEFAULT_REDIRECT_URI}"
        )

    async def async_step_get_authorize_code(  # pylint: disable=too-many-locals,too-many-nested-blocks
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Get authorization code."""
        errors = {}
        if user_input is not None:
            try:
                parsed_url = urlparse(user_input["browser_url"])
                query_params = parse_qs(parsed_url.query)

                if (
                    not query_params.get("code", [""])[0]
                    or not query_params.get("state", [""])[0]
                ):
                    raise ValueError("Invalid URL: Missing code or state.")

                self.data["code"] = query_params.get("code", [""])[0]

                # Exchange code for tokens
                (
                    access_token,
                    refresh_token,
                    access_token_expires_on,
                ) = await exchange_code_for_tokens(
                    self.hass,
                    self.data["client_id"],
                    self.data["client_secret"],
                    query_params.get("code", [""])[0],
                )

                self.data["access_token"] = access_token
                self.data["refresh_token"] = refresh_token
                self.data["access_token_expires_on"] = access_token_expires_on

                # Init API
                self.bticino_api = BticinoX8000Api(self.hass, self.data)

                if not await self.bticino_api.check_api_endpoint_health():
                    return self.async_abort(reason="Auth Failed! API endpoint unreachable.")

                # Fetch plants with Timeout & Robust Parsing
                try:
                    async with asyncio.timeout(API_TIMEOUT):
                        plants_data = await self.bticino_api.get_plants()
                except asyncio.TimeoutError:
                    return self.async_abort(reason="Timeout retrieving plants.")
                
                # Check status code
                if plants_data.get("status_code") != 200:
                    _LOGGER.error("Failed to get plants: %s", plants_data)
                    return self.async_abort(reason="API Error: Could not retrieve plants.")

                # Credentials reconfigure: at this point the new secrets have
                # been validated end-to-end (health check + token exchange +
                # plants fetch all succeeded). Update the existing entry in
                # place - preserving external_url and selected_thermostats -
                # and reload. This is the first and only mutation of the live
                # entry; plant re-selection is intentionally skipped.
                if self._reconfig_entry_id is not None:
                    entry = self.hass.config_entries.async_get_entry(
                        self._reconfig_entry_id
                    )
                    if entry is None:
                        return self.async_abort(reason="unknown")
                    _LOGGER.info(
                        "Reconfigure: credentials renewed and validated; "
                        "updating entry and reloading."
                    )
                    return self.async_update_reload_and_abort(
                        entry,
                        data={
                            **entry.data,
                            "client_id": self.data["client_id"],
                            "client_secret": self.data["client_secret"],
                            "subscription_key": self.data["subscription_key"],
                            "access_token": self.data["access_token"],
                            "refresh_token": self.data["refresh_token"],
                            "access_token_expires_on": self.data[
                                "access_token_expires_on"
                            ],
                        },
                        reason="reconfigure_successful",
                    )

                # Robust Parsing: Handle nested structure safely
                plants_payload = plants_data.get("data", {})
                # Some API versions wrap it in "plants", others return list directly
                plants_list = []
                if isinstance(plants_payload, dict):
                    plants_list = plants_payload.get("plants", [])
                elif isinstance(plants_payload, list):
                    plants_list = plants_payload
                
                if not isinstance(plants_list, list):
                    _LOGGER.error("Unexpected API structure for plants: %s", type(plants_payload))
                    plants_list = []

                # Unique list of IDs
                plant_ids = list({plant.get("id") for plant in plants_list if plant.get("id")})
                
                self._selection_map = {}

                for plant_id in plant_ids:
                    try:
                        # Timeout for Topology
                        async with asyncio.timeout(API_TIMEOUT):
                            topologies = await self.bticino_api.get_topology(plant_id)
                        
                        if topologies.get("status_code") != 200:
                            continue

                        # Robust Parsing for Topology
                        topo_data = topologies.get("data", {})
                        modules = []
                        
                        # Handle potential nesting: plant -> modules OR just modules
                        if "plant" in topo_data and isinstance(topo_data["plant"], dict):
                            modules = topo_data["plant"].get("modules", [])
                        elif "modules" in topo_data:
                            modules = topo_data.get("modules", [])
                        elif isinstance(topo_data, list):
                            modules = topo_data

                        for thermo in modules:
                            if not isinstance(thermo, dict) or "id" not in thermo:
                                continue
                                
                            thermo_name = thermo.get("name", "Unknown")
                            
                            # Fetch programs with safe timeout handling inside helper
                            try:
                                programs = await self.get_programs_from_api(plant_id, thermo["id"])
                            except Exception:
                                _LOGGER.warning("Could not fetch programs for %s", thermo_name)
                                programs = []

                            # Disambiguate names by adding Plant ID
                            display_name = f"{thermo_name} (Plant {plant_id})"
                            
                            self._selection_map[display_name] = {
                                "id": thermo["id"],
                                "name": thermo_name,
                                "programs": programs,
                                "plant_id": plant_id # Store plant_id for later retrieval
                            }
                            
                    except asyncio.TimeoutError:
                        _LOGGER.warning("Timeout fetching topology for plant %s", plant_id)
                        continue
                    except Exception as e:
                        _LOGGER.error("Error processing plant %s: %s", plant_id, e)
                        continue

                if not self._selection_map:
                    return self.async_abort(reason="No compatible thermostats found.")

                return self.async_show_form(
                    step_id="select_thermostats",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                "selected_thermostats",
                                description="Select Thermostats",
                                default=list(self._selection_map.keys()),
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=list(self._selection_map.keys()),
                                    multiple=True,
                                    mode=selector.SelectSelectorMode.LIST,
                                )
                            ),
                        }
                    ),
                )

            except ValueError as error:
                _LOGGER.error("Auth flow error: %s", error)
                errors["base"] = "auth_callback_error"
                # Fall through to show form again
            except Exception:
                _LOGGER.exception("Unexpected error in auth flow")
                return self.async_abort(reason="unknown_error")

        # Re-show form if user_input is None or if ValueError occurred
        # We need to re-generate the auth URL to be safe
        auth_url = self.get_authorization_url(self.data)
        return self.async_show_form(
            step_id="get_authorize_code",
            data_schema=vol.Schema(
                {
                    vol.Required("browser_url"): str,
                }
            ),
            description_placeholders={"auth_url": auth_url},
            errors=errors,
        )

    async def get_programs_from_api(
        self, plant_id: str, topology_id: str
    ) -> list[dict[str, Any]] | None:
        """Retrieve the program list with timeout protection."""
        if self.bticino_api is not None:
            try:
                # 5 second timeout for programs is sufficient, don't block flow too long
                async with asyncio.timeout(5):
                    programs = await self.bticino_api.get_chronothermostat_programlist(
                        plant_id, topology_id
                    )
            except asyncio.TimeoutError:
                return []

            if programs.get("status_code") != 200:
                return []

            # Robust Parsing for Programs
            data = programs.get("data", {})
            chrono_list = []
            
            if "chronothermostats" in data:
                 chrono_list = data["chronothermostats"]
            elif isinstance(data, list):
                 chrono_list = data # Rare case
            
            if chrono_list and isinstance(chrono_list, list) and len(chrono_list) > 0:
                item = chrono_list[0]
                if isinstance(item, dict):
                    programs_list = item.get("programs", [])
                    return [p for p in programs_list if p.get("number") != 0]
            
            return []
        return None

    async def async_step_select_thermostats(
        self, user_input: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Let the user select one or more thermostats to add."""
        
        # Reconstruct structure using the Selection Map
        # This properly maps the selected "Display Names" back to the full data objects
        # and groups them by plant_id as expected by the integration structure.
        
        # Structure target: [ { plant_id: { id: ..., name: ..., programs: ... } } ]
        final_selection = []

        for display_name in user_input["selected_thermostats"]:
            if display_name in self._selection_map:
                thermo_data = self._selection_map[display_name]
                plant_id = thermo_data.pop("plant_id") # Remove helper key

                final_selection.append({plant_id: thermo_data})

        return self.async_create_entry(
            title="Legrand/Bticino Smarther x8000 NOT Netatmo (Smart Adaptive API Calls)",
            data={
                "client_id": self.data["client_id"],
                "client_secret": self.data["client_secret"],
                "subscription_key": self.data["subscription_key"],
                "external_url": self.data["external_url"],
                "access_token": self.data["access_token"],
                "refresh_token": self.data["refresh_token"],
                "access_token_expires_on": self.data["access_token_expires_on"],
                "selected_thermostats": final_selection,
            },
        )