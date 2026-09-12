"""RF command wrapper for the radio_frequency platform.

Uses the official ``rf-protocols`` library for Somfy RTS command encoding,
with a local override for diagnostic testing (see ``SomfyRTSCommand`` below).
"""

from __future__ import annotations

from rf_protocols.codes.somfy.rts import SomfyRTSButton
from rf_protocols.commands.somfy_rts import SomfyRTSCommand as _UpstreamSomfyRTSCommand

# Re-export for convenience
SomfyRTSButton = SomfyRTSButton


class SomfyRTSCommand(_UpstreamSomfyRTSCommand):
    """Somfy RTS command with an explicit, caller-supplied key byte.

    Upstream hardcodes the key byte to a fixed 0xA7, which the protocol spec
    (pushstack) says is valid ("leaving this on a constant value also
    works") and receivers reportedly don't validate. Genuine remotes vary
    this byte alongside the rolling code instead, so we track it the same
    way we track the rolling code: seeded from a real capture at pairing
    time and incremented by the caller between transmissions.
    """

    def __init__(
        self,
        *,
        address: int,
        rolling_code: int,
        button: int,
        key: int,
        frame_repeats: int = 3,
    ) -> None:
        """Initialize the Somfy RTS command."""
        super().__init__(
            address=address,
            rolling_code=rolling_code,
            button=button,
            frame_repeats=frame_repeats,
        )
        self.key = key

    def get_raw_timings(self) -> list[int]:
        """Compute Somfy RTS frame timings (identical to upstream, except
        for the key byte on the line marked below).
        """
        frame = bytearray(7)
        frame[0] = self.key & 0xFF  # encryption key (caller-supplied, not fixed)
        frame[1] = self.button << 4  # command nibble; lower = checksum
        frame[2] = (self.rolling_code >> 8) & 0xFF
        frame[3] = self.rolling_code & 0xFF
        frame[4] = (self.address >> 16) & 0xFF
        frame[5] = (self.address >> 8) & 0xFF
        frame[6] = self.address & 0xFF

        # Checksum: XOR of all nibbles across all 7 bytes
        cksum = 0
        for byte in frame:
            cksum ^= byte ^ (byte >> 4)
        frame[1] |= cksum & 0x0F

        # Obfuscation: rolling XOR
        for i in range(1, 7):
            frame[i] ^= frame[i - 1]

        # ── Build OOK pulse sequence ──────────────────────────────────────
        timings: list[int] = []

        def add(us: int) -> None:
            if timings and (us > 0) == (timings[-1] > 0):
                timings[-1] += us
            else:
                timings.append(us)

        def encode_frame(sync: int) -> None:
            if sync == 2:
                add(9415)
                add(-89565)  # wake-up pulse + silence
            for _ in range(sync):
                add(2416)
                add(-2416)  # hardware sync pulses
            add(4550)
            add(-604)  # software sync
            # 56 data bits, MSB first, Manchester encoded
            for b in range(56):
                if (frame[b // 8] >> (7 - b % 8)) & 1:
                    add(-604)
                    add(604)
                else:
                    add(604)
                    add(-604)
            add(-30415)  # inter-frame gap

        encode_frame(2)
        for _ in range(self.frame_repeats):
            encode_frame(7)

        return timings


__all__ = ["SomfyRTSCommand", "SomfyRTSButton"]
