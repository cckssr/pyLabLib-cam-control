"""Typed accessors over the ASCII command layer.

Argument syntax and response formats below are confirmed from live captures of ThorCam
driving a CS165CU and a CS135MU (protocol.md). Two conventions run through the whole
command set and are worth knowing before reading further:

* **Setters echo the value the camera actually applied**, which is often not the one you
  asked for -- it snaps exposure, frame period and ROI to what the sensor supports. Always
  believe the response, not the request.
* **Limits are dynamic.** ``exp.min``/``exp.max`` change when frame-rate control is toggled,
  ``sfr.min``/``sfr.max`` change with the ROI, and so on. Query them when you need them
  rather than caching.
"""

from __future__ import annotations

import json

from .protocol import (
    DISCOVER_COMMAND,
    MAX_SUFFIX,
    MIN_SUFFIX,
    SCHEMA_SUFFIX,
    CommandError,
)
from .transport import TsiCameraTransport

__all__ = ["CommandError", "Commands"]


class Commands:
    def __init__(self, transport: TsiCameraTransport) -> None:
        self._transport = transport

    # -- raw ---------------------------------------------------------------

    def query(self, name: str) -> str:
        """Send a raw command and return its response verbatim."""
        return self._transport.command(name)

    def supported(self) -> list[str]:
        """The command names this firmware advertises, straight from the camera.

        Not exhaustive: `iosetup` on the Zelux and `prnu` on the CS-series both work and
        appear in neither camera's `discover` output. To test for a command outside this
        list, send it and catch CommandError ("Invalid command").
        """
        return self._transport.command(DISCOVER_COMMAND).split()

    def schema(self, name: str) -> dict:
        """The camera's own JSON description of a command (`<name>.disc`).

        The firmware wraps the object in an extra pair of quotes, which is stripped here.
        """
        raw = self._transport.command(name + SCHEMA_SUFFIX).strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        return json.loads(raw)

    def limits(self, name: str) -> tuple[int, int]:
        """Current (min, max) for a numeric command. Both move with camera state."""
        return (
            int(self._transport.command(name + MIN_SUFFIX)),
            int(self._transport.command(name + MAX_SUFFIX)),
        )

    # -- identity ----------------------------------------------------------

    def get_serial_number(self) -> str:
        """The camera's serial number string."""
        return self._transport.command("sernum")

    def get_model(self) -> str:
        """The camera's model string, e.g. ``"CS165CU"``."""
        return self._transport.command("model")

    def get_firmware_version(self) -> str:
        """The camera's firmware version string."""
        return self._transport.command("ver")

    def get_name(self) -> str:
        """The camera's user-assigned display name (set via ThorCam; read-only here)."""
        return self._transport.command("name")

    # -- exposure and timing -----------------------------------------------

    def get_exposure_us(self) -> int:
        """Exposure in microseconds."""
        return int(self._transport.command("exp"))

    def set_exposure_us(self, exposure_us: int) -> int:
        """Set exposure in microseconds; returns the value the camera actually applied."""
        return int(self._transport.command(f"exp {int(exposure_us)}"))

    def get_frame_time_us(self) -> int:
        """Actual frame period in microseconds (read-only; follows exposure and ROI)."""
        return int(self._transport.command("frametime"))

    def get_frame_rate_control(self) -> tuple[bool, int]:
        """Frame-rate control as (enabled, frame period in us).

        ``sfr`` answers "<on|off> <period>". While it is off the period is the shortest the
        current ROI allows and setting one is refused with "Command not enabled", so the
        camera must be switched on first.
        """
        state, _, period = self._transport.command("sfr").partition(" ")
        return state == "on", int(period)

    def set_frame_rate_control(self, enabled: bool) -> None:
        """Enable or disable frame-rate control (``sfr``)."""
        self._transport.command(f"sfr {'on' if enabled else 'off'}")

    def set_frame_period_us(self, period_us: int) -> int:
        """Set the frame period in microseconds; frame-rate control must be on."""
        return int(self._transport.command(f"sfr {int(period_us)}"))

    def get_sensor_readout_time_ns(self) -> int:
        """Sensor readout time.

        Named ``_ns`` to match the vendor SDK's own ``sensor_readout_time_ns`` property,
        which passes ``tl_camera_get_sensor_readout_time``'s value through unmodified -- but
        no recorded capture annotates a ``sensor_readout_time`` read, so that unit is
        inherited from the vendor's naming convention, not independently confirmed against
        this firmware. See protocol.md's open questions.
        """
        return int(self._transport.command("sensor_readout_time"))

    # -- gain and level ----------------------------------------------------

    def get_gain(self) -> int:
        """Raw gain, in the camera's own units -- see get_gain_db()."""
        return int(self._transport.command("gain"))

    def set_gain(self, gain: int) -> int:
        """Set raw gain in the camera's own units; returns the value actually applied."""
        return int(self._transport.command(f"gain {int(gain)}"))

    def get_gain_table(self) -> list[int] | None:
        """Discrete gain steps in 0.1 dB, or None if gain is continuous.

        The two families differ here. The CS-series exposes a handful of discrete gains and
        ``gain`` is an *index* into this table (CS135MU: ``-75 -56 0 53``, so ``gain 3`` is
        5.3 dB). The Zelux has no table and ``gain`` is directly in 0.1 dB units, 0..480.
        """
        try:
            return [int(step) for step in self._transport.command("gain.table").split()]
        except CommandError:
            return None

    def get_gain_db(self) -> float:
        """Gain in dB, resolving the table indirection on cameras that have one."""
        raw = self.get_gain()
        table = self.get_gain_table()
        if not table:
            return raw / 10.0
        if not 0 <= raw < len(table):
            raise CommandError(
                "gain",
                None,
                f"gain index {raw} outside its {len(table)}-entry gain.table",
            )
        return table[raw] / 10.0

    def get_black_level(self) -> int:
        """Black-level offset, in the camera's own units."""
        return int(self._transport.command("blklvl"))

    def set_black_level(self, level: int) -> int:
        """Set the black-level offset; returns the value actually applied."""
        return int(self._transport.command(f"blklvl {int(level)}"))

    # -- geometry ----------------------------------------------------------

    def get_roi(self) -> tuple[int, int, int, int]:
        """ROI as (x0, y0, x1, y1) in sensor pixels, both corners inclusive."""
        x0, y0, x1, y1 = (int(v) for v in self._transport.command("roi").split())
        return x0, y0, x1, y1

    def set_roi(self, x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
        """Set the ROI; returns what the camera snapped it to.

        Both corners are inclusive, so a 772-wide ROI at x=100 is ``100 .. 871``. The camera
        quantises the width (to a multiple of 16 on the CS135MU, and it may move *both*
        edges to do it) and trims the height to fit the current binning, so the returned box
        is regularly not the one that was asked for. Use the return value.
        """
        response = self._transport.command(
            f"roi {int(x0)} {int(y0)} {int(x1)} {int(y1)}"
        )
        a, b, c, d = (int(v) for v in response.split())
        return a, b, c, d

    def get_roi_range(
        self,
    ) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
        """(min box, max box) for set_roi(), each an (x0, y0, x1, y1) inclusive box.

        Unlike limits(), which assumes a single numeric value, ``roi.min``/``roi.max`` each
        answer with all four corner values at once -- e.g. ``roi.min`` reads ``"0 0 15 1"``
        on a CS135MU (minimum ROI 16x2, agreeing with its 16-pixel width quantisation).
        """
        x0, y0, x1, y1 = (
            int(v) for v in self._transport.command("roi" + MIN_SUFFIX).split()
        )
        lo = (x0, y0, x1, y1)
        x0, y0, x1, y1 = (
            int(v) for v in self._transport.command("roi" + MAX_SUFFIX).split()
        )
        hi = (x0, y0, x1, y1)
        return lo, hi

    def get_binx(self) -> int:
        """Horizontal binning factor; 1 on cameras without on-camera binning.

        Only the CS-series bins in hardware. The Zelux has no ``hbin`` at all -- ThorCam
        bins Zelux images on the host -- so this returns 1 there rather than raising.
        """
        try:
            return int(self._transport.command("hbin"))
        except CommandError:
            return 1

    def set_binx(self, horizontal: int) -> int:
        """Set horizontal binning; returns the value actually applied. Set this *before*
        the ROI: the camera trims the ROI to a whole multiple of the bin factor."""
        return int(self._transport.command(f"hbin {int(horizontal)}"))

    def get_biny(self) -> int:
        """Vertical binning factor; 1 on cameras without on-camera binning. See get_binx()."""
        try:
            return int(self._transport.command("vbin"))
        except CommandError:
            return 1

    def set_biny(self, vertical: int) -> int:
        """Set vertical binning; returns the value actually applied. Set this *before*
        the ROI: the camera trims the ROI to a whole multiple of the bin factor (1024 rows
        at vbin 5 becomes 1020)."""
        return int(self._transport.command(f"vbin {int(vertical)}"))

    def get_binning(self) -> tuple[int, int]:
        """(horizontal, vertical) binning; (1, 1) on cameras without on-camera binning."""
        return self.get_binx(), self.get_biny()

    def set_binning(self, horizontal: int, vertical: int) -> tuple[int, int]:
        """Set binning. Set this *before* the ROI: the camera trims the ROI to a whole
        multiple of the bin factor (1024 rows at vbin 5 becomes 1020)."""
        return self.set_binx(horizontal), self.set_biny(vertical)

    def get_image_size(self) -> tuple[int, int]:
        """Output image size in pixels, after ROI and binning -- the size of the array a
        frame decodes to, and the authoritative value to read back after any geometry
        change."""
        return (
            int(self._transport.command("image_width")),
            int(self._transport.command("image_height")),
        )

    def get_pixel_bit_depth(self) -> int:
        """Bits per pixel the sensor reports (10 on both families)."""
        return int(self._transport.command("pixel_bit_depth"))

    # -- streaming ---------------------------------------------------------

    def get_frames_per_trigger(self) -> int:
        """Frames per trigger; 0 means continuous."""
        return int(self._transport.command("nframes"))

    def set_frames_per_trigger(self, count: int) -> int:
        return int(self._transport.command(f"nframes {int(count)}"))

    def arm(self) -> None:
        """Prepare the camera to stream; must precede ``start()``."""
        self._transport.command("arm")

    def start(self) -> None:
        """Begin streaming frames on an armed camera."""
        self._transport.command("start")

    def stop(self) -> None:
        """Stop streaming; the camera stays armed."""
        self._transport.command("stop")

    def disarm(self) -> None:
        """Release the camera from the armed state."""
        self._transport.command("disarm")

    # -- frame decoding inputs ---------------------------------------------

    def get_timestamp_clock_hz(self) -> int:
        """Tick rate of the frame-header timestamp (``pixel_clock_count_base``).

        Wildly different between families -- 1 kHz on the Zelux, 72 MHz on the CS135MU --
        so it must be read from the camera, never assumed.
        """
        return int(self._transport.command("pixel_clock_count_base"))

    def get_calibration_lines(self) -> int:
        """Extra sensor rows shipped with each frame, beyond the image itself.

        These count toward the frame's size on the wire. Cameras without the command send
        none.
        """
        try:
            return int(self._transport.command("num_calibration_lines"))
        except CommandError:
            return 0

    # -- color ---------------------------------------------------------------

    def get_color_mode(self) -> str:
        """The camera's sensor color type: ``"bayer"`` on a color body (e.g. CS165CU),
        ``"none"`` on a mono one (e.g. CS135MU, CS165MU)."""
        return self._transport.command("color")

    def is_color(self) -> bool:
        """Whether the sensor is a Bayer color sensor."""
        return self.get_color_mode() == "bayer"

    def get_color_filter_phase(self) -> str | None:
        """Which Bayer color sits at the origin pixel of the full frame, or None on a
        mono camera (the command errors there rather than answering "none").

        See ``frames.BAYER_PATTERNS`` for how this maps to a 2x2 tile.
        """
        try:
            return self._transport.command("color_filter_phase")
        except CommandError:
            return None
