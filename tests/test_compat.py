"""Compatibility shims for third-party libraries (see camcontrol/_compat.py)."""

import numpy as np
from pylablib.core.utils import string as string_utils

from camcontrol import _compat


def test_settings_serializer_handles_0d_arrays():
    _compat.apply()
    assert string_utils.to_string(np.array(5)) == "5"
    assert string_utils.to_string(np.array(2.5)) == string_utils.to_string(2.5)
