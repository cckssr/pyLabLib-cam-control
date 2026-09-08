"""Shared pytest fixtures. QT_QPA_PLATFORM must be set before Qt is first imported."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

import pytest


@pytest.fixture
def settings_test_path(tmp_path, request):
    """A minimal hardware-free settings file (simulated camera + built-in plugins)."""
    repo_root = request.config.rootpath
    src = repo_root / "settings_test.cfg"
    dst = tmp_path / "settings_test.cfg"
    dst.write_text(src.read_text())
    return dst
