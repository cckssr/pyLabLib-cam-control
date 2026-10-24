"""Compatibility shims for third-party libraries (see camcontrol/_compat.py)."""

import numpy as np
from pylablib.core.utils import string as string_utils

from camcontrol import _compat


def test_settings_serializer_handles_0d_arrays():
    _compat.apply()
    assert string_utils.to_string(np.array(5)) == "5"
    assert string_utils.to_string(np.array(2.5)) == string_utils.to_string(2.5)


def test_image_lim_preset_never_none():
    # Exercised on a stand-in object: constructing pylablib's plotter widgets inside the
    # pytest process segfaults at interpreter shutdown.
    class FakeCtl:
        img_lim_preset = property(_compat._get_lim_preset, _compat._set_lim_preset)

        def __init__(self):
            self.v = {"minlim": 10, "maxlim": 200}

    ctl = FakeCtl()
    assert ctl.img_lim_preset == (10, 200)  # never set
    ctl.img_lim_preset = (None, None)  # as stored by older versions
    assert ctl.img_lim_preset == (10, 200)
    ctl.img_lim_preset = (5, 50)
    assert ctl.img_lim_preset == (5, 50)


def test_image_lim_preset_patch_installs():
    from pylablib.gui.widgets.plotters.image_plotter import ImagePlotterCtl

    _compat.apply()
    assert isinstance(ImagePlotterCtl.img_lim_preset, property)
