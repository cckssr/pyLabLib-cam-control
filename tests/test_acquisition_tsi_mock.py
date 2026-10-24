"""Full app on the emulated Thorlabs TSI camera (ThorlabsTSIMock): driver, adapter and GUI.

Each case runs the app in a fresh process (see app_probe.py).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).with_name("app_probe.py")


def run_app_probe(tmp_path, config_lines, camera, seconds=2.0):
    config = tmp_path / "settings.cfg"
    config.write_text("\n".join(config_lines) + "\n")
    proc = subprocess.run(
        [sys.executable, str(PROBE), str(config), camera, str(seconds)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("APP-PROBE-RESULT "):
            return json.loads(line.split(" ", 1)[1])
    raise AssertionError(f"app probe produced no result:\n{proc.stdout}\n{proc.stderr}")


@pytest.mark.parametrize(
    "params",
    [["family\tzelux"], ["family\tcs", "color\tTrue"]],
    ids=["zelux", "cs-color"],
)
def test_app_streams_from_emulated_tsi_camera(tmp_path, params):
    lines = ["cameras/tsi/kind\tThorlabsTSIMock", 'cameras/tsi/display_name\t"Emulated TSI"']
    lines += [f"cameras/tsi/params/{p}" for p in params]
    results = run_app_probe(tmp_path, lines, "tsi")
    assert "error" not in results, results
    assert results["connection"] == "opened"
    assert results["frames_acquired"] > 0
