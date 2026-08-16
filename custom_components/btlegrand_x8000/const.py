"""Constants for the Legrand/Bticino Smarther X8000 integration."""

DOMAIN = "btlegrand_x8000"
WEBHOOK_ID = f"{DOMAIN}_webhook"

DEFAULT_AUTH_BASE_URL: str = "https://partners-login.eliotbylegrand.com"
DEFAULT_API_BASE_URL: str = "https://api.developer.legrand.com"
DEFAULT_REDIRECT_URI: str = "https://my.home-assistant.io/"

# OAuth application credentials, supplied by the user during the config flow.
CLIENT_ID = ""
CLIENT_SECRET = ""
SUBSCRIPTION_KEY = ""

# Temperature bounds (°C) offered by the climate entity.
DEFAULT_MIN_TEMP = 7
DEFAULT_MAX_TEMP = 40

# Set by the release workflow from the git tag; keep the names stable.
INTEGRATION_VERSION = "main"
MIN_REQUIRED_HA_VERSION = "2024.4.0"

# API endpoints (appended to the base URLs above).
AUTH_REQ_ENDPOINT = "/token"
AUTH_URL_ENDPOINT = "/authorize"
THERMOSTAT_API_ENDPOINT: str = "/smarther/v2.0"
PLANTS = "/plants"
TOPOLOGY = "/topology"

# Config-entry option keys.
CONF_UPDATE_INTERVAL = "update_interval"
CONF_COOL_DOWN = "cool_down_interval"
CONF_DEBOUNCE = "webhook_debounce"
CONF_NOTIFY_ERRORS = "notify_errors"
CONF_BTLG_DAILY_QUOTA = "btlg_api_daily_quota"
CONF_SMART_POLLING = "smart_polling_enabled"
CONF_PASSIVE_MULTIPLIER = "passive_multiplier"

# Default option values.
DEFAULT_UPDATE_INTERVAL = 15     # minutes (standard polling)
DEFAULT_COOL_DOWN = 60           # minutes (wait after a rate-limit ban)
DEFAULT_DEBOUNCE = 1.0           # seconds (window to group webhook events)
DEFAULT_NOTIFY_ERRORS = True     # show a persistent notification on error
DEFAULT_BTLG_DAILY_QUOTA = 500   # calls/day (Legrand Starter Kit limit)
DEFAULT_SMART_POLLING = True     # adaptive polling on by default

# Passive-mode multiplier: an OFF / antifrost thermostat is polled this many
# times less often than an active (heating/cooling) one, e.g. with a 15 min
# base interval a passive zone is polled every 15 * 4 = 60 min. User-tunable.
DEFAULT_PASSIVE_MULTIPLIER = 4

# Bounds enforced by the number entities.
MIN_UPDATE_INTERVAL = 1          # minutes
MAX_UPDATE_INTERVAL = 120        # minutes
MIN_COOL_DOWN = 15               # minutes
MAX_COOL_DOWN = 180              # minutes
MIN_DEBOUNCE = 0.5               # seconds
MAX_DEBOUNCE = 5.0               # seconds
MIN_BTLG_DAILY_QUOTA = 100       # calls/day
MAX_BTLG_DAILY_QUOTA = 10000     # calls/day
MIN_PASSIVE_MULTIPLIER = 1       # 1 = treat passive zones like active ones
MAX_PASSIVE_MULTIPLIER = 12      # 12 = extreme slow-down for dormant zones

# --- Adaptive API-budget throttling (smart polling) ---
# When smart polling is on, the coordinator slows down as the remaining daily
# budget (daily_api_quota - calls_used) runs low. The tiers are DERIVED from the
# two user knobs (update_interval and daily_api_quota) so they scale coherently
# with any user setting instead of being fixed magic numbers.
#
# Tier thresholds, expressed as a fraction of the daily quota. Below the
# fraction the corresponding (slower) tier engages. Example at quota 500:
# economy < 100, survival < 40, frozen < 5 (frozen is floored at min(5, 2%)).
ECONOMY_BUDGET_FRACTION = 0.20    # < 20% of quota -> economy tier
SURVIVAL_BUDGET_FRACTION = 0.08   # < 8%  of quota -> survival tier
FROZEN_BUDGET_FRACTION = 0.02     # < 2%  of quota -> freeze scheduled polling
FROZEN_BUDGET_MIN = 5             # never freeze with more than this many calls left
#
# Active-zone interval per tier, expressed as a multiple of update_interval.
# Passive zones are additionally slowed by the passive multiplier. Example at
# update_interval 15 min: economy active 30 min, survival active 60 min.
ECONOMY_INTERVAL_FACTOR = 2       # economy active interval = base * 2
SURVIVAL_INTERVAL_FACTOR = 4      # survival active interval = base * 4

# Tier labels surfaced by the diagnostic sensor.
TIER_OFF = "disabled"            # smart polling switched off
TIER_NORMAL = "normal"
TIER_ECONOMY = "economy"
TIER_SURVIVAL = "survival"
TIER_FROZEN = "frozen"

# Below this fraction of the day elapsed the projected-daily-calls figure is
# too noisy to be meaningful, so the diagnostic sensor reports the raw count.
PROJECTION_MIN_DAY_FRACTION = 0.04  # ~ first hour of the day
