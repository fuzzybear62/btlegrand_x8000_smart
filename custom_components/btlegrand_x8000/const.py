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
MIN_REQUIRED_HA_VERSION = "2024.1.0b0"

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

# Default option values.
DEFAULT_UPDATE_INTERVAL = 15     # minutes (standard polling)
DEFAULT_COOL_DOWN = 60           # minutes (wait after a rate-limit ban)
DEFAULT_DEBOUNCE = 1.0           # seconds (window to group webhook events)
DEFAULT_NOTIFY_ERRORS = True     # show a persistent notification on error
DEFAULT_BTLG_DAILY_QUOTA = 500   # calls/day (Legrand Starter Kit limit)
DEFAULT_SMART_POLLING = True     # adaptive polling on by default

# Passive-mode multiplier: an OFF thermostat is polled this many times less
# often than an active one (e.g. 15 min * 4 = 60 min for inactive zones).
PASSIVE_POLLING_MULTIPLIER = 4

# Bounds enforced by the number entities.
MIN_UPDATE_INTERVAL = 1          # minutes
MAX_UPDATE_INTERVAL = 120        # minutes
MIN_COOL_DOWN = 15               # minutes
MAX_COOL_DOWN = 180              # minutes
MIN_DEBOUNCE = 0.5               # seconds
MAX_DEBOUNCE = 5.0               # seconds
MIN_BTLG_DAILY_QUOTA = 100       # calls/day
MAX_BTLG_DAILY_QUOTA = 10000     # calls/day

# Adaptive API-budget throttling (smart polling).
# When smart polling is on, the coordinator slows down as the remaining daily
# budget (daily_api_quota - calls_used) runs low, in two tiers. The interval
# below applies to active (heating/cooling) zones; passive (OFF / antifrost)
# zones are polled PASSIVE_POLLING_MULTIPLIER x slower, mirroring normal mode.
#
# Tier thresholds, in API calls still available for the day:
BUDGET_ECONOMY_THRESHOLD = 100    # below this -> economy tier (slower)
BUDGET_SURVIVAL_THRESHOLD = 40    # below this -> survival tier (slowest)
#
# Active-zone interval per tier, in minutes:
ECONOMY_ACTIVE_INTERVAL_MIN = 30   # -> passive 30 * 4 = 120 min
SURVIVAL_ACTIVE_INTERVAL_MIN = 60  # -> passive 60 * 4 = 240 min
