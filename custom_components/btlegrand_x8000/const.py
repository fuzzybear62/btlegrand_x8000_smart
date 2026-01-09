"""Constant."""

# UPDATED: New Domain Name
DOMAIN = "btlegrand_x8000"
# UPDATED: Dynamic reference to DOMAIN for easier future refactoring
WEBHOOK_ID = f"{DOMAIN}_webhook"
DEFAULT_AUTH_BASE_URL: str = "https://partners-login.eliotbylegrand.com"
DEFAULT_API_BASE_URL: str = "https://api.developer.legrand.com"
DEFAULT_REDIRECT_URI: str = "https://my.home-assistant.io/"
CLIENT_ID = ""
CLIENT_SECRET = ""
SUBSCRIPTION_KEY = ""

# Temperature Defaults
DEFAULT_MIN_TEMP = 7
DEFAULT_MAX_TEMP = 40

################################
# Do not change! Will be set by release workflow
INTEGRATION_VERSION = "main"  # git tag will be used
MIN_REQUIRED_HA_VERSION = "2024.1.0b0"  # set min required version in hacs.json
################################
# View
# UPDATED: Dynamic paths based on DOMAIN
AUTH_CALLBACK_PATH = f"/auth/{DOMAIN}/callback/"
AUTH_CALLBACK_NAME = f"auth:{DOMAIN}:callback"
# Endpoints
AUTH_REQ_ENDPOINT = "/token"
AUTH_URL_ENDPOINT = "/authorize"
AUTH_CHECK_ENDPOINT = "/echo/resource"
THERMOSTAT_API_ENDPOINT: str = "/smarther/v2.0"
PLANTS = "/plants"
TOPOLOGY = "/topology"

# Attributes
ATTR_END_DATETIME = "end_datetime"
ATTR_TARGET_TEMPERATURE = "target_temperature"
ATTR_TIME_PERIOD = "time_period"
ATTR_TIME_BOOST_MODE = "boost_time"
ATTR_HVAC_MODE = "hvac_mode"
ATTR_SCHEDULE_NAME = "schedule_name"

# services
SERVICE_SET_BOOST_MODE = "set_boost_mode"
SERVICE_SET_TEMPERATURE_WITH_END_DATETIME = "set_temperature_with_end_datetime"
SERVICE_SET_TEMPERATURE_WITH_TIME_PERIOD = "set_temperature_with_time_period"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_SET_TURN_OFF_WITH_END_DATETIME = "set_turn_off_with_end_datetime"
SERVICE_SET_TURN_OFF_WITH_TIME_PERIOD = "set_turn_off_with_time_period"

# config
CONF_URL_WARNING = "url_warning"
CONF_LEGACY_MODE = "legacy_mode"
CONF_EXTERNAL_URL = "external_url"
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_SUBSCRIPTION_KEY = "subscription_key"
CONF_BROWSER_URL = "browser_url"

# --- NEW: Advanced Tuning Options ---

# Keys for Config Entry Options
CONF_UPDATE_INTERVAL = "update_interval"
CONF_COOL_DOWN = "cool_down_interval"
CONF_DEBOUNCE = "webhook_debounce"
CONF_NOTIFY_ERRORS = "notify_errors"
# New Key for Daily API Quota (Bticino Legrand)
CONF_BTLG_DAILY_QUOTA = "btlg_api_daily_quota"
# New Key for Smart Energy Saving (Optimized Polling)
CONF_SMART_POLLING = "smart_polling_enabled"

# Default Values
DEFAULT_UPDATE_INTERVAL = 15     # Minutes (Standard polling)
DEFAULT_COOL_DOWN = 60           # Minutes (Wait time after Rate Limit ban)
DEFAULT_DEBOUNCE = 1.0           # Seconds (Wait time to group webhook events)
DEFAULT_NOTIFY_ERRORS = True     # Boolean (Show persistent notification on error)
DEFAULT_BTLG_DAILY_QUOTA = 500   # Calls/Day (Standard Legrand Starter Kit limit)
DEFAULT_SMART_POLLING = True     # Default to ON (Recommended)

# Multiplier for Passive Mode
# If a thermostat is OFF, we poll it 4x less frequently.
# e.g. 15 min interval * 4 = 60 min for inactive zones.
PASSIVE_POLLING_MULTIPLIER = 4

# Boundaries for Number Entities
MIN_UPDATE_INTERVAL = 1          # Minimum 1 minute
MAX_UPDATE_INTERVAL = 120        # Maximum 2 hours
MIN_COOL_DOWN = 15               # Minimum 15 minutes
MAX_COOL_DOWN = 180              # Maximum 3 hours
MIN_DEBOUNCE = 0.5               # Minimum 0.5 seconds
MAX_DEBOUNCE = 5.0               # Maximum 5.0 seconds
MIN_BTLG_DAILY_QUOTA = 100       # Minimum allowed quota
MAX_BTLG_DAILY_QUOTA = 10000     # Maximum allowed quota (Future-proof)

# --- NEW: Adaptive Budgeting Constants (Algorithm Tuning) ---
BTICINO_DAILY_RESERVE = 20       # Safety buffer calls to keep at end of day
WEIGHT_ACTIVE = 4                # Cost weight for active thermostats
WEIGHT_PASSIVE = 1               # Cost weight for passive thermostats
COORDINATOR_HEARTBEAT = 60       # Seconds (1 min) for decision loop when Smart is ON