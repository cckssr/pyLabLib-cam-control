"""Run the full app headless against one camera and print what happened as JSON.

Tests that need a full app instance run this in a fresh process: pylablib's thread
controller registry is global per process, so a second app instance in the pytest process
could see stale entries left over from the first one.

Usage: python app_probe.py CONFIG_FILE CAMERA_NAME [ACQUIRE_SECONDS]
"""

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pylablib.core.gui import QtCore
from pylablib.core.thread import controller, threadprop

from camcontrol import app as camapp

RESULT_PREFIX = "APP-PROBE-RESULT "


def main():
    config, camera = sys.argv[1], sys.argv[2]
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
    results = {}
    qapp = camapp.prepare_app()

    def probe():
        try:
            cam = controller.sync_controller("camera")
            results["connection"] = cam.v["status/connection"]
            cam.ca.acq_start()
            time.sleep(seconds)
            results["frames_acquired"] = cam.v["frames/acquired"]
            results["acquisition"] = cam.v["status/acquisition"]
            cam.ca.acq_stop()
            results["running_threads"] = sorted(controller._running_threads)
        except Exception as exc:
            results["error"] = repr(exc)
        finally:
            try:
                controller.get_gui_controller().stop()
            except threadprop.InterruptExceptionStop:
                pass

    QtCore.QTimer.singleShot(3000, probe)
    camapp.main(argv=["--config-file", config, "--camera", camera], app=qapp)
    sys.__stdout__.write(RESULT_PREFIX + json.dumps(results, default=str) + "\n")


if __name__ == "__main__":
    main()
