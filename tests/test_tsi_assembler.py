"""ChunkAssembler (thorlabs_tsi.stream) fed with the fake device's wire bytes."""

import numpy as np
import pytest

from camcontrol.thorlabs_tsi import frames
from camcontrol.thorlabs_tsi.fake import FakeTsiDevice
from camcontrol.thorlabs_tsi.stream import ChunkAssembler


def _setup(family="cs", sensor=None, roi=(0, 0, 511, 255)):
    dev = FakeTsiDevice(family=family, sensor=sensor)
    if roi is not None:
        dev.roi = dev._snap_roi(*roi)
    width, height = dev.image_size
    expected = frames.frame_chunks_on_wire(
        width, height, dev.spec.calibration_lines, is_zelux=family == "zelux"
    )
    assembler = ChunkAssembler(
        expected, frame_count_of=lambda d: frames.parse_frame_header(d[:64])["frame_count"]
    )
    return dev, assembler


def _frame(dev, n):
    return dev.wire_chunks(dev.render(n), n, n)


def _feed_in_pieces(assembler, data, piece=7001):
    out = []
    for i in range(0, len(data), piece):
        out += assembler.feed(data[i : i + piece])
    return out + assembler.flush_ready()


def _decode(dev, raw):
    width, height = dev.image_size
    return frames.decode_frame(
        raw.data, width, height, calibration_lines=dev.spec.calibration_lines
    )


@pytest.mark.parametrize("family", ["zelux", "cs"])
def test_clean_stream_in_odd_sized_reads(family):
    dev, assembler = _setup(family)
    data = b"".join(b"".join(_frame(dev, n)) for n in range(5))
    out = _feed_in_pieces(assembler, data)
    assert [f.frame_count for f in out] == [0, 1, 2, 3, 4]
    assert all(f.complete for f in out)
    for n, raw in enumerate(out):
        assert FakeTsiDevice.frame_count_from_image(_decode(dev, raw).image) == n


def test_dropped_chunk_discards_only_that_frame():
    dev, assembler = _setup()
    broken = _frame(dev, 1)
    del broken[len(broken) // 2]
    data = b"".join(_frame(dev, 0)) + b"".join(broken) + b"".join(_frame(dev, 2))
    out = [f for f in _feed_in_pieces(assembler, data) if f.complete]
    assert [f.frame_count for f in out] == [0, 2]
    assert assembler.stats.frames_truncated >= 1


def test_truncated_frame_is_reported_not_merged():
    dev, assembler = _setup()
    short = _frame(dev, 1)[:2]
    data = b"".join(_frame(dev, 0)) + b"".join(short) + b"".join(_frame(dev, 2))
    out = _feed_in_pieces(assembler, data)
    assert [(f.frame_count, f.complete) for f in out] == [(0, True), (1, False), (2, True)]


@pytest.mark.parametrize("junk", [100, 5000, 20000])
def test_realigns_after_junk_and_trailers(junk):
    dev, assembler = _setup()
    filler = bytes(range(256)) * (junk // 256) + bytes(junk % 256)
    data = b"".join(_frame(dev, 0)) + filler + b"".join(_frame(dev, 1)) + bytes(512)
    data += b"".join(_frame(dev, 2))
    out = [f for f in _feed_in_pieces(assembler, data) if f.complete]
    assert [f.frame_count for f in out] == [0, 1, 2]
    assert assembler.stats.bytes_resynced > 0


def test_frames_over_256_chunks():
    dev, assembler = _setup("cs", sensor=(2448, 2048), roi=None)
    data = b"".join(b"".join(_frame(dev, n)) for n in range(3))
    out = _feed_in_pieces(assembler, data, piece=1 << 20)
    assert [(f.frame_count, f.complete) for f in out] == [(0, True), (1, True), (2, True)]
    image = _decode(dev, out[1]).image
    assert image.shape == (2048, 2448)
    assert np.array_equal(image, dev.render(1))


def test_frames_over_256_chunks_without_geometry_hint():
    # expected_chunks too small (e.g. a stale prediction): the wire must still win
    dev, _ = _setup("cs", sensor=(2448, 2048), roi=None)
    assembler = ChunkAssembler(
        100, frame_count_of=lambda d: frames.parse_frame_header(d[:64])["frame_count"]
    )
    data = b"".join(b"".join(_frame(dev, n)) for n in range(3))
    out = [f for f in _feed_in_pieces(assembler, data, piece=1 << 20) if f.complete]
    # the first frame is what teaches the assembler the real length; later ones decode
    assert [f.frame_count for f in out][-2:] == [1, 2]
    assert np.array_equal(_decode(dev, out[-1]).image, dev.render(2))
