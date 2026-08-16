"""OAuth2 token exchange/refresh for the Legrand/Bticino Smarther custom component.

Both public helpers share a single transport (`_request_token`) and a single
expiry parser (`_parse_expiry`), so retry/backoff behaviour and token-lifetime
handling are defined in exactly one place.
"""

import asyncio
import logging
import time
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import AUTH_REQ_ENDPOINT, DEFAULT_AUTH_BASE_URL

_LOGGER = logging.getLogger(__name__)

# Explicit timeout so an unresponsive auth server can't hang the config flow
# or a coordinator refresh.
AUTH_TIMEOUT = aiohttp.ClientTimeout(total=15)
# Retries apply only to transient failures (network errors, 5xx). A 4xx is the
# caller's fault (bad code / stale credentials) and is never retried.
MAX_RETRIES = 2
# Fallback lifetime when the server returns neither expires_on nor expires_in.
DEFAULT_TOKEN_TTL = 3600


class TokenRequestError(ValueError):
    """Base for token-endpoint failures.

    Subclasses ``ValueError`` so existing callers that catch ``ValueError``
    (e.g. the config flow) keep working unchanged.
    """


class TokenInvalidGrantError(TokenRequestError):
    """The auth server rejected the grant (4xx): bad code / stale refresh token.

    Permanent — retrying with the same credentials will keep failing, so the
    caller should require re-authentication rather than retry.
    """


class TokenTransientError(TokenRequestError):
    """The auth server was temporarily unavailable (5xx / network / bad body).

    Retriable — the stored credentials are presumed still valid, so the caller
    should try again on the next cycle instead of forcing re-authentication.
    """


async def _request_token(
    hass: HomeAssistant, payload: dict[str, str], ctx: str
) -> dict[str, Any]:
    """POST ``payload`` to the token endpoint and return the decoded token data.

    Retries network errors and 5xx with exponential backoff up to ``MAX_RETRIES``.
    Raises ``ValueError`` for every unusable outcome (4xx, exhausted retries,
    unreachable server, unparseable body); those are expected errors and are
    logged as such — the caller does not need a broad ``except`` around this.
    """
    token_url = f"{DEFAULT_AUTH_BASE_URL}{AUTH_REQ_ENDPOINT}"
    session = async_get_clientsession(hass)
    _LOGGER.debug("%s: requesting token from %s", ctx, token_url)

    for attempt in range(MAX_RETRIES + 1):
        try:
            async with session.post(
                token_url, data=payload, timeout=AUTH_TIMEOUT
            ) as response:
                status = response.status
                _LOGGER.debug("%s: auth server responded %s", ctx, status)

                if status == 200:
                    try:
                        return await response.json()
                    except (aiohttp.ClientError, ValueError) as err:
                        body = await response.text()
                        # A 200 with an unparseable body is more likely a
                        # transient proxy/error page than a dead grant: treat it
                        # as retriable so a blip doesn't force re-authentication.
                        _LOGGER.error(
                            "%s: could not decode token JSON (%s). Body: %s",
                            ctx, err, body,
                        )
                        raise TokenTransientError(
                            f"Invalid JSON from auth server: {err}"
                        ) from err

                body = await response.text()

                # 4xx: bad code / invalid refresh token — permanent, not retriable.
                if 400 <= status < 500:
                    _LOGGER.error(
                        "%s: auth server rejected the request (%s). Body: %s",
                        ctx, status, body,
                    )
                    raise TokenInvalidGrantError(
                        f"Auth server rejected request: {status}"
                    )

                # 5xx / anything else: transient, back off and retry.
                if attempt < MAX_RETRIES:
                    delay = 2 ** attempt
                    _LOGGER.warning(
                        "%s: transient auth error %s, retrying in %ss",
                        ctx, status, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                _LOGGER.error(
                    "%s: auth server still failing (%s) after %d retries. Body: %s",
                    ctx, status, MAX_RETRIES, body,
                )
                raise TokenTransientError(
                    f"Auth server error {status} after {MAX_RETRIES} retries"
                )

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            if attempt >= MAX_RETRIES:
                _LOGGER.error(
                    "%s: auth server unreachable after %d retries: %s",
                    ctx, MAX_RETRIES, err,
                )
                raise TokenTransientError(
                    f"Auth server unreachable: {err}"
                ) from err
            delay = 2 ** attempt
            _LOGGER.warning(
                "%s: network error contacting auth server (%s), retrying in %ss",
                ctx, err, delay,
            )
            await asyncio.sleep(delay)

    # The loop always returns, raises, or continues; this guards against a
    # future edit accidentally falling through.
    raise TokenTransientError(f"{ctx}: token request exhausted without a result")


def _parse_expiry(token_data: dict[str, Any], ctx: str):
    """Resolve the absolute expiry instant from the token response.

    Honours ``expires_on`` (absolute epoch) first, then ``expires_in`` (relative
    seconds), falling back to ``DEFAULT_TOKEN_TTL`` when the server sends neither.
    """
    expires_on = token_data.get("expires_on")
    expires_in = token_data.get("expires_in")

    if expires_on is not None:
        expires_ts = int(expires_on)
    elif expires_in is not None:
        expires_ts = int(time.time()) + int(expires_in)
    else:
        _LOGGER.warning(
            "%s: response had no expires_on/expires_in; assuming %ss",
            ctx, DEFAULT_TOKEN_TTL,
        )
        expires_ts = int(time.time()) + DEFAULT_TOKEN_TTL

    return dt_util.utc_from_timestamp(expires_ts)


async def exchange_code_for_tokens(
    hass: HomeAssistant, client_id: str, client_secret: str, code: str
) -> tuple[str, Any, Any]:
    """Exchange an authorization code for an (access, refresh, expiry) triple."""
    payload = {
        "code": code,
        "grant_type": "authorization_code",
        "client_secret": client_secret,
        "client_id": client_id,
    }
    token_data = await _request_token(hass, payload, "exchange_code_for_tokens")

    access_token = token_data.get("access_token")
    if not access_token:
        _LOGGER.error(
            "exchange_code_for_tokens: no access_token in response: %s", token_data
        )
        raise TokenTransientError("Missing access_token in token exchange response")

    expires_on = _parse_expiry(token_data, "exchange_code_for_tokens")
    return "Bearer " + access_token, token_data.get("refresh_token"), expires_on


async def refresh_access_token(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[str, Any, Any]:
    """Refresh an access token, returning an (access, refresh, expiry) triple."""
    payload = {
        "refresh_token": data["refresh_token"],
        "grant_type": "refresh_token",
        "client_secret": data["client_secret"],
        "client_id": data["client_id"],
    }
    token_data = await _request_token(hass, payload, "refresh_access_token")

    access_token = token_data.get("access_token")
    if not access_token:
        _LOGGER.error(
            "refresh_access_token: no access_token in response: %s", token_data
        )
        raise TokenTransientError("Missing access_token in refresh response")

    # OAuth2 servers may omit a new refresh token on refresh; keep the old one.
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        _LOGGER.debug(
            "refresh_access_token: no new refresh_token returned, keeping existing"
        )
        refresh_token = data["refresh_token"]

    expires_on = _parse_expiry(token_data, "refresh_access_token")
    _LOGGER.debug(
        "refresh_access_token: token valid until %s", expires_on.isoformat()
    )
    return "Bearer " + access_token, refresh_token, expires_on
