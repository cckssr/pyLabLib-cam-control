"""Run the full app headless against one camera and print what happened as JSON.

Tests that need a full app instance run this in a fresh process: pylablib's thread
controller registry is global per process, so a second app instance in the pytest process
could see stale entries left over from the first one.

Usage: python app_probe.py CONFIG_FILE CAMERA_NAME [ACQUIRE_SECONDS]
"""

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("CAMCONTROL_USE_OPENGL", "0")  # no GL context offscreen

from pylablib.core.gui import QtCore, QtWidgets
from pylablib.core.thread import controller, threadprop

from camcontrol import app as camapp

RESULT_PREFIX = "APP-PROBE-RESULT "


def main():
    config, camera = sys.argv[1], sys.argv[2]
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
    results = {}
    qapp = camapp.prepare_app()

    def finish():
        try:
            controller.get_gui_controller().stop()
        except threadprop.InterruptExceptionStop:
            pass

    def guarded(func):
        def wrapped():
            try:
                func()
            except Exception as exc:
                results["error"] = repr(exc)
                finish()

        return wrapped

    # Timer callbacks rather than sleeping, so the GUI thread (and with it the frame
    # pipeline's GUI-side consumers) keeps running while frames are acquired
    @guarded
    def start():
        cam = controller.sync_controller("camera")
        results["connection"] = cam.v["status/connection"]
        window = next(
            w for w in QtWidgets.QApplication.topLevelWidgets() if type(w).__name__ == "MainWindow"
        )
        results["docks"] = {n: d.isVisible() for n, d in window.frame.docks.items()}
        results["menus"] = [a.text().replace("&", "") for a in window.menuBar().actions()]
        results["dock_state_restored"] = window.frame._dock_state_restored
        cam.ca.acq_start()
        QtCore.QTimer.singleShot(int(seconds * 1000), measure)

    @guarded
    def measure():
        cam = controller.sync_controller("camera")
        results["frames_acquired"] = cam.v["frames/acquired"]
        results["fps"] = cam.v["frames/fps"]
        results["acquisition"] = cam.v["status/acquisition"]
        results["connection_after"] = cam.v["status/connection"]
        results["device_error"] = cam.v["device_error"] if "device_error" in cam.v else None
        # device calls the camera thread runs as queued commands; with a failed camera, any
        # of these used to end the application
        cam.cs.update_parameters()
        cam.cs.apply_parameters({})
        cam.cs.get_full_info()
        cam.ca.acq_stop()
        results["running_threads"] = sorted(controller._running_threads)
        finish()

    QtCore.QTimer.singleShot(3000, start)
    camapp.main(argv=["--config-file", config, "--camera", camera], app=qapp)
    sys.__stdout__.write(RESULT_PREFIX + json.dumps(results, default=str) + "\n")


if __name__ == "__main__":
    main()
