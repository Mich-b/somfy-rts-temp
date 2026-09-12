"""Shared helpers for Somfy RTS motor setup / limit-programming sequences.

Registering a new remote and setting a motor's travel limits uses combined
button presses and PROG, which plain open/close/stop can't express. This is
used by both the config flow's guided pairing wizard and the
``send_button`` entity service.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.radio_frequency import async_send_command
from homeassistant.core import Context, HomeAssistant
from rf_protocols.codes.somfy.rts import SomfyRTSButton
from rf_protocols.commands.somfy_rts import SomfyRTSCommand

# Codes captured from a real pairing/limit-setting sequence on the physical
# remote (combined presses are the bitwise OR of the individual buttons).
BUTTON_CODES: dict[str, int] = {
    "up": SomfyRTSButton.UP,  # 0x2
    "down": SomfyRTSButton.DOWN,  # 0x4
    "up_down": SomfyRTSButton.UP | SomfyRTSButton.DOWN,  # 0x6
    "my_down": SomfyRTSButton.MY | SomfyRTSButton.DOWN,  # 0x5
    "my": SomfyRTSButton.MY,  # 0x1
    "my_up": SomfyRTSButton.MY | SomfyRTSButton.UP,  # 0x3
    "prog": SomfyRTSButton.PROG,  # 0x8
}


async def async_send_programming_button(
    hass: HomeAssistant,
    *,
    transmitter: str,
    address: int,
    rolling_code: int,
    button: str,
    frame_repeats: int = 0,
    context: Context | None = None,
) -> None:
    """Transmit one programming-sequence button code.

    Unlike the cover entity's normal commands, this does not persist the
    rolling code anywhere - callers (the pairing wizard, the entity
    service) are responsible for tracking/persisting it themselves.
    """
    command: Any = SomfyRTSCommand(
        address=address,
        rolling_code=rolling_code,
        button=BUTTON_CODES[button],
        frame_repeats=frame_repeats,
    )
    await async_send_command(hass, transmitter, command, context=context)
