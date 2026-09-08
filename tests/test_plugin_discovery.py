"""
Plugin and filter discovery.

Guards against the phase-3 fix: find_plugins()/find_filters() used to scan a
directory resolved relative to the launch CWD, which only happened to work
when running from the repo root. They now scan the package's own bundled
directory, plus an optional external directory for user-supplied additions.
"""

import os

from camcontrol.plugins.base import IPlugin, find_plugins
from camcontrol.plugins.filter import find_filters
from camcontrol.plugins.filters.base import IFrameFilter


def test_builtin_plugins_are_discovered():
    names = {p.get_class_name() for p in find_plugins()}
    assert names == {"filter", "server", "trigger_save"}
    for cls in find_plugins():
        assert issubclass(cls, IPlugin)


def test_builtin_filters_are_discovered():
    names = {f.get_class_name() for f in find_filters()}
    assert names == {
        "blur",
        "fft_bandpass",
        "moving_avg",
        "moving_acc",
        "moving_avg_sub",
        "time_map",
        "diff_matrix",
        "beam_profile",
    }
    for cls in find_filters():
        assert issubclass(cls, IFrameFilter)


def test_external_plugin_directory_is_scanned(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "my_plugin.py").write_text(
        "from camcontrol.plugins.base import IPlugin\n"
        "class MyExtPlugin(IPlugin):\n"
        "    _class_name = 'my_ext_plugin_test'\n"
    )
    names = {p.get_class_name() for p in find_plugins(extra_dir=str(plugin_dir))}
    assert "my_ext_plugin_test" in names
    assert names >= {"filter", "server", "trigger_save", "my_ext_plugin_test"}


def test_external_filter_directory_is_scanned(tmp_path):
    filters_dir = tmp_path / "filters"
    filters_dir.mkdir()
    (filters_dir / "my_filter.py").write_text(
        "from camcontrol.plugins.filters.base import ISingleFrameFilter\n"
        "class MyExtFilter(ISingleFrameFilter):\n"
        "    _class_name = 'my_ext_filter_test'\n"
        "    def process_frame(self, frame):\n"
        "        return frame\n"
    )
    names = {f.get_class_name() for f in find_filters(extra_dir=str(filters_dir))}
    assert "my_ext_filter_test" in names


def test_extra_dir_equal_to_builtin_dir_does_not_duplicate():
    builtin_dir = os.path.dirname(
        __import__("camcontrol.plugins.base", fromlist=["x"]).__file__
    )
    names = [p.get_class_name() for p in find_plugins(extra_dir=builtin_dir)]
    assert len(names) == len(set(names))
