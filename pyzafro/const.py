"""Protocol constants.

Every value here is recovered from the shipping Android app and, where marked, confirmed
against live traffic. See ZAFRO_API_NOTES.md for the derivation of each one.
"""

from __future__ import annotations

from typing import Final

BASE_URL: Final = "https://zafro.nbrowan.com/iot1"
WS_HOST: Final = "zafro.nbrowan.com"
WS_PORT: Final = 443
WS_PATH: Final = "/ws/iot1/"

#: The app hardcodes this on login for every user in every country and the server does
#: not validate it. It is a constant, not a parameter.
LOGIN_COUNTRY: Final = "US"

# --- REST envelope -----------------------------------------------------------------
#: Body-level success code. Independent of the HTTP status.
OK_CODE: Final = 0
#: Body-level codes meaning the session is dead.
UNAUTHORIZED_CODES: Final = frozenset({401, 424})

PATH_LOGIN: Final = "/user/login"
PATH_DEVICE_LIST: Final = "/device/list"
PATH_MQTT_USERINFO: Final = "/mqtt/userinfo"
PATH_ROOM_LIST: Final = "/user/room/list"

#: Tokens come back with expires_in=604800 (7 days). Refresh at this fraction of it.
TOKEN_REFRESH_RATIO: Final = 0.9
#: Used only if a login response omits expires_in.
TOKEN_DEFAULT_LIFETIME: Final = 604800

# --- MQTT --------------------------------------------------------------------------
TOPIC_REQUEST: Final = "dev/{vendor}/{sn}/command/request"
TOPIC_REPLY: Final = "dev/{vendor}/{sn}/command/reply"
TOPIC_LWT: Final = "lwt/{vendor}/{sn}"

CMD_PRESENCE: Final = 1  # device -> app, on lwt/
CMD_ACCEPT_STATE: Final = 2  # purpose unresolved; do not use
CMD_STATE: Final = 3  # both ways; full state
CMD_STATE_PUSH: Final = 4  # device -> app; delta only
CMD_BASE_INFO: Final = 5  # both ways
CMD_CONTROL: Final = 6  # app -> device; data = {"state": {...}}
CMD_UNBIND: Final = 9
CMD_UPGRADE: Final = 12

#: The only two commands that carry state. Mirrors the app's own stateStream filter.
STATE_CMDS: Final = frozenset({CMD_STATE, CMD_STATE_PUSH})

#: Seconds to wait for a solicited cmd:3 / cmd:5 reply.
REQUEST_TIMEOUT: Final = 15.0
#: Seconds after an optimistic write before an unconfirmed field forces a cmd:3 resync.
RESYNC_DELAY: Final = 5.0

RECONNECT_MIN_DELAY: Final = 1.0
RECONNECT_MAX_DELAY: Final = 300.0

#: How long the transport may be down before devices are reported unavailable. A cloud
#: broker drops a websocket every so often and the reconnect takes a second or two; the
#: device is reachable either side of it, so announcing an outage for that gap is noise
#: the consumer then has to filter. An LWT saying a device is gone is not deferred —
#: that is the device speaking, not the socket.
OFFLINE_GRACE: Final = 60.0
