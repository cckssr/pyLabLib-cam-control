"""Shared pytest fixtures. QT_QPA_PLATFORM must be set before Qt is first imported."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# offscreen Qt has no OpenGL context; pyqtgraph would fail to paint GL-accelerated plots
os.environ.setdefault("CAMCONTROL_USE_OPENGL", "0")

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def settings_test_path(tmp_path, request):
    """A minimal hardware-free settings file (simulated camera + built-in plugins)."""
    repo_root = request.config.rootpath
    src = repo_root / "settings_test.cfg"
    dst = tmp_path / "settings_test.cfg"
    dst.write_text(src.read_text())
    return dst


@pytest.fixture
def run_app_probe():
    """Run the full app in a fresh process via tests/app_probe.py; return its JSON result.

    Call as ``run_app_probe(directory, config_lines, camera_name, seconds=2.0)``; the
    directory is the app's working directory (settings, defaults and logs go there).
    """
    probe = Path(__file__).with_name("app_probe.py")

    def run(directory, config_lines, camera, seconds=2.0):
        config = directory / "settings.cfg"
        config.write_text("\n".join(config_lines) + "\n")
        proc = subprocess.run(
            [sys.executable, str(probe), str(config), camera, str(seconds)],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=120,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("APP-PROBE-RESULT "):
                return json.loads(line.split(" ", 1)[1])
        raise AssertionError(f"app probe produced no result:\n{proc.stdout}\n{proc.stderr}")

    return run
