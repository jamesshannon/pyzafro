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

#: How long a device may go without saying anything before it is probed with a cmd:3.
#:
#: Nothing else re-reads state. There is no poll, and neither of the two things that
#: might stand in for one is dependable. The `lwt/` beacon was documented as a ~10-40s
#: heartbeat, but a 126s capture of a *running* unit carried exactly one and a 103s
#: capture of the same unit idle carried none, with nothing retained on the topic at
#: subscribe time either. Unsolicited cmd:4 deltas are just as conditional: 42 in the
#: first of those captures, 0 in the second. An idle device is simply silent.
#:
#: So availability has to be asked for. The probe is 38 bytes and the reply under 600,
#: once a minute per device, which is what the app does every time a device page opens.
PROBE_INTERVAL: Final = 60.0

#: Requests one probe makes before it counts as unanswered. Commands go out at QoS 0
#: — the broker's CONNECT is `wq0` and every publish logs `q0` — so a single request
#: the device never sees is an ordinary event on this transport, not a fault. Asking
#: twice is what separates a dropped publish from a device that is not there.
PROBE_ATTEMPTS: Final = 2

#: How long a device must go on not answering before it is reported unavailable.
#:
#: A duration rather than a count of misses. The count was a bad way to say this: it
#: took REQUEST_TIMEOUT, PROBE_ATTEMPTS and the probe cadence multiplied together to
#: work out what it meant in seconds, and the answer moved whenever any of them was
#: tuned. What matters is how long a fault has to last, so that is what this states.
#:
#: A floor, not the typical figure: the run is only reviewed when a probe finishes,
#: so in practice a device is reported unavailable after ~90-110s of continuous
#: silence — three or four unanswered requests. Deliberately long. An availability
#: change is recorded by the consumer and read by a human afterwards, so a false one
#: costs more than a reading that stays a minute stale.
UNANSWERED_GRACE: Final = 90.0

RECONNECT_MIN_DELAY: Final = 1.0
RECONNECT_MAX_DELAY: Final = 300.0

#: How long a connection must hold before the backoff is treated as spent. Without this
#: the delay only ever grows, so a client that has been up for a day reconnects from a
#: dropped socket five minutes later, having earned that penalty one routine drop at a
#: time. Shorter than the 60s keepalive, because a connection that survives half of one
#: is working; anything briefer looks like a flap and should still be backed off.
RECONNECT_RESET_AFTER: Final = 30.0

#: How long the transport may be down before devices are reported unavailable. A cloud
#: broker drops a websocket every so often and the reconnect takes a second or two; the
#: device is reachable either side of it, so announcing an outage for that gap is noise
#: the consumer then has to filter. An LWT saying a device is gone is not deferred —
#: that is the device speaking, not the socket.
OFFLINE_GRACE: Final = 60.0
