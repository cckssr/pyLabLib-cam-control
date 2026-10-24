"""Runtime patches for third-party libraries.

Applied from the entry points' ``main()`` functions rather than at import time, so that
``import camcontrol`` stays free of side effects.
"""

import numpy as np


def _patch_image_lim_preset():
    """Make pylablib's image-limits preset never hold ``None`` limits.

    ``ImagePlotterCtl`` seeds the preset from its initial ``(None, None)`` limits, and settings
    files written while that seed was in place store it; "Load preset" then assigns ``None``
    to the min/max fields and the exception closes the application. With this patch an unset
    or ``None`` preset reads as the current limits instead.
    """
    from pylablib.gui.widgets.plotters.image_plotter import ImagePlotterCtl

    if not isinstance(getattr(ImagePlotterCtl, "img_lim_preset", None), property):
        ImagePlotterCtl.img_lim_preset = property(_get_lim_preset, _set_lim_preset)


def _get_lim_preset(ctl):
    preset = ctl.__dict__.get("_img_lim_preset")
    if preset is None or any(v is None for v in preset):
        try:
            return ctl.v["minlim"], ctl.v["maxlim"]
        except (KeyError, AttributeError):
            return preset
    return preset


def _set_lim_preset(ctl, value):
    ctl.__dict__["_img_lim_preset"] = value


def apply():
    """Apply all patches (idempotent)."""
    # pylablib 1.4.5's ``core.utils.string.to_string`` (used to write settings and defaults
    # files) still calls ``np.asscalar`` for 0-d arrays; numpy removed it in 1.23.
    if not hasattr(np, "asscalar"):
        np.asscalar = lambda a: a.item()
    _patch_image_lim_preset()
