"""12-bit packing kernels used by the frame saver: the numba kernel must match the numpy path."""

import numpy as np
import pytest

from camcontrol.services.framestream import u16to12


@pytest.mark.parametrize("width", [1, 2, 3, 4, 5, 6, 7, 8, 9, 96])
def test_u16to12_numba_matches_numpy(width):
    rng = np.random.default_rng(width)
    frame = rng.integers(0, 4096, size=(7, width), dtype=np.uint16).astype("<u2")
    plain = u16to12(frame, use_nb=False)
    jit = u16to12(frame, use_nb=True)
    assert plain.dtype == jit.dtype == np.uint8
    assert plain.shape == jit.shape == (7, (width // 2) * 3 + (width % 2) * 2)
    assert np.array_equal(plain, jit)


def test_u16to12_packing_layout():
    # two 12-bit pixels 0xABC, 0xDEF -> bytes BC, FA, DE
    packed = u16to12(np.array([[0xABC, 0xDEF]], dtype="<u2"), use_nb=False)
    assert packed.tolist() == [[0xBC, 0xFA, 0xDE]]
