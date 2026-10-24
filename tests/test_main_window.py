"""The dock-based main window, run as a full app in a fresh process (see app_probe.py)."""

SIM_CONFIG = ["cameras/sim1/kind\tsimulated", "cameras/sim1/params/size\t(320, 240)"]


def test_window_structure_and_layout_persistence(tmp_path, run_app_probe):
    first = run_app_probe(tmp_path, SIM_CONFIG, "sim1", seconds=1.0)
    assert "error" not in first, first
    assert first["menus"] == ["File", "Camera", "View", "Help"]
    visible = {name for name, shown in first["docks"].items() if shown}
    assert visible == {"camera", "processing", "image", "saving", "status"}  # no time series yet
    assert not first["dock_state_restored"]
    assert first["frames_acquired"] > 0
    assert "dock_state" in (tmp_path / "defaults.cfg").read_text()
    second = run_app_probe(tmp_path, SIM_CONFIG, "sim1", seconds=0.5)
    assert second["dock_state_restored"]
    assert (
        not (tmp_path / "logerr.txt").exists()
        or "Traceback" not in (tmp_path / "logerr.txt").read_text()
    )
