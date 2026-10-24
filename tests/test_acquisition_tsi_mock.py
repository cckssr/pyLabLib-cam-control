"""Full app on the emulated Thorlabs TSI camera (ThorlabsTSIMock): driver, adapter and GUI.

Each case runs the app in a fresh process (see app_probe.py and the run_app_probe fixture).
"""

import pytest


@pytest.mark.parametrize(
    "params",
    [["family\tzelux"], ["family\tcs", "color\tTrue"]],
    ids=["zelux", "cs-color"],
)
def test_app_streams_from_emulated_tsi_camera(tmp_path, params, run_app_probe):
    lines = ["cameras/tsi/kind\tThorlabsTSIMock", 'cameras/tsi/display_name\t"Emulated TSI"']
    lines += [f"cameras/tsi/params/{p}" for p in params]
    results = run_app_probe(tmp_path, lines, "tsi")
    assert "error" not in results, results
    assert results["connection"] == "opened"
    assert results["frames_acquired"] > 0


def test_unplugged_camera_disconnects_instead_of_closing_the_app(tmp_path, run_app_probe):
    lines = [
        "cameras/tsi/kind\tThorlabsTSIMock",
        "cameras/tsi/params/family\tzelux",
        "cameras/tsi/params/faults/disconnect_after_frames\t5",
    ]
    results = run_app_probe(tmp_path, lines, "tsi", seconds=3.0)
    assert "error" not in results, results  # the app (and the probe) kept running
    assert results["connection"] == "opened"
    assert results["connection_after"] == "closed"
    assert "No such device" in results["device_error"]
