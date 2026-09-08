"""TIFF/BigTIFF round-trip via imageio, as used by FrameSaveThread (camcontrol.services.framestream)."""

import imageio
import numpy as np
import pytest


@pytest.mark.parametrize("bigtiff", [False, True])
def test_tiff_write_read_roundtrip(tmp_path, bigtiff):
    path = tmp_path / ("big.tiff" if bigtiff else "std.tiff")
    rng = np.random.default_rng(0)
    frames = [rng.integers(0, 4096, size=(32, 32), dtype=np.uint16).astype("<u2") for _ in range(5)]

    writer = imageio.get_writer(str(path), format="tiff", bigtiff=bigtiff, mode="V")
    for frame in frames:
        writer.append_data(frame)
    writer.close()

    read_back = imageio.mimread(str(path))
    assert len(read_back) == len(frames)
    for original, read in zip(frames, read_back, strict=True):
        assert np.array_equal(original, read)
