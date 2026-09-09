"""On-wire protocol for Thorlabs TSI scientific cameras (Zelux, Kiralux/Quantalux CS-series).

The transport, the command language and the frame framing are confirmed against live USB
captures of ThorCam driving a CS165CU (Zelux) and a CS135MU (CS-series). A few pixel-format
details are not -- ``frames.py`` marks them at the point of use. See ``protocol.md`` for the
evidence behind each fact and for what would settle the rest.

Both families speak the same transport and the same ASCII command language; they differ in
which commands they implement and in the header wrapped around each image frame.
"""

from __future__ import annotations

import re
import struct

VID = 0x1313  # Thorlabs vendor ID.

# The PID identifies the *family*, not the model -- both CS165MU and CS165CU enumerate as
# 0x4002. Use the `model`/`name` command to tell individual models apart.
PID_ZELUX = 0x4002  # CS165MU / CS165CU
PID_CS_SERIES = 0x4001  # CS135MU and the rest of the Kiralux/Quantalux CS line
KNOWN_PIDS = (PID_ZELUX, PID_CS_SERIES)

# Endpoint map, identical on both families. Endpoint 0x01 OUT is declared by the device but
# never used by ThorCam.
CMD_OUT_EP = 0x02  # "UART_TX_ENDPOINT" -- bulk, 1024-byte packets
CMD_IN_EP = 0x82  # "UART_RX_ENDPOINT" -- bulk, 1024-byte packets
DATA_IN_EP = 0x83  # "DATA_ENDPOINT" -- bulk, 1024-byte packets, image stream
EVENT_IN_EP = 0x81  # "EVENT_ENDPOINT" -- interrupt, 128 bytes, bInterval 1 (never used)

USB_TIMEOUT_MS = 3000

# Command and response frames are a fixed 1024 bytes: NUL-terminated ASCII, zero-padded.
# There is no CR/LF terminator, no length prefix, and no checksum on the wire.
COMMAND_FRAME_BYTES = 1024

# Image transfers are read in 16 KiB chunks; the tail of a frame is padded up to a whole
# chunk (with 0x0000ffff on the CS-series), so a reader must stop at the expected frame
# size rather than at the end of the last transfer.
DATA_CHUNK_BYTES = 16384


def encode_command_frame(command: str) -> bytes:
    """ASCII command -> the exact 1024-byte frame written to CMD_OUT_EP."""
    payload = command.encode("ascii")
    if len(payload) >= COMMAND_FRAME_BYTES:
        raise ValueError(
            f"command too long for a {COMMAND_FRAME_BYTES}-byte frame: {command!r}"
        )
    return payload.ljust(COMMAND_FRAME_BYTES, b"\x00")


def decode_command_frame(frame: bytes) -> str:
    """A 1024-byte command/response frame -> its ASCII payload, without padding."""
    return frame.split(b"\x00", 1)[0].decode("ascii", "replace")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

# Errors come back in-band as "error <n>,<text>". The numeric code is firmware-specific --
# "Command not enabled" is 18 on the Zelux and 21 on the CS135MU -- so match on the text,
# not the number. The vendor SDK reports these to its callers as 1000 + n.
ERROR_PREFIX = "error "
SDK_ERROR_BASE = 1000


class CommandError(RuntimeError):
    """A command the camera rejected, carrying its in-band error code and text."""

    def __init__(self, command: str, code: int | None, text: str) -> None:
        super().__init__(f"{command!r} failed: {text}")
        self.command = command
        self.code = code
        self.text = text

    @property
    def sdk_code(self) -> int | None:
        """The code the vendor SDK would have reported for this error."""
        return None if self.code is None else SDK_ERROR_BASE + self.code


_ERROR_RE = re.compile(r"^error (\d+),(.*)$", re.DOTALL)


def parse_response(command: str, response: str) -> str:
    """Return the response payload, or raise CommandError if the camera rejected it.

    Matches the full ``error <digits>,<text>`` shape rather than just the ``error`` prefix,
    and never treats the reply to the `error` command itself as a rejection -- `error` is a
    real command in both firmwares' `discover` output, and its normal reply would otherwise
    be raised instead of returned.
    """
    if command.split(" ")[0] == "error":
        return response

    match = _ERROR_RE.match(response)
    if match is None:
        return response

    raise CommandError(command, int(match.group(1)), match.group(2))


# ---------------------------------------------------------------------------
# Image frame headers
# ---------------------------------------------------------------------------

FRAME_MAGIC = b"\x90\x90\x00\x00"
CS_SIGNATURE = b"TSI\x00"

# CS-series metadata is a run of 8-byte chunks -- 4-char ASCII tag + little-endian uint32 --
# terminated by ENDT. The vocabulary below is not closed; unknown tags are passed through
# by parse_metadata() rather than dropped.
METADATA_CHUNK_BYTES = 8
TAG_FRAME_COUNT = b"FCNT"
TAG_PIXEL_CLOCK_HIGH = b"PCKH"
TAG_PIXEL_CLOCK_LOW = b"PCKL"
TAG_IMAGE_FORMAT = b"IFMT"
TAG_IMAGE_OFFSET = b"IOFF"
TAG_END = b"ENDT"

ZELUX_HEADER_BYTES = 16
ZELUX_OVERHEAD_BYTES = ZELUX_HEADER_BYTES  # nothing follows the pixel data

# Every DATA_CHUNK_BYTES boundary in the image stream carries a chunk marker instead of pixel
# data, and the marker's size differs per family -- getting this wrong silently shifts the
# whole pixel payload (see "Resolved since Phase 2" in protocol.md for how this was found).
#
# Both families start a chunk with FRAME_MAGIC's first two bytes, the chunk index mod 256 in
# the 3rd byte, and 0x00 in the 4th. Chunk 0 of a frame carries the real frame header; chunks
# 1..N carry only the marker.
#
# * Zelux -- 16 bytes: a byte-for-byte repeat of the whole 16-byte frame header (same
#   frame_count, same timestamp), differing only in the index byte.
# * CS-series -- 4 bytes: the magic and index only, with pixel data resuming immediately
#   after. The 60-byte tagged metadata header is NOT repeated.
ZELUX_CHUNK_MARKER_BYTES = 16
CS_CHUNK_MARKER_BYTES = 4


def chunk_marker_bytes(is_zelux: bool) -> int:
    """Size of the per-chunk continuation marker for this camera family."""
    return ZELUX_CHUNK_MARKER_BYTES if is_zelux else CS_CHUNK_MARKER_BYTES


def parse_chunk_marker(chunk: bytes) -> int | None:
    """Chunk index from a DATA_IN_EP chunk's marker, or None if this isn't a chunk start.

    The image stream is self-describing through these: index 0 starts a frame and every
    later chunk of that frame increments (mod 256). A reader that tracks them never has to
    predict a frame's byte count to stay aligned, which is what makes it immune to the
    camera truncating a frame when the host stops draining the endpoint.
    """
    if len(chunk) < 4 or chunk[0:2] != FRAME_MAGIC[0:2] or chunk[3] != 0:
        return None
    return chunk[2]


# The CS-series appends a 32-byte footer after the pixel data, bracketed by this sentinel,
# carrying the frame's height and width.
CS_FOOTER_SENTINEL = bytes.fromhex("4444333322221111")
CS_FOOTER_BYTES = 32

# Total non-pixel bytes in a CS-series frame: the metadata header (magic + "TSI\0" + 4
# reserved + 6 tagged 8-byte chunks, ENDT last) plus the footer.
CS_HEADER_BYTES = 60
CS_OVERHEAD_BYTES = CS_HEADER_BYTES + CS_FOOTER_BYTES
# Aliases kept for callers that still size a read from an explicit bound; the header length
# is pinned, so both bounds are the same value.
CS_MIN_OVERHEAD_BYTES = CS_OVERHEAD_BYTES
CS_MAX_OVERHEAD_BYTES = CS_OVERHEAD_BYTES


def parse_metadata(data: bytes) -> dict[bytes, int]:
    """Split a CS-series metadata block into {tag: value}, stopping at ENDT.

    Unknown tags are passed through rather than dropped -- the vocabulary is not closed
    (the camera's own ``tags`` command would enumerate it; see protocol.md).

    Raises ValueError if the block ends without an ENDT, which means it was cut short. That
    matters: silently returning the tags seen so far would drop PCKH/PCKL and turn a frame's
    timestamp into None with nothing to show that anything was lost.
    """
    tags: dict[bytes, int] = {}
    for offset in range(0, len(data) - METADATA_CHUNK_BYTES + 1, METADATA_CHUNK_BYTES):
        chunk = data[offset : offset + METADATA_CHUNK_BYTES]
        tag = chunk[0:4]
        if tag == TAG_END:
            return tags
        (value,) = struct.unpack("<I", chunk[4:8])
        tags[tag] = value
    raise ValueError(
        f"metadata block of {len(data)} bytes ends without an {TAG_END!r} tag"
    )


def parse_frame_header(head: bytes) -> dict | None:
    """Decode the header at the start of an image frame, or None if it isn't one.

    The two families use different headers behind a shared ``0x9090`` magic:

    * CS-series -- magic, ``"TSI\\0"``, 4 reserved bytes, then tagged metadata chunks.
    * Zelux     -- magic, frame count, timestamp, 4 reserved bytes. 16 bytes, no tags.

    ``timestamp`` is in ticks of the camera's ``pixel_clock_count_base`` (1 kHz on the
    Zelux, 72 MHz on the CS135MU), so converting it to seconds needs that value.

    Give this the whole start of the frame, not a tight slice: the metadata block's length
    is not fixed, so a short buffer can cut it off. Returns None -- rather than raising --
    for anything that isn't a frame header, since callers scanning a stream use that to tell
    aligned buffers from unaligned ones.
    """
    if len(head) < ZELUX_HEADER_BYTES or not head.startswith(FRAME_MAGIC):
        return None

    if head[4:8] == CS_SIGNATURE:
        try:
            tags = parse_metadata(head[12:])
        except ValueError:
            return None
        high = tags.get(TAG_PIXEL_CLOCK_HIGH)
        low = tags.get(TAG_PIXEL_CLOCK_LOW)
        return {
            "family": "cs",
            "frame_count": tags.get(TAG_FRAME_COUNT),
            "timestamp": None if high is None or low is None else (high << 32) | low,
            "image_format": tags.get(TAG_IMAGE_FORMAT),
            "image_offset": tags.get(TAG_IMAGE_OFFSET),
            "tags": {
                tag.decode("ascii", "replace"): value for tag, value in tags.items()
            },
        }

    frame_count, timestamp = struct.unpack("<II", head[4:12])
    return {
        "family": "zelux",
        "frame_count": frame_count,
        "timestamp": timestamp,
        "image_format": None,
        "image_offset": None,
        "tags": {},
    }


def parse_cs_footer(footer: bytes) -> dict | None:
    """Decode the CS-series 32-byte frame footer, or None if the sentinel doesn't match."""
    if len(footer) < CS_FOOTER_BYTES or not footer.startswith(CS_FOOTER_SENTINEL):
        return None
    if footer[24:32] != CS_FOOTER_SENTINEL:
        return None
    height, width = struct.unpack("<II", footer[16:24])
    return {"width": width, "height": height}


def timestamp_ns(ticks: int, clock_frequency_hz: int) -> float:
    """Frame timestamp ticks -> nanoseconds, given ``pixel_clock_count_base``."""
    return ticks / clock_frequency_hz * 1_000_000_000


# ---------------------------------------------------------------------------
# Command vocabulary
# ---------------------------------------------------------------------------

# The camera describes itself: `discover` lists its commands, and `<command>.disc` returns
# a JSON schema for one (desc, syntax, options, min/max/inc/default, flags, response).
# `<command>.min` / `.max` return the current limits directly, which is what ThorCam uses
# on every parameter change. Prefer querying these at runtime over hardcoding anything
# below -- limits move as ROI, binning and frame-rate control change.
DISCOVER_COMMAND = "discover"
SCHEMA_SUFFIX = ".disc"
MIN_SUFFIX = ".min"
MAX_SUFFIX = ".max"

# Observed in artifacts/tracelog-CS165CU.pcapng (Zelux) and -CS135MU.pcapng (CS-series).
# The union is what a driver may encounter; neither family implements all of it, and a
# command the firmware lacks answers "error <n>,Invalid command".
COMMON_COMMANDS = (
    "discover",
    "error",
    "echo",
    "id",
    "info",
    "ver",
    "ping",
    "model",
    "name",
    "sernum",
    "unlock",
    "camera_radix",
    "exp",
    "roi",
    "stop",
    "start",
    "arm",
    "disarm",
    "nframes",
    "state",
    "statusled",
    "image_dims",
    "image_width",
    "image_height",
    "pixel_bit_depth",
    "pixel_size",
    "pixel_width_nm",
    "pixel_height_nm",
    "pixel_byte_ordering",
    "vflip",
    "gain",
    "debug_radix",
    "time",
    "frametime",
    "color",
    "color_filter_phase",
    "ir_filter_type",
    "color_correction_matrix",
    "output_color_space",
    "default_white_balance_matrix",
    "sensor_readout_time",
    "sfr",
    "pixel_clock_count_base",
)

ZELUX_ONLY_COMMANDS = (
    "hflip",
    "blklvl",
    "data_packet_size",
    "comm_packet_size",
    "data_packets_per_frame",
    "num_calibration_lines",
)

CS_SERIES_ONLY_COMMANDS = (
    "hbin",
    "vbin",
    "hot_pixel_correction",
    "hwtrig",
    "temperature",
    "imgpack",
    "gpo",
    "tags",
    "nvstor",
)

# Writes non-volatile state or firmware. Never send without an explicit, understood reason.
# `prnu r` is excluded here: the capture shows ThorCam using it as a plain calibration-table
# *read* (`prnu r <plane> <offset> <count>`, hex args, float32 little-endian response).
DO_NOT_SEND_COMMANDS = (
    "nvstor wb",
    "nvstor erase",
    "ccload",
    "ccon",
    "ccoff",
    "prnu w",
)

# Commands that are safe in a driver but must not be swept blindly by a probe: `nvstor` and
# `unlock` reach the non-volatile store and the service lock (their no-argument behaviour is
# undocumented and untested), and the rest change acquisition state mid-probe. Their `.disc`
# schemas are still safe to read, which is the way to learn their syntax.
PROBE_SKIP_COMMANDS = frozenset(
    {"nvstor", "unlock", "arm", "disarm", "start", "stop", "prnu", "ccload"}
)


# ---------------------------------------------------------------------------
# Pixel data
# ---------------------------------------------------------------------------

# Both cameras report pixel_bit_depth 10 and stream 10 bits per pixel, packed 4 pixels into
# 5 bytes with no per-row padding: one row is exactly width * 10 // 8 bytes.
PIXEL_BITS = 10
PIXEL_GROUP_PIXELS = 4
PIXEL_GROUP_BYTES = 5


def row_stride_bytes(width: int, bits_per_pixel: int = PIXEL_BITS) -> int:
    """Bytes per image row on the wire."""
    return width * bits_per_pixel // 8
