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
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import CONF_ADDRESS, CONF_COUNTER, CONF_TRANSMITTER, DEFAULT_NAME, DOMAIN, FREQUENCY
from .programming import async_send_programming_button


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
            menu_options=["configure", "pair_intro"],
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
    #
    # Every step here is a menu: each option is its own async_step_* that
    # sends one command and either loops back to the same menu (jogging,
    # retry) or moves on to the next step. Retry/re-jog options simply
    # point back at the step that sends the command, so re-entering it
    # re-sends rather than needing separate branching logic.

    def _generate_unique_address(self) -> int:
        """Pick a random address not already used by an existing entry."""
        existing = {
            entry.data[CONF_ADDRESS]
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if CONF_ADDRESS in entry.data
        }
        for _ in range(100):
            address = random.randint(0, 0xFFFFFF)
            if address not in existing:
                return address
        raise HomeAssistantError("Could not generate a unique address")

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
            self._pair_address = self._generate_unique_address()
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
        await self._async_send("up_down")
        return self.async_show_menu(
            step_id="pair_register",
            menu_options=["pair_top", "pair_register"],
        )

    async def async_step_pair_top(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Jog with Up/Down until the blind is at the desired top position."""
        return self.async_show_menu(
            step_id="pair_top",
            menu_options=["pair_top_up", "pair_top_down", "pair_bottom"],
        )

    async def async_step_pair_top_up(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Jog up once, then return to the top-jog menu."""
        await self._async_send("up")
        return await self.async_step_pair_top()

    async def async_step_pair_top_down(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Jog down once, then return to the top-jog menu."""
        await self._async_send("down")
        return await self.async_step_pair_top()

    async def async_step_pair_bottom(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Send My+Down once, then hand off to the interactive jog menu."""
        await self._async_send("my_down")
        return await self.async_step_pair_bottom_jog()

    async def async_step_pair_bottom_jog(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Jog/stop at the desired bottom position."""
        return self.async_show_menu(
            step_id="pair_bottom_jog",
            menu_options=[
                "pair_bottom_resend",
                "pair_bottom_down",
                "pair_bottom_up",
                "pair_bottom_stop",
                "pair_bottom_confirm",
            ],
        )

    async def async_step_pair_bottom_resend(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Resend My+Down, e.g. if the blind never started moving."""
        await self._async_send("my_down")
        return await self.async_step_pair_bottom_jog()

    async def async_step_pair_bottom_down(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Jog down once, then return to the bottom-jog menu."""
        await self._async_send("down")
        return await self.async_step_pair_bottom_jog()

    async def async_step_pair_bottom_up(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Jog up once (overshoot correction), then return to the menu."""
        await self._async_send("up")
        return await self.async_step_pair_bottom_jog()

    async def async_step_pair_bottom_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Stop the motor, then return to the jog menu to fine-tune or confirm."""
        await self._async_send("my")
        return await self.async_step_pair_bottom_jog()

    async def async_step_pair_bottom_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Move on - assumes the motor is already stopped where you want it."""
        return await self.async_step_pair_top_confirm()

    async def async_step_pair_top_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Send My+Up to run back to the top and confirm the limit."""
        await self._async_send("my_up")
        return self.async_show_menu(
            step_id="pair_top_confirm",
            menu_options=["pair_long_my", "pair_top_confirm"],
        )

    async def async_step_pair_long_my(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Send a long My/Stop press (6 total frames) to save the limits."""
        await self._async_send("my", frame_repeats=5)
        return self.async_show_menu(
            step_id="pair_long_my",
            menu_options=["pair_prog", "pair_long_my"],
        )

    async def async_step_pair_prog(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Send Prog to finish registering this remote."""
        await self._async_send("prog")
        return self.async_show_menu(
            step_id="pair_prog",
            menu_options=["pair_finish", "pair_prog"],
        )

    async def async_step_pair_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create the entry with whatever rolling code the sequence ended on."""
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
