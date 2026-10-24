"""Thorlabs TSI wire format: the fake device's encoder against the driver's decoder."""

import numpy as np
import pytest

from camcontrol.thorlabs_tsi import frames
from camcontrol.thorlabs_tsi.fake import FakeTsiDevice, pack_contiguous, pack_lsb_last


@pytest.mark.parametrize(
    ("pack", "order"), [(pack_lsb_last, "lsb_last"), (pack_contiguous, "contiguous")]
)
def test_packers_invert_unpack(pack, order):
    rng = np.random.default_rng(1)
    pixels = rng.integers(0, 1024, size=(5, 24), dtype=np.uint16)
    assert np.array_equal(frames.unpack_pixels(pack(pixels), 24, 5, order=order), pixels)


@pytest.mark.parametrize("family", ["zelux", "cs"])
@pytest.mark.parametrize("color", [False, True])
def test_wire_frame_decodes(family, color):
    dev = FakeTsiDevice(family=family, color=color)
    dev.roi = dev._snap_roi(32, 16, 32 + 319, 16 + 199)
    width, height = dev.image_size
    image = dev.render(frame_count=7)
    chunks = dev.wire_chunks(image, frame_count=7, timestamp=1234)
    calib = dev.spec.calibration_lines
    assert len(chunks) == frames.frame_chunks_on_wire(
        width, height, calib, is_zelux=family == "zelux"
    )
    frame = frames.decode_frame(
        b"".join(chunks),
        width,
        height,
        clock_frequency_hz=dev.spec.clock_hz,
        calibration_lines=calib,
    )
    assert np.array_equal(frame.image, image)
    assert frame.frame_count == 7
    assert frame.timestamp_ns == pytest.approx(1234 / dev.spec.clock_hz * 1e9)
    assert FakeTsiDevice.frame_count_from_image(frame.image) == 7


def test_large_cs_frame_spans_more_than_256_chunks():
    dev = FakeTsiDevice(family="cs", sensor=(2448, 2048))
    width, height = dev.image_size
    image = dev.render(frame_count=3)
    chunks = dev.wire_chunks(image, frame_count=3, timestamp=0)
    assert len(chunks) > 256
    assert chunks[256][:4] == b"\x90\x90\x00\x00"  # the index byte has wrapped to 0
    frame = frames.decode_frame(b"".join(chunks), width, height)
    assert np.array_equal(frame.image, image)
