"""Serialized access to the RF transmitter.

Every config entry (= every cover) used to transmit independently, so a
Home Assistant cover group fired all of its commands at the single RF
proxy at the same moment. Overlapping or back-to-back-without-gap
transmissions are dropped or garbled by the proxy / CC1101, and because
each command is a single frame (see ``cover.py``) there is no redundancy
to absorb that - one random curtain in the group simply doesn't move.

This module funnels every transmission for a given transmitter entity -
normal cover commands, the ``send_button`` service and the pairing wizard
- through one shared FIFO lock, and holds that lock until the waveform
has had time to finish playing on the device plus a silence gap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

import voluptuous as vol
from homeassistant.components.radio_frequency import async_send_command
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from rf_protocols.commands import RadioFrequencyCommand

from .const import (
    DOMAIN,
    TX_FALLBACK_AIRTIME,
    TX_GAP,
    TX_LOCK_TIMEOUT,
    TX_SEND_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

_DATA_TX_LOCKS = "tx_locks"


def _resolve_transmitter_id(hass: HomeAssistant, transmitter: str) -> str:
    """Normalise a transmitter reference (entity id or registry id).

    The lock must be keyed on the same string no matter which form a config
    entry stored, otherwise two covers on the same proxy would get two
    different locks and could transmit simultaneously again.
    """
    try:
        return er.async_validate_entity_id(er.async_get(hass), transmitter)
    except (vol.Invalid, ValueError, KeyError):
        return transmitter


def _get_lock(hass: HomeAssistant, transmitter_id: str) -> asyncio.Lock:
    """Return the shared lock for one physical transmitter."""
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(DOMAIN, {}).setdefault(
        _DATA_TX_LOCKS, {}
    )
    lock = locks.get(transmitter_id)
    if lock is None:
        lock = locks[transmitter_id] = asyncio.Lock()
    return lock


def estimate_airtime(command: RadioFrequencyCommand) -> float:
    """Seconds the device needs to play this command over the air.

    ``repeat_count`` is the number of *additional* times the proxy plays the
    full timing sequence. If anything about the command is unexpected, fall
    back to a generous fixed value rather than risk under-waiting.
    """
    try:
        one_pass = sum(abs(t) for t in command.get_raw_timings()) / 1_000_000
        passes = 1 + max(0, int(getattr(command, "repeat_count", 0) or 0))
        airtime = one_pass * passes
    except Exception:  # noqa: BLE001 - defensive, never block a send on this
        _LOGGER.debug("Could not compute airtime for %r", command, exc_info=True)
        return TX_FALLBACK_AIRTIME
    if not 0 < airtime < 60:
        return TX_FALLBACK_AIRTIME
    return airtime


async def async_transmit(
    hass: HomeAssistant,
    transmitter: str,
    build_command: Callable[[], Awaitable[RadioFrequencyCommand | None]],
    *,
    context: Context | None = None,
    description: str = "",
) -> bool:
    """Transmit one command with exclusive use of the transmitter.

    ``build_command`` is awaited *after* the lock is acquired. That lets the
    caller reserve (and persist) the rolling code at the last moment and decide, once its
    turn has actually come, that the transmission is no longer wanted
    (return ``None``) - e.g. a queued second press that was superseded by a
    newer command while it was waiting.

    Returns True if something was sent, False if the caller skipped it.
    Raises HomeAssistantError if the lock or the send times out.
    """
    transmitter_id = _resolve_transmitter_id(hass, transmitter)
    lock = _get_lock(hass, transmitter_id)

    try:
        async with asyncio.timeout(TX_LOCK_TIMEOUT):
            await lock.acquire()
    except TimeoutError as err:
        raise HomeAssistantError(
            f"Timed out waiting for RF transmitter {transmitter_id} ({description})"
        ) from err

    try:
        command = await build_command()
        if command is None:
            _LOGGER.debug("Skipped transmission on %s: %s", transmitter_id, description)
            return False

        airtime = estimate_airtime(command)
        _LOGGER.debug(
            "Transmitting on %s: %s (~%.2fs airtime)",
            transmitter_id,
            description,
            airtime,
        )
        try:
            async with asyncio.timeout(TX_SEND_TIMEOUT):
                await async_send_command(hass, transmitter, command, context=context)
        except TimeoutError as err:
            raise HomeAssistantError(
                f"RF transmitter {transmitter_id} did not accept command ({description})"
            ) from err
        finally:
            # Hold the lock for the full airtime + a silence gap even if the
            # send failed half-way: we can't know how much was already put
            # on air, and the next sender must not overlap with it.
            # async_send_command may return as soon as the proxy accepted the
            # request, before the waveform has finished playing.
            await asyncio.sleep(airtime + TX_GAP)
        return True
    finally:
        lock.release()
