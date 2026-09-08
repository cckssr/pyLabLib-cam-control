"""Bundled resource files (icon, splash image, button icons)."""

import importlib.resources


def resource_path(name):
    """Return a filesystem path (str) to a bundled resource file, e.g. ``"icon.ico"``."""
    return str(importlib.resources.files(__name__).joinpath(name))
