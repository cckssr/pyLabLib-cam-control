"""Frame reassembly and pixel unpacking for the streamed image data.

Headers, footer, frame sizes, the 10-bit row stride and the pixel bit layout are all
confirmed against live captures and live hardware; see protocol.md for the evidence.

The camera embeds a continuation marker at the start of every ``DATA_CHUNK_BYTES`` (16 KiB)
boundary after the first, and it must be stripped before the buffer is treated as pixel data.
Its size is family-specific -- 16 bytes on the Zelux, 4 on the CS-series; see
``protocol.CS_CHUNK_MARKER_BYTES``.

Pixel layout is 10-bit, and the bit *grouping* (4 pixels packed into 5 bytes, no per-row
padding) is shared by both families -- but the byte order within a group is not: it is
``lsb_last`` on the Zelux/CS165CU family and ``contiguous`` on the CS-series (CS135MU). See
``unpack_pixels`` and ``decode_frame``, which picks the right one from the frame header's own
family tag (``ORDER_BY_FAMILY``) so callers do not have to know which camera they are
talking to.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .protocol import (
    CS_CHUNK_MARKER_BYTES,
    CS_FOOTER_BYTES,
    CS_FOOTER_SENTINEL,
    CS_HEADER_BYTES,
    DATA_CHUNK_BYTES,
    PIXEL_GROUP_BYTES,
    PIXEL_GROUP_PIXELS,
    ZELUX_CHUNK_MARKER_BYTES,
    ZELUX_HEADER_BYTES,
    parse_cs_footer,
    parse_frame_header,
    row_stride_bytes,
    timestamp_ns,
)


class FrameAlignmentError(ValueError):
    """The buffer handed to split_frame() does not start on a frame boundary.

    Distinct from the other ValueErrors raised here (bad width, unknown pixel order): those
    are configuration mistakes that will recur identically on retry, while this one means the
    caller's read landed off a frame -- exactly the condition transport.StreamDesyncError
    exists to signal, so callers should treat the two the same way.
    """


@dataclass
class Frame:
    """One decoded image frame."""

    image: np.ndarray
    frame_count: int | None
    timestamp_ns: float | None


def frame_bytes_on_wire(
    width: int, height: int, calibration_lines: int = 0, overhead_bytes: int = 0
) -> int:
    """Total bytes the camera sends for one frame, rounded up to a whole 16 KiB chunk.

    ``calibration_lines`` is the camera's ``num_calibration_lines`` -- extra rows the sensor
    ships alongside the image. Including them is what makes the predicted size match the
    captured single-frame transfers exactly (e.g. the Zelux at 1440x1080 with 6 calibration
    lines: 120 chunks, as recorded).

    ``overhead_bytes`` is every non-pixel byte in the frame: the header, plus the footer on
    the CS-series. Use protocol.ZELUX_OVERHEAD_BYTES / protocol.CS_OVERHEAD_BYTES.
    """
    payload = overhead_bytes + row_stride_bytes(width) * (height + calibration_lines)
    chunks = -(-payload // DATA_CHUNK_BYTES)  # ceil
    return chunks * DATA_CHUNK_BYTES


def unpack_pixels(
    payload: bytes, width: int, height: int, order: str = "contiguous"
) -> np.ndarray:
    """Unpack 10-bit packed pixel data into a (height, width) uint16 array.

    Four pixels occupy five bytes and rows are exactly ``width * 10 // 8`` bytes with no
    padding, so a row's groups never straddle a row boundary for the widths the cameras
    actually produce. That grouping is shared by both camera families; the byte order
    *within* a group is not, and differs by family -- see ``decode_frame``, which picks the
    right one automatically from the frame header rather than making the caller guess.
    Passing ``order`` here (or through ``decode_frame``) overrides that, for diagnostics.

    ``order`` selects the byte layout within each 5-byte group, each settled live by an
    exposure sweep -- see protocol.md's "Pixel packing" sections for the methodology and
    measured data behind each:

    * ``contiguous`` -- no grouping; pixel *k* is bits [10k, 10k+10) of the row, LSB-first.
      Confirmed correct for the CS-series (CS135MU).
    * ``lsb_last`` -- four high bytes then a byte of packed low bits (the usual FX3 layout).
      Confirmed correct for the Zelux/CS165CU family. Unconfirmed on the CS165MU (same PID
      family, no unit tested live), but there is no reason to expect it differs.
    * ``lsb_first`` -- the byte of packed low bits leads the group. Not observed correct on
      either family; kept only so both sweeps stay reproducible against it.

    Calibration lines, if the camera sends any, are assumed to *lead* the image, so the last
    ``height`` rows of ``payload`` are returned and any extra rows at the front are dropped --
    confirmed live on a CS165CU (protocol.md, "Resolved since Phase 2").
    """
    if width % PIXEL_GROUP_PIXELS:
        # A partial group at the end of a row would need bit-level carry between rows, and
        # row_stride_bytes() floor-divides, so both the stride and the reshape below would be
        # wrong. Reachable in practice: CS-series `hbin 3` gives image_width 426.
        raise ValueError(
            f"width {width} is not a multiple of {PIXEL_GROUP_PIXELS}; "
            "row packing for such widths has not been reverse engineered (see protocol.md)"
        )

    stride = row_stride_bytes(width)
    needed = stride * height
    if len(payload) < needed:
        raise ValueError(
            f"need {needed} bytes for {width}x{height}, got {len(payload)}"
        )

    # Take the *last* `needed` bytes: any extra rows in a longer payload are leading
    # calibration lines, not trailing ones (see the docstring above).
    rows = np.frombuffer(payload[len(payload) - needed :], dtype=np.uint8).reshape(
        height, stride
    )

    if order == "contiguous":
        # Five bytes hold four pixels, LSB-first, with no padding. Doing this with
        # np.unpackbits and a bit-weight sum is the obvious transcription of that sentence
        # but costs ~13.6 ms per 1280x1024 frame -- slower than the camera produces frames,
        # so the decode itself becomes the reason frames are dropped. The explicit
        # shift-and-mask below is bit-for-bit identical and ~1.4 ms, which keeps a full-rate
        # stream decodable on one thread.
        groups = rows.reshape(height, -1, PIXEL_GROUP_BYTES).astype(np.uint16)
        b0, b1, b2, b3, b4 = (groups[:, :, i] for i in range(PIXEL_GROUP_BYTES))
        pixels = np.empty(
            (height, groups.shape[1], PIXEL_GROUP_PIXELS), dtype=np.uint16
        )
        pixels[:, :, 0] = b0 | ((b1 & 0x03) << 8)
        pixels[:, :, 1] = (b1 >> 2) | ((b2 & 0x0F) << 6)
        pixels[:, :, 2] = (b2 >> 4) | ((b3 & 0x3F) << 4)
        pixels[:, :, 3] = (b3 >> 6) | (b4 << 2)
        return pixels.reshape(height, -1)[:, :width]

    groups = rows.reshape(height, -1, 5).astype(np.uint16)
    pixels = np.empty((height, groups.shape[1], 4), dtype=np.uint16)
    if order == "lsb_last":
        high, low = groups[:, :, :4], groups[:, :, 4]
    elif order == "lsb_first":
        high, low = groups[:, :, 1:], groups[:, :, 0]
    else:
        raise ValueError(f"unknown pixel order {order!r}")

    for i in range(4):
        pixels[:, :, i] = (high[:, :, i] << 2) | ((low >> (2 * i)) & 0x3)

    return pixels.reshape(height, -1)[:, :width]


# Maps the firmware's `color_filter_phase` string (the color of the pixel at the origin of
# the full frame) to the 2x2 Bayer tile, read top-left, top-right, bottom-left, bottom-right.
# Only "red" is confirmed live (on a CS165CU); the other three follow from
# vendor/python_reference/tl_color_enums.py's FILTER_ARRAY_PHASE diagram but are unconfirmed
# against hardware -- see protocol.md's "Color" section.
BAYER_PATTERNS = {
    "red": "RGGB",
    "blue": "BGGR",
    "green_left_of_red": "GRBG",
    "green_left_of_blue": "GBRG",
}


def debayer_bilinear(mosaic: np.ndarray, phase: str) -> np.ndarray:
    """Demosaic a single-channel Bayer mosaic into an (height, width, 3) uint16 RGB image.

    ``phase`` is the camera's own ``color_filter_phase`` string (see
    ``commands.get_color_filter_phase``); it picks the 2x2 tile via ``BAYER_PATTERNS``.

    Standard bilinear demosaic -- cheap and adequate for a live preview, not the edge-aware
    algorithm ThorCam's own color pipeline uses, so output will look softer at sharp edges.
    Each channel keeps its own real samples and fills the rest from same-color neighbors:
    green (present at 2 of every 4 tile cells) fills from the 4 orthogonal neighbors, which
    always land on its other two cells; red and blue (present at 1 of every 4 cells each)
    fill a same-row cell from its horizontal neighbors, a same-column cell from its vertical
    neighbors, and the remaining (diagonal) cell -- which holds the other of red/blue -- from
    its 4 diagonal neighbors. Which rule applies at a given cell falls out of comparing that
    cell's (row parity, column parity) to the channel's own tile position, so the same code
    handles all four ``BAYER_PATTERNS`` entries without special-casing which one it is.

    Border pixels are filled by reflecting one step back into the mosaic (``np.pad`` with
    mode ``"reflect"``) rather than replicating the literal edge pixel (mode ``"edge"``):
    since the Bayer tile repeats with period 2, reflecting by 1 lands back on a same-color
    sample two cells in, while replicating the edge pixel would as often as not copy in a
    different color and tint the border.
    """
    try:
        pattern = BAYER_PATTERNS[phase]
    except KeyError:
        raise ValueError(
            f"unknown color_filter_phase {phase!r}; expected one of {sorted(BAYER_PATTERNS)}"
        ) from None

    height, width = mosaic.shape
    if height % 2 or width % 2:
        raise ValueError(
            f"debayer_bilinear needs even dimensions, got {width}x{height}"
        )

    # Color at each 2x2 tile cell, keyed by (row parity, col parity).
    tile = {
        (0, 0): pattern[0],
        (0, 1): pattern[1],
        (1, 0): pattern[2],
        (1, 1): pattern[3],
    }

    m = mosaic.astype(np.float32)
    padded = np.pad(m, 1, mode="reflect")

    def shift(dr: int, dc: int) -> np.ndarray:
        return padded[1 + dr : 1 + dr + height, 1 + dc : 1 + dc + width]

    horizontal = (shift(0, -1) + shift(0, 1)) / 2.0
    vertical = (shift(-1, 0) + shift(1, 0)) / 2.0
    diagonal = (shift(-1, -1) + shift(-1, 1) + shift(1, -1) + shift(1, 1)) / 4.0
    orthogonal = (shift(-1, 0) + shift(1, 0) + shift(0, -1) + shift(0, 1)) / 4.0

    def mask_for(color: str) -> np.ndarray:
        mask = np.zeros((height, width), dtype=bool)
        for (rp, cp), c in tile.items():
            if c == color:
                mask[rp::2, cp::2] = True
        return mask

    def channel(color: str) -> np.ndarray:
        out = m.copy()
        if color == "G":
            missing = ~mask_for("G")
            out[missing] = orthogonal[missing]
            return out

        own_rp, own_cp = next((rp, cp) for (rp, cp), c in tile.items() if c == color)
        for (rp, cp), c in tile.items():
            if c == color:
                continue
            spot = np.zeros((height, width), dtype=bool)
            spot[rp::2, cp::2] = True
            if rp == own_rp:
                fill = horizontal
            elif cp == own_cp:
                fill = vertical
            else:
                fill = diagonal
            out[spot] = fill[spot]
        return out

    rgb = np.stack([channel("R"), channel("G"), channel("B")], axis=-1)
    return np.clip(rgb, 0, None).astype(np.uint16)


def detect_group_phase_drift(payload: bytes, width: int, height: int) -> dict:
    """Check a raw pixel payload for the drifting single-byte-per-group artifact.

    Looks at the raw group bytes directly, without unpacking pixels at all -- the artifact
    (one byte per ``PIXEL_GROUP_BYTES``-byte group carrying almost all the signal, and which
    byte position does so drifting by about one position every several rows) is independent
    of pixel order, since no relabeling of bits into pixels can move which raw byte carries
    it. See protocol.md's "Pixel packing" section for how this artifact was found and fixed.

    Each row's mean byte value is computed per group-position; a position only counts as that
    row's "dominant" one if it clears the rest by a wide margin relative to the row's own
    noise floor (an absolute margin, not just whichever of several near-equal, noisy means
    happens to be largest). The outlier can be a peak (signal against a near-zero floor, as on
    a dark frame) or a trough (the one position *not* pinned near the ceiling, as on a
    bright/saturated frame) -- checking only peaks misses the artifact on saturated captures.
    Returns each row's byte-position (mod ``PIXEL_GROUP_BYTES``, meaningful only where
    ``dominant`` is True), which of the dominant rows change position from the previous
    dominant row, and a ``has_drift`` verdict -- True if most rows have a dominant position
    *and* that position keeps changing, which is far more than sensor noise would produce on
    real image content.
    """
    stride = row_stride_bytes(width)
    needed = stride * height
    if len(payload) < needed:
        raise ValueError(
            f"need {needed} bytes for {width}x{height}, got {len(payload)}"
        )
    if stride % PIXEL_GROUP_BYTES:
        raise ValueError(
            f"row stride {stride} is not a multiple of {PIXEL_GROUP_BYTES}; "
            "can't group into pixel groups"
        )

    rows = np.frombuffer(payload[len(payload) - needed :], dtype=np.uint8).reshape(
        height, stride
    )
    groups = rows.reshape(
        height, stride // PIXEL_GROUP_BYTES, PIXEL_GROUP_BYTES
    ).astype(np.int32)
    n_groups = groups.shape[1]
    phase_means = groups.mean(axis=1)  # (height, PIXEL_GROUP_BYTES)
    # Standard error of each position's mean, from that position's own within-row spread --
    # an absolute noise floor, unlike comparing the gap to the row's total range (top - bottom
    # of the same 5 noisy means), which doesn't shrink as more groups/row average out the
    # noise and so flags ordinary noise as "dominant" regardless of width.
    standard_error = groups.std(axis=1) / np.sqrt(n_groups)
    typical_se = np.maximum(standard_error.mean(axis=1), 1e-6)

    sorted_means = np.sort(phase_means, axis=1)
    top1, top2 = sorted_means[:, -1], sorted_means[:, -2]
    bottom1, bottom2 = sorted_means[:, 0], sorted_means[:, 1]
    peak_gap = top1 - top2
    trough_gap = bottom2 - bottom1
    # 5 sigma on the gap between the outlier and its nearest neighbor is a comfortable margin
    # for 5-way order statistics under the null (no real per-position difference).
    dominant = np.maximum(peak_gap, trough_gap) > 5.0 * typical_se
    is_peak = peak_gap >= trough_gap
    phases = np.where(is_peak, phase_means.argmax(axis=1), phase_means.argmin(axis=1))

    dominant_phases = phases[dominant]
    changes = np.flatnonzero(np.diff(dominant_phases)) + 1
    dominant_fraction = float(dominant.mean())
    change_fraction = len(changes) / max(len(dominant_phases) - 1, 1)

    return {
        "stride_bytes": stride,
        "phases": phases,
        "dominant": dominant,
        "dominant_fraction": dominant_fraction,
        "phase_changes": changes,
        "has_drift": bool(dominant_fraction > 0.5 and change_fraction > 0.05),
    }


def strip_chunk_markers(data: bytes, marker_bytes: int) -> bytes:
    """Remove the per-chunk continuation marker at every DATA_CHUNK_BYTES boundary.

    ``marker_bytes`` is family-specific and must be right: 16 on the Zelux, 4 on the
    CS-series (``protocol.chunk_marker_bytes()``) -- see ``protocol.CS_CHUNK_MARKER_BYTES``
    for why getting this wrong silently shifts the whole pixel payload.

    Idempotent on an already-short (single-chunk) buffer, which just returns unchanged.
    """
    if len(data) <= DATA_CHUNK_BYTES:
        return data
    pieces = [data[:DATA_CHUNK_BYTES]]
    for start in range(DATA_CHUNK_BYTES, len(data), DATA_CHUNK_BYTES):
        pieces.append(data[start + marker_bytes : start + DATA_CHUNK_BYTES])
    return b"".join(pieces)


def frame_chunks_on_wire(
    width: int,
    height: int,
    calibration_lines: int = 0,
    is_zelux: bool = False,
) -> int:
    """How many 16 KiB chunks the camera sends for one whole frame.

    This is the completeness test for a frame the chunk parser assembled -- not a read size.
    Nothing reads by byte count any more (``stream.ChunkAssembler`` follows chunk indices),
    which is what makes a truncated frame recoverable instead of permanently desynchronising
    everything after it.

    Every chunk after the first loses ``marker`` bytes to its continuation marker; the first
    chunk's marker is the frame header itself and is already counted in the overhead.
    """
    marker = ZELUX_CHUNK_MARKER_BYTES if is_zelux else CS_CHUNK_MARKER_BYTES
    overhead = ZELUX_HEADER_BYTES if is_zelux else CS_HEADER_BYTES + CS_FOOTER_BYTES
    content = overhead + row_stride_bytes(width) * (height + calibration_lines)
    # chunk 0 carries DATA_CHUNK_BYTES of content; each later chunk carries that minus its
    # marker.
    if content <= DATA_CHUNK_BYTES:
        return 1
    return 1 + -(-(content - DATA_CHUNK_BYTES) // (DATA_CHUNK_BYTES - marker))


def find_cs_footer(data: bytes) -> tuple[dict, int] | tuple[None, None]:
    """Locate and validate the CS-series footer in ``data``, or (None, None) if there isn't
    one yet.

    Factored out of split_frame() so camera.py's "has this frame's footer arrived yet" check
    (deciding whether to read more before decoding) can be exactly as strict as the final
    decode -- a naive ``CS_FOOTER_SENTINEL in data`` substring check is not strict enough:
    that 8-byte pattern can occur by coincidence within real pixel data, and unlike this
    function it doesn't validate what it finds via parse_cs_footer(). A false-positive match
    would make the caller believe an incomplete frame is already complete, permanently
    under-reading it (see protocol.md, "CS135MU still can't complete a capture").
    """
    marker = data.rfind(CS_FOOTER_SENTINEL)
    if marker == -1:
        return None, None
    for start in (marker - 24, marker):
        if start < 0:
            continue
        footer = parse_cs_footer(data[start : start + CS_FOOTER_BYTES])
        if footer is not None:
            return footer, start
    return None, None


def cs_frame_is_complete(data: bytes, calibration_lines: int = 0) -> bool:
    """Whether ``data`` already holds a whole CS-series frame -- a valid footer *and* enough
    preceding bytes for the pixel payload its own width/height declare.

    A syntactically valid footer is necessary but not sufficient: ``frame_bytes_on_wire()``'s
    fixed size (CS_MIN_OVERHEAD_BYTES) can genuinely be short of the true per-frame size, in
    which case the footer this finds is real, not a coincidence -- it has just arrived too
    early relative to the width/height it itself declares. Checking only "is there a valid
    footer" is not enough; see protocol.md, "CS135MU still can't complete a capture".
    """
    footer, end = find_cs_footer(data)
    if footer is None:
        return False
    needed = row_stride_bytes(footer["width"]) * (footer["height"] + calibration_lines)
    return end >= needed


def split_frame(
    data: bytes, width: int, height: int, calibration_lines: int = 0
) -> tuple[dict | None, bytes, dict | None]:
    """Split one frame's bytes into (header, pixel payload, footer).

    Handles both families: the Zelux prefixes a 16-byte header and sends nothing after the
    pixels, while the CS-series prefixes a tagged metadata header and appends a 32-byte
    footer carrying the frame's own dimensions. The image is anchored to the *end* of the
    pixel region (the footer) on the CS-series, since its header length is not fixed -- see
    protocol.md.

    Raises ValueError rather than guessing when an anchor is missing: a buffer that isn't
    frame-aligned would otherwise decode to a plausible-looking but shifted image.

    ``data`` must still be the *unstripped* wire buffer -- the header is parsed from its
    first 64 bytes (chunk 0 never carries a marker, so this is unaffected either way) before
    ``strip_chunk_markers()`` runs, so that a truncated or misaligned buffer that's shorter
    than one chunk is still detected the same way it always was. The header also tells this
    function which family's chunk-marker size to strip with, so callers don't have to.
    """
    header = parse_frame_header(data[:64])

    if header is None:
        raise FrameAlignmentError(
            "no frame header at the start of this buffer -- the image stream is not "
            "frame-aligned"
        )

    data = strip_chunk_markers(
        data,
        ZELUX_CHUNK_MARKER_BYTES
        if header["family"] == "zelux"
        else CS_CHUNK_MARKER_BYTES,
    )

    if header["family"] == "zelux":
        needed = row_stride_bytes(width) * (height + calibration_lines)
        return header, data[ZELUX_HEADER_BYTES : ZELUX_HEADER_BYTES + needed], None

    # CS-series: the pixel data is anchored to the footer. find_cs_footer() searches backwards --
    # the sentinel is an 8-byte pattern pixel data could in principle contain, but the frame's
    # trailing padding cannot, so the last occurrence is the reliable one. It appears twice per
    # footer (at +0 and +24), so if the match is the closing copy, step back to the opening one.
    footer, end = find_cs_footer(data)

    if footer is None:
        # Without the anchor the payload would be taken from the chunk padding and decode to
        # a silently shifted image, which is worse than not decoding at all.
        raise FrameAlignmentError(
            "CS-series frame has no footer sentinel -- cannot locate pixel data"
        )

    width, height = footer["width"], footer["height"]
    needed = row_stride_bytes(width) * (height + calibration_lines)
    if end < needed:
        raise FrameAlignmentError(
            f"frame holds {end} bytes before its footer, need {needed} for {width}x{height}"
        )

    return header, data[end - needed : end], footer


# The confirmed-correct unpack_pixels() byte order per frame-header family -- "zelux" covers
# the CS165CU (and, unconfirmed but assumed, the CS165MU); "cs" is the CS135MU and the rest
# of the CS-series. See unpack_pixels()'s docstring for the evidence behind each entry.
ORDER_BY_FAMILY = {"zelux": "lsb_last", "cs": "contiguous"}


def decode_frame(
    data: bytes,
    width: int,
    height: int,
    clock_frequency_hz: int | None = None,
    calibration_lines: int = 0,
    order: str | None = None,
) -> Frame:
    """Decode one complete frame's bytes into a Frame.

    ``clock_frequency_hz`` is the camera's ``pixel_clock_count_base``; without it the
    timestamp is left as None rather than being scaled by a guess.

    ``order`` picks the pixel byte layout (see ``unpack_pixels``); left as the default
    ``None``, it is chosen from the frame header's own family tag via ``ORDER_BY_FAMILY``,
    which is correct for a real camera and is what every caller should use. Pass it
    explicitly only for diagnostics (e.g. comparing orders against a raw dump).
    """
    header, payload, footer = split_frame(data, width, height, calibration_lines)
    if footer is not None:
        width, height = footer["width"], footer["height"]

    if order is None:
        family = None if header is None else header["family"]
        order = ORDER_BY_FAMILY.get(family, "contiguous")

    image = unpack_pixels(payload, width, height, order=order)

    stamp = None
    if header is not None and header["timestamp"] is not None and clock_frequency_hz:
        stamp = timestamp_ns(header["timestamp"], clock_frequency_hz)

    return Frame(
        image=image,
        frame_count=None if header is None else header["frame_count"],
        timestamp_ns=stamp,
    )
