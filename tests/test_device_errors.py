"""Camera device errors must not close the application (cameras/base.py's mixin)."""

import subprocess
import sys

# Run in a fresh process: it creates a (never started) thread controller, which would
# otherwise stay registered in the pytest process
_CHECK_BINDING = """
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from camcontrol.cameras.base import (
    _COMMANDS_BOUND_AT_INIT, DeviceErrorTolerantThreadMixin, make_device_errors_non_fatal)
from camcontrol.cameras.sim import SimulatedCameraThread
thread = make_device_errors_non_fatal(SimulatedCameraThread(name="cam", kwargs={}))
for name in _COMMANDS_BOUND_AT_INIT:
    bound = thread._commands[name].command.__func__ is getattr(DeviceErrorTolerantThreadMixin, name)
    print(name, bound, flush=True)
os._exit(0)  # skip Qt teardown of the unstarted thread
"""


def test_commands_registered_at_construction_use_the_mixin():
    # pylablib binds these commands in the thread's constructor; queued calls to them
    # (e.g. update_parameters right after a camera is unplugged) used to bypass the mixin,
    # and the resulting exception closed the application
    proc = subprocess.run(
        [sys.executable, "-c", _CHECK_BINDING], capture_output=True, text=True, timeout=120
    )
    lines = proc.stdout.split("\n")
    results = dict(line.split() for line in lines if line.strip())
    assert results and all(v == "True" for v in results.values()), (proc.stdout, proc.stderr)
