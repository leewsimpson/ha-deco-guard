"""Constants for Deco Guard."""

DOMAIN = "deco_guard"

CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_HOST = "http://192.168.68.1"
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 15

STORAGE_VERSION = 1

EVENT_NEW_DEVICE = f"{DOMAIN}_new_device"
SIGNAL_NEW_APPROVED = f"{DOMAIN}_new_approved_{{}}"

PANEL_URL_PATH = "deco-guard"
PANEL_COMPONENT = "deco-guard-panel"
PANEL_STATIC_URL = "/deco_guard_static"

# Device review status (stored per MAC).
STATUS_APPROVED = "approved"
STATUS_PENDING = "pending"  # new device, quarantined (blocked) until reviewed
STATUS_DENIED = "denied"  # reviewed and kept blocked permanently
