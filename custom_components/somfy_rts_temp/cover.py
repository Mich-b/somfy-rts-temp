"""Cover platform for Somfy RTS."""

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.cover import (
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import (
    STATE_CLOSED,
    STATE_OPEN,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from rf_protocols.codes.somfy.rts import SomfyRTSButton
from rf_protocols.commands.somfy_rts import SomfyRTSCommand

from .const import CONF_ADDRESS, CONF_TRANSMITTER, DOMAIN, SECOND_PRESS_DELAY
from .entity import SomfyRTSConfigEntry
from .programming import BUTTON_CODES
from .transmitter import async_transmit

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1

SERVICE_SEND_BUTTON = "send_button"

_PROGRAMMING_BUTTONS = ("up_down", "my_down", "my", "my_up", "prog")

_SEND_BUTTON_SCHEMA = {
    vol.Required("button"): vol.In(_PROGRAMMING_BUTTONS),
    vol.Optional("frame_repeats", default=0): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=50)
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SomfyRTSConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Somfy RTS cover platform."""
    async_add_entities([SomfyRTSCover(config_entry)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SEND_BUTTON,
        _SEND_BUTTON_SCHEMA,
        "async_send_button",
    )


class SomfyRTSCover(CoverEntity, RestoreEntity):
    """A Somfy RTS cover controlled via RF."""

    _attr_assumed_state = True
    _attr_available = False
    _attr_device_class = None
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )

    def __init__(self, entry: SomfyRTSConfigEntry) -> None:
        """Initialize the cover."""
        self._attr_is_closed = None
        self._entry = entry
        # Bumped on every new command; a queued second press only fires if
        # it is still the latest command for this cover.
        self._command_generation = 0
        self._transmitter: str = entry.data[CONF_TRANSMITTER]
        self._address: int = entry.data[CONF_ADDRESS]
        address_hex = format(self._address, "06X")
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Somfy",
            model="RTS Remote",
            name=f"Somfy RTS {address_hex}",
        )

    async def async_added_to_hass(self) -> None:
        """Restore last known state and subscribe to transmitter availability."""
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state == STATE_OPEN:
                self._attr_is_closed = False
            elif last_state.state == STATE_CLOSED:
                self._attr_is_closed = True

        transmitter_entity_id = er.async_validate_entity_id(
            er.async_get(self.hass), self._transmitter
        )

        @callback
        def _async_transmitter_state_changed(
            event: Event[EventStateChangedData],
        ) -> None:
            new_state = event.data["new_state"]
            transmitter_available = (
                new_state is not None and new_state.state != STATE_UNAVAILABLE
            )
            if transmitter_available != self.available:
                _LOGGER.info(
                    "Transmitter %s used by %s is %s",
                    transmitter_entity_id,
                    self.entity_id,
                    "available" if transmitter_available else "unavailable",
                )
                self._attr_available = transmitter_available
                self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [transmitter_entity_id],
                _async_transmitter_state_changed,
            )
        )

        transmitter_state = self.hass.states.get(transmitter_entity_id)
        self._attr_available = (
            transmitter_state is not None
            and transmitter_state.state != STATE_UNAVAILABLE
        )

    async def async_will_remove_from_hass(self) -> None:
        """Make sure no queued second press fires after removal/unload."""
        self._command_generation += 1
        await super().async_will_remove_from_hass()

    async def _async_send_command(
        self,
        button: int,
        *,
        frame_repeats: int = 0,
        generation: int | None = None,
        description: str = "",
    ) -> bool:
        """Transmit one press with a fresh rolling code.

        frame_repeats stays 0 for normal commands: extra frames with the same
        rolling code look like a *held* button to the motor and caused
        incomplete travel (see git history, Sep 2026).

        If ``generation`` is given, the press is only sent if no newer command
        was issued for this cover while it was waiting for the transmitter.
        Returns True if it was actually sent.
        """
        data = self._entry.runtime_data

        async def _build() -> SomfyRTSCommand | None:
            if generation is not None and generation != self._command_generation:
                return None
            async with data.lock:
                # Reserve and persist the rolling code *before* transmitting:
                # skipping a code is harmless (the motor accepts jumps ahead),
                # re-using one after a crash/cancel mid-send is not (the motor
                # would ignore that command).
                data.rolling_code += 1
                rolling_code = data.rolling_code
                try:
                    await data.store.async_save({"rolling_code": rolling_code})
                except Exception:  # noqa: BLE001 - never block a command on disk I/O
                    _LOGGER.exception(
                        "Could not persist rolling code %s for %s",
                        rolling_code,
                        self.entity_id,
                    )
            return SomfyRTSCommand(
                address=self._address,
                rolling_code=rolling_code,
                button=button,
                frame_repeats=frame_repeats,
            )

        return await async_transmit(
            self.hass,
            self._transmitter,
            _build,
            context=self._context,
            description=f"{description or button} for {self.entity_id}",
        )

    async def _async_move(self, button: int, name: str) -> None:
        """Send UP/DOWN, then schedule a second, independent press.

        The second press uses a new rolling code, so the motor sees two
        separate button presses rather than one held button. Pressing the
        same direction again while the motor is already moving is a no-op;
        if the first press was missed, the second one starts it.
        """
        self._command_generation += 1
        generation = self._command_generation
        try:
            await self._async_send_command(button, description=f"{name} (1/2)")
        finally:
            # Schedule the backup press even if the first one raised: that is
            # exactly the case where it's most useful. Background tasks on the
            # config entry are cancelled automatically when it is unloaded.
            self._entry.async_create_background_task(
                self.hass,
                self._async_second_press(button, name, generation),
                f"{DOMAIN} second {name} press for {self.entity_id}",
            )

    async def _async_second_press(
        self, button: int, name: str, generation: int
    ) -> None:
        """Re-send a movement command unless it has been superseded."""
        await asyncio.sleep(SECOND_PRESS_DELAY)
        if generation != self._command_generation:
            return
        try:
            await self._async_send_command(
                button, generation=generation, description=f"{name} (2/2)"
            )
        except HomeAssistantError as err:
            # The first press already went out; a failed backup press is only
            # worth a warning, not an error surfaced to the caller.
            _LOGGER.warning("Second %s press for %s failed: %s", name, self.entity_id, err)

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self._async_move(SomfyRTSButton.UP, "up")
        self._attr_is_closed = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        await self._async_move(SomfyRTSButton.DOWN, "down")
        self._attr_is_closed = True
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover.

        Sent once only, never repeated: a second MY on a stopped motor would
        send it to its favourite ("my") position. Bumping the generation also
        cancels any pending second UP/DOWN press, which would otherwise
        restart the motor right after this stop.
        """
        self._command_generation += 1
        await self._async_send_command(SomfyRTSButton.MY, description="my/stop")

    async def async_send_button(self, button: str, frame_repeats: int = 0) -> None:
        """Send a raw/combined button code for motor setup and limit
        programming (e.g. up+down, my+down, my+up, prog) that plain
        open/close/stop can't express. For a "long press", raise
        frame_repeats (e.g. 5 for 6 total frames) rather than calling this
        repeatedly, so it stays one rolling-code transmission.

        Sent exactly once (no second press) and cancels any pending second
        UP/DOWN press, so it can't interfere with a programming sequence.
        """
        self._command_generation += 1
        await self._async_send_command(
            BUTTON_CODES[button],
            frame_repeats=frame_repeats,
            description=f"button {button}",
        )
