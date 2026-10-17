"""Constants for the Somfy RTS integration."""

from typing import Final

DOMAIN: Final = "somfy_rts_temp"

CONF_ADDRESS: Final = "address"
CONF_COUNTER: Final = "rolling_code"
CONF_TRANSMITTER: Final = "transmitter_entity"

STORAGE_VERSION: Final = 1

DEFAULT_NAME: Final = "Somfy RTS Cover"
FREQUENCY: Final = 433_420_000

# ── Transmission pacing (see transmitter.py) ─────────────────────────────
# Silence between two transmissions on the same RF transmitter, on top of
# the computed airtime of the previous command.
TX_GAP: Final = 0.5
# Used when the airtime of a command can't be computed.
TX_FALLBACK_AIRTIME: Final = 2.0
# Max time to wait for our turn on the transmitter (a big group, each
# cover sending two presses, can legitimately queue for a while).
TX_LOCK_TIMEOUT: Final = 120.0
# Max time for the proxy to accept a single command.
TX_SEND_TIMEOUT: Final = 15.0

# ── Second press (see cover.py) ──────────────────────────────────────────
# UP/DOWN are sent a second time, as a genuinely new press (new rolling
# code), this long after the first one went out. Must be clearly longer
# than a "held button" window so the motor sees two separate presses.
SECOND_PRESS_DELAY: Final = 2.0
