"""Runtime compatibility shims for third-party libraries.

Applied from the entry points' ``main()`` functions rather than at import time, so that
``import camcontrol`` stays free of side effects.
"""

import numpy as np


def apply():
    """Apply all compatibility shims (idempotent)."""
    # pylablib 1.4.5's ``core.utils.string.to_string`` (used to write settings and defaults
    # files) still calls ``np.asscalar`` for 0-d arrays; numpy removed it in 1.23.
    if not hasattr(np, "asscalar"):
        np.asscalar = lambda a: a.item()
