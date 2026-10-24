"""
End-to-end acquisition through the full app, using the `simulated` camera backend
(no vendor hardware/SDK required), with the `filter` and `trigger_save` plugins enabled.

Runs the app in a fresh process (see app_probe.py and the run_app_probe fixture):
pylablib's thread controller registry and Qt's application object are per-process state,
so a full app inside the pytest process would leak into every later test.
"""


def test_full_pipeline_acquires_frames_with_plugins(tmp_path, settings_test_path, run_app_probe):
    config_lines = settings_test_path.read_text().splitlines()
    results = run_app_probe(tmp_path, config_lines, "sim1", seconds=2.0)
    assert "error" not in results, results
    assert results["frames_acquired"] > 0
    running = results["running_threads"]
    assert "plugin.filter.filt" in running
    assert "plugin.filter.filt.filter_thread" in running
    assert "plugin.trigger_save.trigsave" in running
