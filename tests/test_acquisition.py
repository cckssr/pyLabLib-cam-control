"""
End-to-end acquisition through the full app, using the `simulated` camera backend
(no vendor hardware/SDK required). Must run with QT_QPA_PLATFORM=offscreen (set by
conftest.py if not already set in the environment).

This drives camcontrol.app the same way the real GUI does: build a QApplication,
schedule a probe on a QTimer, run the Qt event loop via app.main(), and have the
probe stop the GUI controller when done so the event loop returns. Kept as a single
test (rather than one test per assertion) since pylablib's thread controller
registry is global process state -- running two full app instances in the same
pytest session risks stale entries from incomplete teardown between tests.
"""

import time

from pylablib.core.gui import QtCore
from pylablib.core.thread import controller, threadprop

from camcontrol import app as camapp


def test_full_pipeline_acquires_frames_with_plugins(tmp_path, settings_test_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    results = {}
    app = camapp.prepare_app()

    def probe():
        try:
            results["running_threads"] = set(controller._running_threads.keys())
            cam = controller.sync_controller("camera")
            cam.ca.open()
            cam.ca.acq_start()
            time.sleep(2.0)
            results["frames_acquired"] = cam.v["frames/acquired"]
            cam.ca.acq_stop()
        finally:
            # stop() raises threadprop.InterruptExceptionStop by design, as its mechanism
            # for unwinding the GUI thread's event loop -- not a real error.
            try:
                controller.get_gui_controller().stop()
            except threadprop.InterruptExceptionStop:
                pass

    QtCore.QTimer.singleShot(4000, probe)
    camapp.main(
        argv=["--config-file", str(settings_test_path), "--camera", "sim1"], app=app
    )

    assert results["frames_acquired"] > 0
    running = results["running_threads"]
    assert "plugin.filter.filt" in running
    assert "plugin.filter.filt.filter_thread" in running
    assert "plugin.trigger_save.trigsave" in running
