"""Config flow for Somfy RTS."""

from __future__ import annotations

import random
from typing import Any

try:
    from rf_protocols import ModulationType
except ImportError:
    ModulationType = None  # type: ignore[assignment]
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigEntry, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components.radio_frequency import async_get_transmitters
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import CONF_ADDRESS, CONF_COUNTER, CONF_TRANSMITTER, DEFAULT_NAME, DOMAIN, FREQUENCY
from .programming import async_send_programming_button


def _action_schema(*options: tuple[str, str], default: str) -> vol.Schema:
    """Build a single-field labeled-choice schema for a wizard step."""
    return vol.Schema(
        {
            vol.Required("action", default=default): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=value, label=label)
                        for value, label in options
                    ],
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )


_CONTINUE_SCHEMA = _action_schema(
    ("continue", "Continue"),
    ("retry", "Retry - resend the signal"),
    default="continue",
)

_JOG_TOP_SCHEMA = _action_schema(
    ("up", "Up"),
    ("down", "Down"),
    ("confirm", "Confirm - this is my TOP position"),
    default="up",
)

_JOG_BOTTOM_SCHEMA = _action_schema(
    ("down", "Down"),
    ("up", "Up - I overshot"),
    ("stop", "Stop - this is my BOTTOM position"),
    default="down",
)


class SomfyRTSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Somfy RTS."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step: select transmitter."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._transmitter = user_input[CONF_TRANSMITTER]
            return await self.async_step_setup_choice()

        # Get available transmitters
        try:
            transmitters = async_get_transmitters(
                self.hass, FREQUENCY, ModulationType.OOK
            )
        except HomeAssistantError:
            return self.async_abort(reason="no_transmitters")

        if not transmitters:
            return self.async_abort(reason="no_compatible_transmitters")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRANSMITTER): EntitySelector(
                        EntitySelectorConfig(
                            domain="radio_frequency",
                            include_entities=transmitters,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_setup_choice(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask whether the user already has an address/counter, or needs
        to pair a new motor.
        """
        return self.async_show_menu(
            step_id="setup_choice",
            menu_options={
                "configure": "I already have an address and rolling code",
                "pair_intro": "Pair a new motor (I can long-press its reset button)",
            },
        )

    async def async_step_configure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the configuration step: enter address and counter."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                address = int(user_input[CONF_ADDRESS], 16)
            except ValueError:
                errors[CONF_ADDRESS] = "invalid_hex"
            else:
                if not (0 <= address <= 0xFFFFFF):
                    errors[CONF_ADDRESS] = "address_out_of_range"

            if not errors:
                return self.async_create_entry(
                    title=user_input.get("name", DEFAULT_NAME),
                    data={
                        CONF_TRANSMITTER: self._transmitter,
                        CONF_ADDRESS: address,
                        CONF_COUNTER: int(user_input[CONF_COUNTER]),
                    },
                )

        return self.async_show_form(
            step_id="configure",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=DEFAULT_NAME): str,
                    vol.Required(CONF_ADDRESS, default="970229"): str,
                    vol.Required(CONF_COUNTER, default=0): int,
                }
            ),
            errors=errors,
        )

    # ── Guided pairing wizard ───────────────────────────────────────────
    #
    # Registers Home Assistant as a new remote and walks through setting a
    # motor's travel limits, mirroring a captured real pairing sequence:
    #   up+down -> jog to TOP -> my+down -> jog/stop at BOTTOM
    #   -> my+up (back to TOP) -> long-press my -> prog

    async def _async_send(self, button: str, *, frame_repeats: int = 0) -> None:
        """Send one programming-sequence command with the next rolling code."""
        self._pair_rolling_code += 1
        await async_send_programming_button(
            self.hass,
            transmitter=self._transmitter,
            address=self._pair_address,
            rolling_code=self._pair_rolling_code,
            button=button,
            frame_repeats=frame_repeats,
        )

    async def async_step_pair_intro(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Explain the physical first step and collect a name."""
        if user_input is not None:
            self._pair_name = user_input.get("name", DEFAULT_NAME)
            self._pair_address = random.randint(0, 0xFFFFFF)
            self._pair_rolling_code = 0
            return await self.async_step_pair_register()

        return self.async_show_form(
            step_id="pair_intro",
            data_schema=vol.Schema({vol.Required("name", default=DEFAULT_NAME): str}),
        )

    async def async_step_pair_register(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Send Up+Down to register this new remote with the motor."""
        if user_input is None:
            await self._async_send("up_down")
            return self.async_show_form(
                step_id="pair_register", data_schema=_CONTINUE_SCHEMA
            )

        if user_input["action"] == "retry":
            await self._async_send("up_down")
            return self.async_show_form(
                step_id="pair_register", data_schema=_CONTINUE_SCHEMA
            )

        return await self.async_step_pair_top()

    async def async_step_pair_top(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Jog with Up/Down until the blind is at the desired top position."""
        if user_input is not None:
            action = user_input["action"]
            if action == "confirm":
                return await self.async_step_pair_bottom()
            await self._async_send(action)  # "up" or "down"

        return self.async_show_form(step_id="pair_top", data_schema=_JOG_TOP_SCHEMA)

    async def async_step_pair_bottom(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Send My+Down, then jog/stop at the desired bottom position."""
        if user_input is None:
            await self._async_send("my_down")
            return self.async_show_form(
                step_id="pair_bottom", data_schema=_JOG_BOTTOM_SCHEMA
            )

        action = user_input["action"]
        if action == "stop":
            await self._async_send("my")
            return await self.async_step_pair_top_confirm()

        await self._async_send(action)  # "down" or "up"
        return self.async_show_form(
            step_id="pair_bottom", data_schema=_JOG_BOTTOM_SCHEMA
        )

    async def async_step_pair_top_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Send My+Up to run back to the top and confirm the limit."""
        if user_input is None or user_input["action"] == "retry":
            await self._async_send("my_up")
            return self.async_show_form(
                step_id="pair_top_confirm", data_schema=_CONTINUE_SCHEMA
            )

        return await self.async_step_pair_long_my()

    async def async_step_pair_long_my(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Send a long My/Stop press (6 total frames) to save the limits."""
        if user_input is None or user_input["action"] == "retry":
            await self._async_send("my", frame_repeats=5)
            return self.async_show_form(
                step_id="pair_long_my", data_schema=_CONTINUE_SCHEMA
            )

        return await self.async_step_pair_prog()

    async def async_step_pair_prog(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Send Prog to finish registering this remote, then create the entry."""
        if user_input is None or user_input["action"] == "retry":
            await self._async_send("prog")
            return self.async_show_form(
                step_id="pair_prog", data_schema=_CONTINUE_SCHEMA
            )

        return self.async_create_entry(
            title=self._pair_name,
            data={
                CONF_TRANSMITTER: self._transmitter,
                CONF_ADDRESS: self._pair_address,
                CONF_COUNTER: self._pair_rolling_code,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return SomfyRTSOptionsFlow()


class SomfyRTSOptionsFlow(OptionsFlow):
    """Handle options for Somfy RTS."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options: update transmitter and counter."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TRANSMITTER,
                        default=self.config_entry.data.get(CONF_TRANSMITTER, ""),
                    ): str,
                    vol.Required(
                        CONF_COUNTER,
                        default=self.config_entry.data.get(CONF_COUNTER, 0),
                    ): int,
                }
            ),
        )
