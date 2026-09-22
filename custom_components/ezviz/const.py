"""Constants for the ezviz integration."""

DOMAIN = "ezviz"
MANUFACTURER = "EZVIZ"

# Configuration
ATTR_SERIAL = "serial"
CONF_FFMPEG_ARGUMENTS = "ffmpeg_arguments"

# Identifiants Open Platform, necessaires au flux des cameras sur batterie.
# Ils ne periment pas : c'est le jeton derive qui expire, et il se renouvelle
# seul. Voir stream_proxy.
CONF_APP_KEY = "app_key"
CONF_APP_SECRET = "app_secret"
CONF_OPEN_HOST = "open_host"
DEFAULT_OPEN_HOST = "ieuopen.ezvizlife.com"

# Largeur du transcodage. Le Pi 5 n'a pas d'encodeur H.264 materiel : le cout
# suit le nombre de pixels, et un encodeur en retard ne ralentit pas -- il PERD
# des images. Reglable, parce que le bon compromis depend de la machine et du
# nombre de cameras ouvertes en meme temps.
#      0  AUCUN transcodage : le flux passe tel quel, cout processeur nul et
#         cadence pleine, mais en H.265 que tous les navigateurs ne lisent pas
#   1280  720p   le plus fluide des modes transcodes
#   1920  1080p  defaut
#   2560  1440p  a reserver aux machines confortables
CONF_STREAM_WIDTH = "stream_width"
DEFAULT_STREAM_WIDTH = 1920
ATTR_TYPE_CLOUD = "EZVIZ_CLOUD_ACCOUNT"
ATTR_TYPE_CAMERA = "CAMERA_ACCOUNT"
CONF_SESSION_ID = "session_id"
CONF_RFSESSION_ID = "rf_session_id"
CONF_EZVIZ_ACCOUNT = "ezviz_account"

# Services data
DIR_UP = "up"
DIR_DOWN = "down"
DIR_LEFT = "left"
DIR_RIGHT = "right"
ATTR_ENABLE = "enable"
ATTR_DIRECTION = "direction"
ATTR_SPEED = "speed"
ATTR_LEVEL = "level"
ATTR_TYPE = "type_value"

# Service names
SERVICE_WAKE_DEVICE = "wake_device"
SERVICE_DETECTION_SENSITIVITY = "set_alarm_detection_sensibility"

# Defaults
EU_URL = "apiieu.ezvizlife.com"
RUSSIA_URL = "apirus.ezvizru.com"
DEFAULT_CAMERA_USERNAME = "admin"
DEFAULT_TIMEOUT = 25
DEFAULT_FFMPEG_ARGUMENTS = "/Streaming/Channels/102"
