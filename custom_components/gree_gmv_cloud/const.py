"""Constants for the Gree GMV Cloud integration."""

from datetime import timedelta

DOMAIN = "gree_gmv_cloud"
PLATFORMS = ["climate"]

CONF_TOKEN = "token"
CONF_OPEN_ID = "open_id"
CONF_UID = "uid"
CONF_FAN_TARGETS = "fan_targets"

DEFAULT_BASE_URL = "https://a.gree.com:7016"
DEFAULT_SCAN_INTERVAL = timedelta(seconds=30)
TOKEN_REFRESH_MARGIN = timedelta(hours=6)
WRITE_READBACK_DELAY = 3
FAN_FINAL_READBACK_ADDITIONAL_DELAY = 2
FAN_TARGET_RECONCILE_GRACE = 45

MIN_TEMPERATURE = 16
MAX_TEMPERATURE = 30
TEMPERATURE_STEP = 0.5

MODE_COOL = 1
MODE_DRY = 2
MODE_FAN = 3
MODE_HEAT = 4
MODE_AUTO = 5

FAN_AUTO = "auto"
FAN_OFF = "off"
FAN_LOW = "low"
FAN_MIDDLE = "middle"
FAN_MEDIUM_LOW = "medium_low"
FAN_MEDIUM = "medium"
FAN_MEDIUM_HIGH = "medium_high"
FAN_HIGH = "high"

FAN_MODE_TO_CONTROL = {
    FAN_AUTO: 1,
    FAN_LOW: 2,
    FAN_MEDIUM_LOW: 3,
    FAN_MEDIUM: 4,
    FAN_MEDIUM_HIGH: 5,
    FAN_HIGH: 6,
}
FAN_MODES = list(FAN_MODE_TO_CONTROL)

# Home Assistant's HomeKit climate adapter exposes automatic as a separate
# target-fan-state characteristic and recognizes exactly these four ordered
# fixed-speed names. Fixed level 4 is omitted from the slider so it can expose
# four stable steps; the exact reported level remains available as an attribute.
HOMEKIT_FAN_MODES = [FAN_OFF, FAN_AUTO, FAN_LOW, FAN_MIDDLE, FAN_MEDIUM, FAN_HIGH]

REPORTED_FAN_LEVELS = {
    3: 1,
    4: 2,
    5: 3,
    6: 4,
    7: 5,
}

SENSITIVE_CONFIG_KEYS = {CONF_TOKEN, CONF_OPEN_ID, CONF_UID}
