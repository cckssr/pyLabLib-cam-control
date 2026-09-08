"""12-bit packing kernels used by the frame saver, verified against Python 3.14 + numba 0.67+."""

import numpy as np

from camcontrol.services.framestream import u16to12, u16to12nb2d


def test_u16to12_kernels_agree():
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 4096, size=(64, 96), dtype=np.uint16).astype("<u2")
    plain = u16to12(frame)
    jit = u16to12nb2d(frame)
    assert plain.dtype == np.uint8
    assert jit.dtype == np.uint8
    assert plain.shape == jit.shape
    assert np.array_equal(plain, jit)
