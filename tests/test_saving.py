"""Frame file writers of FrameSaveThread (camcontrol.services.framestream), driven directly."""

import numpy as np
import pytest
import tifffile
from pylablib.misc.file_formats import cam

from camcontrol.services.framestream import FrameSaveThread, u16to12


class _SaverHarness:
    """Just enough state to drive FrameSaveThread's file writers without running the thread"""

    _write_frames = FrameSaveThread._write_frames
    _write_tiff = FrameSaveThread._write_tiff
    _write_finish = FrameSaveThread._write_finish
    _make_path = FrameSaveThread._make_path
    _clean_path = FrameSaveThread._clean_path
    build_path = staticmethod(FrameSaveThread.build_path)

    def __init__(self, path, fmt, filesplit=None, **format_parameters):
        self.v = {"saved": 0, "path": str(path), "path_kind": "pfx"}
        self.format = fmt
        self.filesplit = filesplit
        self.format_parameters = format_parameters
        self._file_idx = 0
        self._tiff_writer = None
        self.paths = []

    def _update_file_save(self, path=None):
        if path is not None and path not in self.paths:
            self.paths.append(path)

    def write(self, stacks):
        """Write a chunk of 3D frame stacks, as ``dump_queue`` does"""
        self._write_frames(stacks, append=self.v["saved"] > 0)
        self.v["saved"] += sum(len(s) for s in stacks)

    def finish(self):
        self._write_finish()


def _frames(n, shape=(6, 10), maxval=4096, dtype="<u2"):
    rng = np.random.default_rng(n)
    return rng.integers(0, maxval, size=(n, *shape)).astype(dtype)


def test_raw_roundtrip_and_last_frame(tmp_path):
    frames = _frames(5)
    saver = _SaverHarness(tmp_path / "f.bin", "raw")
    saver.write([frames[:2], frames[2:]])
    saver.write([frames])
    data = np.fromfile(saver.paths[0], dtype="<u2").reshape(-1, *frames.shape[1:])
    assert np.array_equal(data, np.concatenate([frames, frames]))
    assert saver._last_frame.shape == frames.shape[1:]


def test_raw_uint12(tmp_path):
    frames = _frames(3, shape=(4, 9))
    saver = _SaverHarness(tmp_path / "f.bin", "raw", dtype="uint12")
    saver.write([frames])
    expected = b"".join(u16to12(f, use_nb=False).tobytes() for f in frames)
    with open(saver.paths[0], "rb") as f:
        assert f.read() == expected


def test_raw_filesplit(tmp_path):
    frames = _frames(7)
    saver = _SaverHarness(tmp_path / "f.bin", "raw", filesplit=3)
    saver.write([frames[:4]])
    saver.write([frames[4:]])
    sizes = [len(np.fromfile(p, dtype="<u2")) // frames[0].size for p in saver.paths]
    assert sizes == [3, 3, 1]
    data = np.concatenate([np.fromfile(p, dtype="<u2") for p in saver.paths])
    assert np.array_equal(data.reshape(frames.shape), frames)


@pytest.mark.parametrize("fmt", ["tiff", "bigtiff"])
@pytest.mark.parametrize("filesplit", [None, 3])
def test_tiff_roundtrip(tmp_path, fmt, filesplit):
    frames = _frames(7)
    saver = _SaverHarness(tmp_path / "f.tiff", fmt, filesplit=filesplit)
    saver.write([frames[:4]])
    saver.write([frames[4:]])
    saver.finish()
    # one page per frame; the writer groups pages into several series, so read page by page
    read_back = []
    for p in saver.paths:
        with tifffile.TiffFile(p) as tif:
            read_back += [page.asarray() for page in tif.pages]
    assert len(saver.paths) == (1 if filesplit is None else 3)
    assert np.array_equal(np.array(read_back), frames)


def test_cam_format_records_2d_frame_shape(tmp_path):
    frames = _frames(4)
    saver = _SaverHarness(tmp_path / "f.cam", "cam")
    saver.write([frames])
    assert saver._last_frame.shape == frames.shape[1:]
    read_back = cam.load_cam(saver.paths[0])
    assert np.array_equal(np.array(read_back), frames)
