# Copyright (C) 2021  Alexey Shkarin

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import sys

if sys.platform == "win32":
    python_folder = os.path.split(os.path.abspath(sys.executable))[0]
    pywin32_folder = os.path.join(python_folder, "Lib", "site-packages", "pywin32_system32")
    os.environ["PATH"] = (
        pywin32_folder + os.pathsep + os.environ.get("PATH", "")
    )  # fix pywin32 confusion with some Anaconda installations

import pylablib
import pyqtgraph
from pylablib import widgets as pll_widgets
from pylablib.core.fileio import loadfile, savefile
from pylablib.core.gui import QtCore, QtGui, QtWidgets, Signal, qtkwargs
from pylablib.core.gui.widgets import container, param_table
from pylablib.core.thread import controller, threadprop
from pylablib.core.utils import (
    dictionary,
)
from pylablib.core.utils import (
    files as file_utils,
)
from pylablib.core.utils import (
    general as general_utils,
)

# OpenGL context creation can fail in headless/VM/remote-desktop environments; allow
# disabling it without a code change (e.g. `CAMCONTROL_USE_OPENGL=0`).
pyqtgraph.setConfigOptions(
    useOpenGL=os.environ.get("CAMCONTROL_USE_OPENGL", "1") != "0", antialias=False
)
try:
    pyqtgraph.setConfigOptions(useNumba=True)
except KeyError:
    pass

import argparse
import datetime
import subprocess
import threading
import traceback

try:
    import win32com.client

    win32com_present = True
except ImportError:
    win32com_present = False


from camcontrol import plugins, services, splash
from camcontrol.cameras.base import make_device_errors_non_fatal
from camcontrol.cameras.loader import camera_descriptors
from camcontrol.gui import (
    ActivityIndicator_ctl,
    DisplaySettings_ctl,
    FramePreprocess_ctl,
    FrameProcess_ctl,
    PlotControl_ctl,
    ProcessingIndicator_ctl,
    SaveBox_ctl,
    about,
    camera_control,
    color_theme,
    error_message,
    image_view,
    settings_editor,
)
from camcontrol.resources import resource_path
from camcontrol.services import dev as dev_services

### Redirecting console / errors to file logs ###
log_lock = threading.RLock()


class StreamLogger(general_utils.StreamFileLogger):
    def __init__(self, path, stream=None):
        super().__init__(path, stream=stream, lock=log_lock)
        self.start_time = datetime.datetime.now()

    def write_header(self, f):
        f.write("\n\n" + "-" * 50)
        f.write(
            f"\nStarting {os.path.split(sys.argv[0])[1]} {self.start_time:on %Y/%m/%d at %H:%M:%S}\n\n"
        )
        f.write(f"\nFirst message {datetime.datetime.now():on %Y/%m/%d at %H:%M:%S}\n\n")


def configure_logging():
    """Redirect stdout/stderr to log files in the current working directory."""
    sys.stderr = StreamLogger("logerr.txt", sys.stderr)
    sys.stdout = StreamLogger("logout.txt", sys.stdout)


from camcontrol import _compat, compare_version, version

_defaults_filename = "defaults.cfg"
_locals_filename = "locals.cfg"
_dev_utils_enabled = False

cam_thread = "camera"
process_thread = "frame_process"
preprocess_thread = "frame_preprocess"
slowdown_thread = "frame_slowdown"
channel_accumulator_thread = "channel_accumulator"
save_thread = "frame_save"
snap_save_thread = "frame_save_snap"
settings_manager_thread = "settings_manager"
event_hooks_manager_thread = "event_hooks_manager"
resource_manager_thread = "resource_manager"
garbage_collector_thread = "garbage_collector"

_ext_controller_names = {
    "camera": cam_thread,
    "processor": process_thread,
    "preprocessor": preprocess_thread,
    "saver": save_thread,
    "snap_saver": snap_save_thread,
    "slowdown": slowdown_thread,
    "channel_accumulator": channel_accumulator_thread,
    "settings_manager": settings_manager_thread,
    "event_hooks_manager": event_hooks_manager_thread,
    "resource_manager": resource_manager_thread,
}


### Main window ###
class MainWindow(QtWidgets.QMainWindow):
    """
    Top-level window.

    The :class:`StandaloneFrame` (which holds the GUI values tree and the image area) is its
    central widget; all control and status panels are dock widgets around it.
    """

    def __init__(self, frame):
        super().__init__()
        self.frame = frame
        self.setDockOptions(
            QtWidgets.QMainWindow.AnimatedDocks
            | QtWidgets.QMainWindow.AllowTabbedDocks
            | QtWidgets.QMainWindow.AllowNestedDocks
        )
        self.setTabPosition(QtCore.Qt.AllDockWidgetAreas, QtWidgets.QTabWidget.North)

    def closeEvent(self, event):
        self.frame.closed.emit()
        return super().closeEvent(event)

    if _dev_utils_enabled:

        @controller.exsafe
        def keyPressEvent(self, event):
            dev_services.on_key_press(self.frame, event)
            return super().keyPressEvent(event)


class StandaloneFrame(container.QWidgetContainer):
    """
    Main frame: the root of the GUI values tree and the central image area of :class:`MainWindow`.

    The panels shown in docks are still children of this container (added with
    ``location="skip"``), so their GUI values paths don't depend on where they are displayed.
    """

    # (name, title, default area); the order is the order of the View menu entries
    _docks = [
        ("camera", "Camera", "right"),
        ("processing", "Processing", "right"),
        ("image", "Image", "right"),
        ("saving", "Saving", "right"),
        ("status", "Status", "right"),
        ("time_series", "Time series", "bottom"),
    ]

    @controller.exsafe
    def setup(self, settings, cam_name, cam_desc, plugin_manager):
        super().setup(layout="vbox", no_margins=True)
        self.ctl.res_mgr = controller.sync_controller(resource_manager_thread)

        self.cam_desc = cam_desc
        self.cam_name = cam_name
        self.settings = settings
        self.plugin_manager = plugin_manager
        self.ctl.v["settings/runtime/root_folder"] = settings["runtime/root_folder"]
        self.load_locals()
        self.gui_level = "full"
        self.compact_interface = settings.get("interface/compact", False)

        ### Setup GUI
        self.window = MainWindow(self)
        cam_display_name = settings["cameras", self.cam_name].get("display_name", self.cam_name)
        self.window.setWindowTitle(f"{cam_display_name} control")
        self.window.setWindowIcon(QtGui.QIcon(resource_path("icon.ico")))
        self.cam_ctl = camera_control.GenericCameraCtl(
            cam_thread=cam_thread,
            frame_src_thread=process_thread,
            save_thread=save_thread,
            snap_save_thread=snap_save_thread,
            resource_manager_thread=resource_manager_thread,
            event_hooks_thread=event_hooks_manager_thread,
            frame_tag="frames/new/show",
            cam_name=cam_name,
            settings=settings,
        )
        self.add_child("cam_controller", self.cam_ctl, gui_values_path="cam")
        self.cam_ctl.setup()
        self.panels = {}
        for name, _, _ in self._docks:
            panel = self.add_child(
                f"{name}_panel", container.QFrameContainer(self), location="skip"
            )
            panel.setup(layout="vbox")
            self.panels[name] = panel
        # Central area: image (and plugin) display tabs
        self.plots_tabs = self.add_child("plot_tabs", container.QTabContainer(self))
        self.plots_tabs.setup()
        image_tab = self.plots_tabs.add_tab("standard_frame", "Standard", layout="vbox")
        self.image_proc_indicator = image_tab.add_child(
            "processing_indicator",
            ProcessingIndicator_ctl.ProcessingIndicator_GUI(self),
            gui_values_path="procind",
        )
        self.image_proc_indicator.setup(
            [
                ("binning", ProcessingIndicator_ctl.binning_item(preprocess_thread)),
                ("background", ProcessingIndicator_ctl.background_item(process_thread)),
            ]
        )
        self.cam_ctl.image_updated.connect(self.image_proc_indicator.update_indicators)
        self.image_plotter_area = image_tab.add_to_layout(pll_widgets.ImagePlotter(self))
        self.image_plotter_area.setup(name="plt", min_size=(320, 240))
        self.image_plotter_ctl = pll_widgets.ImagePlotterCtl(self)
        self.image_plotter_ctl.setup(
            self.image_plotter_area, save_values=("colormap", "img_lim_preset")
        )
        self.cam_ctl.add_child("plotter_area", self.image_plotter_area, gui_values_path=False)
        self.cam_ctl.add_child("plotter_ctl", self.image_plotter_ctl, gui_values_path="img")
        self.image_plotter_ctl.set_img_lim(-65536, 65536)
        self.image_view = image_view.ImageViewController(self.image_plotter_area)
        normalize = self.image_plotter_ctl.params.w["normalize"]
        normalize.setText("Auto levels")
        normalize.setToolTip("Scale the display range to each frame's minimum and maximum")
        self.image_plotter_area.set_colormap("gray_sat")
        # Image panel: plotter controls and display settings
        image_panel = self.panels["image"]
        view_params = image_panel.add_child(
            "image_view_params", param_table.ParamTable(self), gui_values_path="view"
        )
        view_params.setup(add_indicator=False)
        view_params.add_combo_box(
            "colormap",
            label="Colormap:",
            options=list(image_view.COLORMAPS.values()),
            index_values=list(image_view.COLORMAPS),
            value="gray_sat",
        )
        view_params.vs["colormap"].connect(controller.exsafe(self.image_plotter_area.set_colormap))
        view_params.add_button("fit_view", "Fit image to view")
        view_params.vs["fit_view"].connect(controller.exsafe(lambda _=None: self.image_view.fit()))
        view_params.w["fit_view"].setToolTip(
            "Show the whole image again after zooming (mouse wheel) or panning (drag)"
        )
        image_panel.add_group_box("image_ctl_box", caption="Image settings").add_to_layout(
            self.image_plotter_ctl
        )
        self.display_settings_table = DisplaySettings_ctl.DisplaySettings_GUI(self)
        image_panel.add_to_layout(self.display_settings_table)
        self.add_child(
            "display_settings_table",
            self.display_settings_table,
            gui_values_path="disp",
            location="skip",
        )
        self.display_settings_table.setup(slowdown_thread=slowdown_thread)
        self._unfix_width(self.display_settings_table)
        self.cam_ctl.image_updated.connect(self.display_settings_table.on_new_frame)
        image_panel.add_padding()
        # Camera panel
        self.cam_settings_table = self.cam_desc.make_gui_control(self)
        self.panels["camera"].add_group_box(
            "cam_settings_box", caption="Camera settings", no_margins=False
        ).add_to_layout(self.cam_settings_table)
        self.cam_settings_table.setup(self.cam_ctl)
        self.cam_ctl.add_child("settings", self.cam_settings_table, gui_values_path="cam")
        self.panels["camera"].add_padding()
        # Saving and status panels
        self._add_savebox(self.panels["saving"])
        self._unfix_width(self.saving_settings_table)
        self.panels["saving"].add_padding()
        status_box = self.panels["status"].add_group_box("status_box", caption="Status")
        self._add_camstatus(status_box)
        self._add_savestatus(status_box)
        self.panels["status"].add_padding()
        # Processing panel: processing settings, plus the plugins' control tabs
        self.control_tabs = self.panels["processing"].add_child(
            "control_tabs", container.QTabContainer(self)
        )
        self.control_tabs.setup()
        proc_tab = self.control_tabs.add_tab("proc_tab", "Processing", no_margins=False)
        self.frame_preprocessing_settings = proc_tab.add_child(
            "frame_preprocessing",
            FramePreprocess_ctl.FramePreproccessBinning_GUI(self),
            gui_values_path="preproc/*",
        )
        self.frame_preprocessing_settings.setup(preprocess_thread=preprocess_thread)
        self.slowdown_settings = proc_tab.add_child(
            "slowdown",
            FramePreprocess_ctl.FramePreproccessSlowdown_GUI(self),
            gui_values_path="preproc/*",
        )
        self.slowdown_settings.setup(slowdown_thread=slowdown_thread)
        self.frame_processing_settings = proc_tab.add_child(
            "frame_processing",
            FrameProcess_ctl.FrameProccess_GUI(self),
            gui_values_path="proc",
        )
        self.frame_processing_settings.setup(
            process_thread=process_thread, settings=settings.get("frame_processing")
        )
        proc_tab.add_spacer(10)
        # Time series plot (its own dock, shown while the plot is enabled)
        self.trace_plotter = pyqtgraph.PlotWidget(self)
        self.trace_plotter.setMinimumSize(300, 150)
        self.panels["time_series"].add_to_layout(self.trace_plotter)
        self.plotting_settings = proc_tab.add_child(
            "plotting_settings",
            PlotControl_ctl.PlotControl_GUI(self),
            gui_values_path="plotting",
        )
        self.plotting_settings.setup(
            channel_accumulator_thread, self.trace_plotter, settings=settings
        )
        self.plotting_settings.set_all_values(
            {
                "update_plot": True,
                "disp_last": 1000,
                "roi/center/x": 16,
                "roi/center/y": 16,
                "roi/size/x": 32,
                "roi/size/y": 32,
            }
        )
        proc_tab.add_padding()
        self.cam_ctl.set_all_values({"img/normalize": True})
        # Settings loading scope (chosen in the File menu; kept as a GUI value for compatibility)
        self._add_param_loading()
        # add virtual GUI values
        self._size_restored = False

        def restore_size(v):
            self.window.resize(*v)
            self._size_restored = True

        self.add_property_element(
            "defaults/window/size",
            lambda: (self.window.size().width(), self.window.size().height()),
            restore_size,
            add_indicator=False,
        )
        self.add_property_element(
            "defaults/window/position",
            lambda: (self.window.geometry().x(), self.window.geometry().y()),
            lambda v: self.window.move(v[0], v[1]),
            add_indicator=False,
        )
        self._maximized = False

        def set_maximized(v):
            if v and self.window.isVisible():
                self.window.showMaximized()
            self._maximized = v

        self.add_property_element(
            "defaults/window/maximized",
            lambda: bool(self.window.windowState() & QtCore.Qt.WindowMaximized),
            set_maximized,
            add_indicator=False,
        )
        self._dock_state_restored = False

        def restore_dock_state(v):
            self._dock_state_restored = self.window.restoreState(
                QtCore.QByteArray.fromHex(v.encode())
            )

        self.add_property_element(
            "defaults/window/dock_state",
            lambda: bytes(self.window.saveState().toHex()).decode(),
            restore_dock_state,
            add_indicator=False,
        )
        self.add_virtual_element("defaults/settings_folder", value="", add_indicator=False)
        self.settings_editor = None
        self.about_window = None

        # Setup plugins
        plugin_tab = self.control_tabs.add_tab(
            "plugins", "Plugins", gui_values_path="plugins", layout="grid"
        )
        plugin_tab.add_padding(stretch=1, location=50)
        self._plugin_parameters = dictionary.Dictionary()

        # Window: docks, actions, menus, toolbar, status bar
        self.window.setCentralWidget(self)
        self.docks = {}
        for name, title, _ in self._docks:
            self.docks[name] = self._make_dock(name, title, self.panels[name])
        self.arrange_default_layout()
        self._setup_actions()
        self._setup_menus()
        self._setup_toolbar()
        self._setup_status_bar()
        self.plotting_settings.params.vs["enable"].connect(self._show_time_series)

        # Setup parameters loading and saving
        if "interface/level" in settings:
            self.set_gui_level(settings["interface/level"])
        # Setup settings manager
        settings_ctl = controller.sync_controller(settings_manager_thread)
        settings_ctl.ca.add_source(
            "gui",
            controller.call_in_gui_thread(lambda: self.get_all_values(full_status=True)),
        )
        settings_ctl.ca.update_settings("cfg", settings.copy())
        # Setup event hooks manager
        self.event_hooks_ctl = controller.sync_controller(event_hooks_manager_thread)
        # Setup thread methods and signals
        self.ctl.finished.connect(self.stop)
        self.ctl.add_thread_method("get_all_values", self.get_all_values)
        self.ctl.add_thread_method("set_all_values", self.set_all_values)

        def toggle_tab(tabs, name):
            self.c[tabs].set_by_name(name)

        self.ctl.add_thread_method("toggle_tab", toggle_tab)
        # Load plugins
        self.plugin_manager.sync_plugins()
        self.plugin_manager.set_main_frame(self)
        self.plugin_manager.unlock_barrier("plugin_setup")

    @staticmethod
    def _unfix_width(widget):
        """Let a panel that sets a fixed width for the old column layout fill its dock instead"""
        widget.setMinimumWidth(0)
        widget.setMaximumWidth(QtWidgets.QWIDGETSIZE_MAX)

    def _make_dock(self, name, title, widget):
        dock = QtWidgets.QDockWidget(title, self.window)
        dock.setObjectName(f"dock_{name}")  # identifies the dock in saveState/restoreState
        scroll = QtWidgets.QScrollArea(dock)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setWidget(widget)
        # don't let the dock shrink until every field is clipped, but don't let a panel with
        # a wide layout claim half the window either (it scrolls horizontally instead)
        scroll.setMinimumWidth(
            min(
                widget.minimumSizeHint().width() + scroll.verticalScrollBar().sizeHint().width(),
                self._dock_min_width,
            )
        )
        dock.setWidget(scroll)
        return dock

    _dock_areas = {"right": QtCore.Qt.RightDockWidgetArea, "bottom": QtCore.Qt.BottomDockWidgetArea}
    _dock_min_width = 340
    _dock_max_default_width = 520
    _dock_default_height = 220

    @controller.exsafe
    def arrange_default_layout(self):
        """Put all docks back in their default places"""
        d = self.docks
        for dock in d.values():
            self.window.removeDockWidget(dock)
            dock.setFloating(False)
        # the right column reaches down to the status bar; the time series plot sits below
        # the image only
        self.window.setCorner(QtCore.Qt.BottomRightCorner, QtCore.Qt.RightDockWidgetArea)
        right = self._dock_areas["right"]
        # right column: camera / processing / image tabs on top, saving / status tabs below;
        # with the compact interface, all of them share one tab group
        self.window.addDockWidget(right, d["camera"], QtCore.Qt.Vertical)
        if not self.compact_interface:
            self.window.addDockWidget(right, d["saving"], QtCore.Qt.Vertical)
        for name in ["processing", "image"]:
            self.window.tabifyDockWidget(d["camera"], d[name])
        if self.compact_interface:
            self.window.tabifyDockWidget(d["camera"], d["saving"])
            self.window.tabifyDockWidget(d["camera"], d["status"])
        else:
            self.window.tabifyDockWidget(d["saving"], d["status"])
            d["saving"].raise_()
        self.window.addDockWidget(self._dock_areas["bottom"], d["time_series"])
        for dock in d.values():
            dock.show()
        d["camera"].raise_()
        self._show_time_series(self.plotting_settings.v["enable"])
        self._resize_default_docks()

    def _resize_default_docks(self):
        """Give the default dock layout its default sizes (only effective once shown)"""
        d = self.docks
        # wide enough for the right column's panels to show without scrolling, within limits
        needed = max(
            self.panels[n].minimumSizeHint().width() for n in ["camera", "processing", "saving"]
        )
        width = max(self._dock_min_width, min(needed + 24, self._dock_max_default_width))
        self.window.resizeDocks([d["camera"]], [width], QtCore.Qt.Horizontal)
        self.window.resizeDocks([d["time_series"]], [self._dock_default_height], QtCore.Qt.Vertical)

    def _show_time_series(self, enabled):
        dock = self.docks["time_series"]
        appearing = enabled and not dock.isVisible()
        dock.setVisible(bool(enabled))
        if appearing and not dock.isFloating():
            QtCore.QTimer.singleShot(
                0,
                lambda: self.window.resizeDocks(
                    [dock], [self._dock_default_height], QtCore.Qt.Vertical
                ),
            )

    def _add_action(self, name, text, slot, shortcut=None, icon=None, checkable=False, tip=None):
        action = QtWidgets.QAction(text, self.window)
        if icon is not None:
            action.setIcon(
                QtGui.QIcon(resource_path(icon))
                if icon.endswith(".png")
                else self.window.style().standardIcon(getattr(QtWidgets.QStyle, icon))
            )
        if shortcut is not None:
            action.setShortcut(QtGui.QKeySequence(shortcut))
            action.setShortcutContext(QtCore.Qt.WindowShortcut)
        action.setCheckable(checkable)
        if tip is not None:
            action.setToolTip(tip + (f" ({action.shortcut().toString()})" if shortcut else ""))
            action.setStatusTip(tip)
        action.triggered.connect(controller.exsafe(slot))
        self.window.addAction(action)  # shortcuts work even while the action's menu is hidden
        self.actions[name] = action
        return action

    def _setup_actions(self):
        self.actions = {}
        ks = QtGui.QKeySequence
        add = self._add_action
        add(
            "connect",
            "&Connect",
            self.cam_ctl.dev_connect,
            icon="SP_DialogYesButton",
            tip="Connect to the camera",
        )
        add(
            "disconnect",
            "&Disconnect",
            self.cam_ctl.dev_disconnect,
            icon="SP_DialogNoButton",
            tip="Disconnect from the camera",
        )
        add(
            "start",
            "&Start acquisition",
            self.cam_ctl.acq_start,
            icon="play.png",
            tip="Start acquisition",
        )
        add(
            "stop",
            "S&top acquisition",
            self.cam_ctl.acq_stop,
            icon="stop.png",
            tip="Stop acquisition",
        )
        add(
            "toggle_acquisition",
            "Start/stop acquisition",
            self.toggle_acquisition,
            shortcut="Ctrl+Space",
        )
        add(
            "record",
            "&Record",
            self.set_recording,
            shortcut="Ctrl+R",
            icon="rec.png",
            checkable=True,
            tip="Start/stop saving frames",
        )
        add(
            "snap",
            "S&nap",
            self.snap,
            shortcut="Ctrl+T",
            icon="SP_DialogSaveButton",
            tip="Save the displayed frame",
        )
        add(
            "auto_levels",
            "&Auto levels",
            self.set_auto_levels,
            shortcut="Ctrl+L",
            checkable=True,
            tip="Scale the display to each frame's min/max",
        )
        add(
            "fit_view",
            "Fit &view",
            self.image_view.fit,
            shortcut="Ctrl+0",
            tip="Show the whole image (after zooming with the mouse wheel or panning)",
        )
        add(
            "full_screen",
            "&Full screen",
            self.set_full_screen,
            shortcut=ks.FullScreen,
            checkable=True,
        )
        add("reset_layout", "&Reset layout", self.arrange_default_layout)
        add("load_settings", "&Load settings...", self.on_load_settings_button, shortcut=ks.Open)
        add("save_settings", "&Save settings...", self.on_save_settings_button, shortcut=ks.Save)
        add("open_save_folder", "Open saving &folder", self.open_save_folder)
        add(
            "preferences",
            "&Preferences...",
            lambda: self.call_extra("settings_editor"),
            shortcut=ks.Preferences,
        )
        self.actions["preferences"].setMenuRole(QtWidgets.QAction.PreferencesRole)
        add("cam_shortcut", "Create camera &shortcut...", lambda: self.call_extra("cam_shortcut"))
        add("quit", "&Quit", self.window.close, shortcut=ks.Quit)
        self.actions["quit"].setMenuRole(QtWidgets.QAction.QuitRole)
        add("shortcuts", "&Keyboard shortcuts", self.show_shortcuts)
        add("open_log_folder", "Open &log folder", self.open_log_folder)
        add("about", "&About", lambda: self.call_extra("about"))
        self.actions["about"].setMenuRole(QtWidgets.QAction.AboutRole)

    def _setup_menus(self):
        menubar = self.window.menuBar()
        a = self.actions
        file_menu = menubar.addMenu("&File")
        file_menu.addAction(a["load_settings"])
        scope_menu = file_menu.addMenu("Settings loading scope")
        scope_group = QtWidgets.QActionGroup(self.window)
        for value, label in [("all", "All"), ("camera", "Camera only"), ("gui", "GUI only")]:
            act = scope_menu.addAction(label)
            act.setCheckable(True)
            act.setData(value)
            scope_group.addAction(act)
        scope_group.triggered.connect(lambda act: self.set_value("settings_load_scope", act.data()))

        def sync_scope():
            for act in scope_group.actions():
                act.setChecked(act.data() == self.v["settings_load_scope"])

        scope_menu.aboutToShow.connect(sync_scope)
        file_menu.addAction(a["save_settings"])
        file_menu.addSeparator()
        file_menu.addAction(a["open_save_folder"])
        if win32com_present:
            file_menu.addAction(a["cam_shortcut"])
        file_menu.addAction(a["preferences"])
        file_menu.addSeparator()
        file_menu.addAction(a["quit"])
        camera_menu = menubar.addMenu("&Camera")
        for name in [
            "connect",
            "disconnect",
            None,
            "start",
            "stop",
            "toggle_acquisition",
            None,
            "snap",
            "record",
        ]:
            camera_menu.addSeparator() if name is None else camera_menu.addAction(a[name])
        view_menu = menubar.addMenu("&View")
        for name, _, _ in self._docks:
            view_menu.addAction(self.docks[name].toggleViewAction())
        view_menu.addSeparator()
        view_menu.addAction(a["auto_levels"])
        view_menu.addAction(a["fit_view"])
        view_menu.addAction(a["full_screen"])
        view_menu.addAction(a["reset_layout"])
        self._view_menu = view_menu
        help_menu = menubar.addMenu("&Help")
        for name in ["shortcuts", "open_log_folder", "about"]:
            help_menu.addAction(a[name])

    def _setup_toolbar(self):
        toolbar = self.window.addToolBar("Main toolbar")
        toolbar.setObjectName("toolbar_main")
        toolbar.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        toolbar.setIconSize(QtCore.QSize(16, 16))
        for name in [
            "connect",
            "disconnect",
            None,
            "start",
            "stop",
            None,
            "snap",
            "record",
            None,
            "auto_levels",
            "fit_view",
        ]:
            toolbar.addSeparator() if name is None else toolbar.addAction(self.actions[name])
        self._view_menu.insertAction(self.actions["auto_levels"], toolbar.toggleViewAction())

    def _setup_status_bar(self):
        status_bar = self.window.statusBar()
        self._status_labels = {}
        for name in ["camera", "fps", "saving", "pixel"]:
            label = QtWidgets.QLabel(status_bar)
            label.setContentsMargins(6, 0, 6, 0)
            status_bar.addWidget(label)
            self._status_labels[name] = label
        self.image_view.pixel_hovered.connect(self._status_labels["pixel"].setText)
        self.activity_indicator = ActivityIndicator_ctl.ActivityIndicator_GUI(status_bar)
        self.add_child(
            "activity_indicator",
            self.activity_indicator,
            gui_values_path="activity_indicator",
            location="skip",
        )
        self.activity_indicator.setup(
            resource_manager_thread=resource_manager_thread, orientation="horizontal"
        )
        status_bar.addPermanentWidget(self.activity_indicator)
        self.add_timer_event("update_status_bar", self.update_status_bar, period=0.5)

    _connection_text = {"opened": "Connected", "closed": "Disconnected"}

    def update_status_bar(self):
        """Refresh the status bar and the enabled/checked state of the actions"""
        dev = self.cam_ctl.dev
        if dev is None:
            return
        connection = dev.v["status/connection"]
        acquisition = dev.v["status/acquisition"]
        connected = connection == "opened"
        acquiring = connected and acquisition not in {None, "stopped"}
        text = self._connection_text.get(connection, str(connection).capitalize())
        if connected:
            text += " · " + ("Acquiring" if acquiring else "Idle")
        error = None if connected else dev.v["device_error"]
        camera_label = self._status_labels["camera"]
        camera_label.setText(text + (" · camera error" if error else ""))
        camera_label.setToolTip(error or "")
        background = "#ff6b6b" if error else "gold"
        camera_label.setStyleSheet(
            "" if connected else f"background: {background}; color: black; font-weight: bold"
        )
        # values are None until the first acquisition / display update
        fps = (dev.v["frames/fps"] or 0) if acquiring else 0
        display_fps = self.display_settings_table.v["display_fps"] or 0
        self._status_labels["fps"].setText(f"Camera {fps:.1f} fps · display {display_fps:.1f} fps")
        saving = self.cam_ctl.saving_in_progress()
        saved = (self.cam_ctl.saver.v["saved"] or 0) if self.cam_ctl.saver else 0
        self._status_labels["saving"].setText(
            f"Saving: {saved} frames" if saving else f"Last saved: {saved} frames"
        )
        a = self.actions
        a["connect"].setEnabled(not connected)
        a["disconnect"].setEnabled(connected)
        a["start"].setEnabled(connected and not acquiring)
        a["stop"].setEnabled(acquiring)
        a["record"].setChecked(bool(self.saving_settings_table.v["saving"]))
        a["auto_levels"].setChecked(bool(self.image_plotter_ctl.v["normalize"]))
        a["full_screen"].setChecked(self.window.isFullScreen())

    def toggle_acquisition(self):
        dev = self.cam_ctl.dev
        if dev is not None and dev.v["status/acquisition"] not in {None, "stopped"}:
            self.cam_ctl.acq_stop()
        else:
            self.cam_ctl.acq_start()

    def set_recording(self, enabled):
        self.saving_settings_table.v["saving"] = bool(enabled)

    def snap(self):
        self.cam_ctl.toggle_saving(
            mode="snap", source=self.saving_settings_table.v["snap_display_source"]
        )

    def set_auto_levels(self, enabled):
        self.image_plotter_ctl.v["normalize"] = bool(enabled)

    def set_full_screen(self, enabled):
        if enabled:
            self.window.showFullScreen()
        else:
            self.window.showNormal()

    def open_save_folder(self):
        path = self.saving_settings_table.v["path"] or ""
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        while folder and not os.path.isdir(folder):
            folder = os.path.dirname(folder)
        if folder:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))

    def open_log_folder(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(os.getcwd()))

    def show_shortcuts(self):
        rows = "".join(
            f"<tr><td>{a.text().replace('&', '')}</td><td>&nbsp;&nbsp;<b>"
            f"{a.shortcut().toString(QtGui.QKeySequence.NativeText)}</b></td></tr>"
            for a in self.actions.values()
            if not a.shortcut().isEmpty()
        )
        QtWidgets.QMessageBox.information(
            self.window, "Keyboard shortcuts", f"<table>{rows}</table>"
        )

    def _add_savebox(self, parent):
        self.saving_settings_table = parent.add_to_layout(SaveBox_ctl.SaveBox_GUI(self))
        self.saving_settings_table.setup(self.cam_ctl)
        self.cam_ctl.add_child("savebox", self.saving_settings_table, gui_values_path="save")

    def _add_camstatus(self, parent):
        self.cam_status_table = parent.add_to_layout(self.cam_desc.make_gui_status(self))
        self.cam_status_table.setup(self.cam_ctl)
        self.cam_ctl.add_child("camstat", self.cam_status_table, gui_values_path="camstat")

    def _add_savestatus(self, parent):
        self.save_status_table = parent.add_to_layout(SaveBox_ctl.SaveStatus_GUI(self))
        self.save_status_table.setup(self.cam_ctl)
        self.cam_ctl.add_child("savestat", self.save_status_table, gui_values_path="savestat")

    def _add_param_loading(self):
        # Never shown: the File menu edits this value; kept as a widget so the stored GUI
        # values (and the settings files written by earlier versions) stay compatible
        self.params_loading_settings = param_table.ParamTable(self)
        self.add_child("params_loading_settings", self.params_loading_settings, location="skip")
        self.params_loading_settings.setup(add_indicator=False)
        self.params_loading_settings.add_combo_box(
            "settings_load_scope",
            label="Loading scope:",
            options=["All", "Camera", "GUI"],
            index_values=["all", "camera", "gui"],
        )
        self.params_loading_settings.hide()

    closed = Signal()

    def show(self):
        if not self._size_restored:  # first run: a reasonable share of the screen
            screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
            self.window.resize(
                min(int(screen.width() * 0.85), 1600), min(int(screen.height() * 0.85), 1000)
            )
        if self._maximized:
            self._maximized = False
            self.window.showMaximized()
        else:
            self.window.show()
        if not self._dock_state_restored:
            # after the first layout pass; resizing an unlaid-out window has no effect
            QtCore.QTimer.singleShot(0, self._resize_default_docks)

    @controller.exsafeSlot()
    def on_load_settings_button(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Load GUI settings...",
            filter="Config Files (*.cfg);;Data Settings Files (*.dat);;All Files (*)",
            **{qtkwargs.file_dialog_dir: self.v["defaults/settings_folder"]},
        )
        if path:
            self.v["defaults/settings_folder"] = os.path.split(path)[0]
            self.load_settings(path, scope=self.v["settings_load_scope"], cam_apply=True)

    @controller.exsafeSlot()
    def on_save_settings_button(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save GUI settings...",
            filter="Config Files (*.cfg);;Data Settings Files (*.dat);;All Files (*)",
            **{qtkwargs.file_dialog_dir: self.v["defaults/settings_folder"]},
        )
        if path:
            self.v["defaults/settings_folder"] = os.path.split(path)[0]
            self.save_settings(path)

    def show_settings_editor(self):
        self.settings_editor = settings_editor.SettingsEditor(self)
        if self.settings_editor.setup(self):
            self.settings_editor.show()
        else:
            self.settings_editor = None

    def show_about_window(self):
        self.about_window = about.AboutBox(self)
        self.about_window.setup()
        self.about_window.show()

    def create_camera_shortcut(self):
        if not win32com_present:
            QtWidgets.QMessageBox.warning(
                self,
                "OS not supported",
                "Camera shortcuts creation is supported only in Windows",
                QtWidgets.QMessageBox.Ok,
            )
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Create camera shortcut...",
            filter="Shortcuts (*.lnk);;All Files (*)",
            **{qtkwargs.file_dialog_dir: os.path.expanduser("~\\Desktop")},
        )
        if path:
            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(path)
            shortcut.TargetPath = os.path.abspath(os.path.join("..", "control.exe"))
            shortcut.WorkingDirectory = os.path.abspath(os.path.join(".."))
            shortcut.Arguments = f'-c "{self.cam_name}"'
            shortcut.IconLocation = resource_path("icon.ico")
            shortcut.Save()

    @controller.exsafeSlot(object)
    def call_extra(self, value):
        if value == "cam_shortcut":
            self.create_camera_shortcut()
        if value == "settings_editor":
            if self.settings_editor is None:
                self.show_settings_editor()
            else:
                self.settings_editor.showNormal()
        if value == "about":
            if self.about_window is None:
                self.show_about_window()
            else:
                self.about_window.showNormal()

    @controller.call_in_gui_thread
    def load_plugin(self, plugin_class, name="__default__", parameters=None, start_order=0):
        """
        Start plugin thread.

        Args:
            plugin_class: class of the plugin (subclass of :cls:`.IPlugin`)
            name: plugin name (for the cases of several plugins of the same class)
            parameters: additional plugin parameters
        """
        self.plugin_manager.start_plugin(
            plugin_class,
            name=name,
            parameters=parameters,
            start_order=start_order,
            barriers={"plugin_start"} if not self._running else {},
        )

    @controller.call_in_gui_thread
    def initialize_plugin(self, plugin):
        """
        Initialize plugin.

        Called automatically by the plugin thread after plugin has been set up.
        """
        full_name = plugin.get_class_name(), plugin.get_instance_name()
        if full_name in self._plugin_parameters:
            values = self._plugin_parameters[full_name]
            plugin.set_all_values(values)
        plugin.start_gui()

    @controller.call_in_gui_thread
    def finalize_plugin(self, plugin):
        """
        Finalize plugin.

        Called automatically by the plugin thread after plugin has been stopped and before it was cleaned up.
        """
        full_name = plugin.get_class_name(), plugin.get_instance_name()
        self.plugin_manager.remove_plugin(full_name)
        if not plugin.is_running():
            return
        values = plugin.get_all_values()
        if full_name in self._plugin_parameters:
            del self._plugin_parameters[full_name]
        self._plugin_parameters[full_name] = values

    def set_gui_level(self, level):
        """Set GUI detail level (either ``"full"`` for all controls, or ``"simplified"`` for essentials)"""
        self.gui_level = level
        if level in {"simple", "simplified"}:
            self.control_tabs.remove_tab("proc_tab")

    def _update_plugin_parameters(self):
        """Update ``_plugin_parameters`` attribute to reflect current plugin parameters"""
        for name, plugin in self.plugin_manager.get_running_plugins(ordered=True):
            if plugin.ctl.is_plugin_running():
                par = plugin.ctl.get_all_values()
                self._plugin_parameters.add_entry(name, par, force=True)

    def _apply_plugin_parameters(self):
        """Apply values in the ``_plugin_parameters`` attribute to all running plugins"""
        for name, plugin in self.plugin_manager.get_running_plugins(ordered=True):
            if plugin.ctl.is_plugin_running() and name in self._plugin_parameters:
                plugin.ctl.set_all_values(self._plugin_parameters[name])

    def get_all_values(self, full_status=False):
        """
        Get all GUI parameters (taking into account GUI level).

        If ``full_status==True``, also include status tables and GUI indicator values are stored.
        """
        values = super().get_all_values()
        self._update_plugin_parameters()
        values.add_entry("plugins", self._plugin_parameters, force=True)
        if full_status:
            values["indicators"] = self.get_all_indicators()
        else:
            for k in ["camstat", "savestat"]:
                if ("cam", k) in values:
                    del values["cam", k]
        return values

    def set_all_values(self, values):
        """Set all GUI parameters (taking into account GUI level)"""
        if "plugins" in values:
            self._plugin_parameters = values.detach("plugins")
            self._apply_plugin_parameters()
        super().set_all_values(values)
        self.image_proc_indicator.update_indicators()

    def load_settings(self, path=None, warn_if_missing=True, scope="all", cam_apply=False):
        if path is None:
            path = _defaults_filename
        if os.path.exists(path):
            settings = loadfile.load_dict(path)
            if "gui" in settings:  # data stored settings file
                ver = settings.get("software/version", None)
                cam = settings.get("cfg/select_camera", None)
                if ver is None:
                    warn_msg = "settings file does not contain the software version"
                elif compare_version(ver) == "?":
                    warn_msg = f"unrecognized version {ver}"
                elif compare_version(ver) == ">":
                    warn_msg = f"settings file version ({ver}) is higher than the current version ({version})"
                elif cam is None:
                    warn_msg = "settings file does not contain the camera name"
                elif cam != self.cam_name:
                    warn_msg = f"settings file camera ({cam}) is different from the current camera ({self.cam_name})"
                else:
                    warn_msg = None
                if warn_msg is not None and warn_if_missing:
                    result = QtWidgets.QMessageBox.warning(
                        self,
                        "Incompatible settings format",
                        f"Warning: {warn_msg}; load anyway?",
                        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    )
                    if result == QtWidgets.QMessageBox.No:
                        return
                settings = settings["gui"]
            elif self.cam_name in settings:
                settings = settings[self.cam_name]
            else:
                if warn_if_missing:
                    QtWidgets.QMessageBox.warning(
                        self,
                        "Missing settings in the file",
                        "Warning: either loading not a settings file, or it contains settings for different cameras",
                        QtWidgets.QMessageBox.Ok,
                    )
                settings = {}
            if scope == "gui" and "cam/cam" in settings:
                del settings["cam/cam"]
            if scope == "camera":
                settings = dictionary.Dictionary({"cam/cam": settings.get("cam/cam", {})})
            self.event_hooks_ctl.cs.call_hook("gui/settings/set/pre_set", settings=settings)
            self.set_all_values(settings)
            if cam_apply and scope in {"all", "camera"}:
                self.cam_ctl.send_parameters()
            self.event_hooks_ctl.cs.call_hook("gui/settings/set/post_set", settings=settings)
            return True
        return False

    def load_locals(self):
        self.locals = (
            loadfile.load_dict(_locals_filename)
            if os.path.exists(_locals_filename)
            else dictionary.Dictionary()
        )

    def save_locals(self):
        savefile.save_dict(self.locals, _locals_filename)

    @controller.exsafe
    def save_settings(self, path=None):
        if path is None:
            path = _defaults_filename
        if os.path.exists(path):
            settings = loadfile.load_dict(path)
        else:
            settings = dictionary.Dictionary()
        if self.cam_name in settings:
            del settings[self.cam_name]
        self.event_hooks_ctl.cs.call_hook("gui/settings/get/pre_get")
        save_settings = self.get_all_values()
        self.event_hooks_ctl.cs.call_hook("gui/settings/get/post_get", settings=save_settings)
        settings[self.cam_name] = save_settings
        savefile.save_dict(settings, path)
        self.event_hooks_ctl.cs.call_hook("gui/settings/get/post_save", settings=settings)

    @controller.exsafeSlot()
    def start(self):
        self.plugin_manager.sync_plugins()
        self.cam_ctl.sync_controller()
        self.load_settings(path=self.settings.get("interface/defaults_path"), warn_if_missing=False)
        self.cam_ctl.dev.cs.apply_parameters({})  # sync with the camera
        super().start()
        self.plugin_manager.unlock_barrier("plugin_start")

    @controller.toploopSlot()
    def stop(self):
        if self._running:
            self.save_settings(path=self.settings.get("interface/defaults_path"))
            self.save_locals()
        for plugin in self.plugin_manager.get_running_plugins().values():
            plugin.ctl.stop(sync=True)
        super().stop()


class CamSelectFrame(param_table.ParamTable):
    def setup(self, settings):
        super().setup(name="camera_select", add_indicator=False)
        self.settings = settings
        self.setWindowFlag(QtCore.Qt.WindowMaximizeButtonHint, False)
        self.setWindowFlag(QtCore.Qt.WindowMinimizeButtonHint, False)
        self.setMinimumWidth(300)
        self.setWindowTitle("Select camera...")
        self.setWindowIcon(QtGui.QIcon(resource_path("icon.ico")))
        self.selected = False  # prevents double-call on multiple clicks
        cameras = self.settings["cameras"]
        cam_ids = list(cameras)
        cam_names = [cameras[k].get("display_name", k) for k in cam_ids]
        self.add_combo_box(
            "camera", label="Select camera:", options=cam_names, index_values=cam_ids
        )
        self.button_box = QtWidgets.QDialogButtonBox(self)
        self.button_box.setStandardButtons(
            QtWidgets.QDialogButtonBox.Close | QtWidgets.QDialogButtonBox.Ok
        )
        self.button_box.setCenterButtons(True)
        self.add_to_layout(self.button_box, location=("next", 0, 1, "end"))
        self.button_box.accepted.connect(controller.exsafe(lambda: self.on_select(True)))
        self.button_box.rejected.connect(controller.exsafe(lambda: self.on_select(False)))

    def showEvent(self, event):
        super().showEvent(event)
        self.setFixedHeight(self.height())

    camera_selected = Signal()

    def on_select(self, accepted):
        if not self.selected:
            self.selected = True
            if accepted:
                self.settings["select_camera"] = self.v["camera"]
                self.setEnabled(False)
                splash.update_splash_screen(True)
                self.camera_selected.emit()
            self.close()


class MissingSettingsFrame(param_table.ParamTable):
    def setup(self, path):
        super().setup(name="missing_settings", add_indicator=False)
        self.path = path
        self.setWindowTitle("Import config...")
        self.setWindowIcon(QtGui.QIcon(resource_path("icon.ico")))
        self.setWindowFlag(QtCore.Qt.WindowMaximizeButtonHint, False)
        self.setWindowFlag(QtCore.Qt.WindowMinimizeButtonHint, False)
        self.setFixedWidth(300)
        self.setFixedHeight(80)
        self.selected = False  # prevents double-call on multiple clicks
        w = self.add_decoration_label(
            "Would you like to import config from a previous version, or to continue with camera detection?",
            location=(0, 0, 1, "end"),
        )
        w.setWordWrap(True)
        with self.using_new_sublayout("buttons", "hbox"):
            self.add_button("import", "Import")
            self.vs["import"].connect(self.import_settings)
            self.add_button("detect", "Continue")
            self.vs["detect"].connect(self.detect_cameras)

    def _copy_settings(self, src, dst):
        ver = loadfile.load_dict(src).get("info/version")
        cmp = compare_version(ver)
        errmsg = None
        if cmp == "?":
            errmsg = f"unrecognized version in the settings file: {ver}"
        elif cmp == ">":
            errmsg = f"settings version {ver} is newer than the current version {version}; some settings may not transfer correctly"
        if errmsg is not None:
            QtWidgets.QMessageBox.warning(
                self,
                "Potential settings incompatibility",
                f"Warning: {errmsg}",
                QtWidgets.QMessageBox.Ok,
            )
        file_utils.retry_copy(src, dst)
        srcdir = os.path.split(src)[0]
        dstdir = os.path.split(dst)[0]
        if os.path.exists(os.path.join(srcdir, "plugins")):
            file_utils.retry_copy_dir(
                os.path.join(srcdir, "plugins"),
                os.path.join(dstdir, "plugins"),
                overwrite=False,
            )
        if os.path.exists(os.path.join(srcdir, _defaults_filename)):
            file_utils.retry_copy(
                os.path.join(srcdir, _defaults_filename),
                os.path.join(dstdir, _defaults_filename),
            )

    @controller.exsafe
    def import_settings(self):
        if not self.selected:
            self.selected = True
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "Import config...",
                filter="Settings Config (settings.cfg);;Config Files (*.cfg);;All Files (*)",
            )
            if path:
                self.hide()
                self._copy_settings(path, self.path)
                start_app(ask_on_no_cam=False)
            self.close()

    @controller.exsafe
    def detect_cameras(self):
        if not self.selected:
            self.selected = True
            self.hide()
            for execname in [
                "python.exe",
                "python3.exe",
                "python",
                "python3",
            ]:  # avoid wpython
                pythonexec = os.path.join(os.path.split(sys.executable)[0], execname)
                if os.path.exists(pythonexec):
                    break
            subprocess.call(
                [
                    pythonexec,
                    "-m",
                    "camcontrol.detect",
                    "--yes",
                    "--wait",
                    "--config-file",
                    self.path,
                ]
            )
            start_app(ask_on_no_cam=False)


class ErrorBoxDisplay:
    """Error box display, which handles error message popup upon exceptions"""

    def __init__(self):
        self.error_msg = None

    def check_for_error(self):
        if self.error_msg is None:
            return
        threadprop.get_app().closeAllWindows()
        app = threadprop.get_app()
        frame = error_message.ErrorBox()
        frame.setup(self.error_msg)
        frame.show()
        app.exec_()

    def on_error(self):
        """Show error message dialog box"""
        etype, exc, _ = sys.exc_info()
        if etype is not None:
            self.error_msg = "  ".join(traceback.format_exception_only(etype, exc)).strip()


error_display = ErrorBoxDisplay()


### Command line arguments ###
def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Pylablib cam-control software for controlling all connected cameras"
    )
    parser.add_argument("--camera", "-c", help="controlled camera name", metavar="CAM_NAME")
    parser.add_argument(
        "--config-file",
        "-cf",
        help="configuration file path",
        metavar="FILE",
        default="settings.cfg",
    )
    parser.add_argument("--subconfig", "-sc", help="subconfiguration name", metavar="SC_NAME")
    return parser.parse_args(argv)


argvp = None


def load_config(path):
    """Load settings config file and add real-time data"""
    if os.path.exists(path):
        settings = loadfile.load_dict(path)
        if "dlls" in settings:
            for k, v in settings["dlls"].items():
                pylablib.par["devices/dlls", k] = v
    else:
        settings = dictionary.Dictionary()
    settings["runtime/root_folder"] = os.path.abspath(".")
    settings["runtime/settings_src"] = os.path.abspath(path)
    return settings


def start_threads(settings, cam_desc):
    """Start and partially set up auxiliary threads"""
    control_settings = cam_desc.get_control_settings()
    services.FrameBinningThread(
        preprocess_thread, kwargs={"src": cam_thread, "tag_in": "frames/new"}
    ).start()
    services.FrameSlowdownThread(
        slowdown_thread, kwargs={"src": preprocess_thread, "tag_in": "frames/new"}
    ).start()
    services.FrameProcessorThread(
        process_thread, kwargs={"src": slowdown_thread, "tag_in": "frames/new"}
    ).start()
    services.ChannelAccumulator(
        channel_accumulator_thread,
        kwargs={"settings": settings.get("interface/trace_plotter")},
    ).start()
    services.FrameSaveThread(
        save_thread,
        kwargs={
            "src": preprocess_thread,
            "tag": "frames/new",
            "settings_mgr": settings_manager_thread,
            "frame_processor": process_thread,
            "garbage_collector": garbage_collector_thread,
        },
    ).start()
    services.FrameSaveThread(
        snap_save_thread,
        kwargs={
            "src": "any",
            "tag": "frames/new/snap",
            "settings_mgr": settings_manager_thread,
        },
    ).start()
    services.SettingsManager(settings_manager_thread).start()
    services.EventHooksManager(event_hooks_manager_thread).start()
    services.ResourceManager(resource_manager_thread).start()
    allow_garbage_collection = control_settings.get("gc/enabled", True)
    garbage_collection_periods = control_settings.get("gc/periods", {"default": 10, "saving": 60})
    services.GarbageCollector(
        garbage_collector_thread,
        kwargs={
            "disabled": not allow_garbage_collection,
            "periods": garbage_collection_periods,
        },
    ).start()
    channel_accum = controller.sync_controller(channel_accumulator_thread)
    channel_accum.cs.add_source(
        "raw", src=preprocess_thread, tag="frames/new", sync=True, kind="raw"
    )
    channel_accum.cs.add_source(
        "show", src=process_thread, tag="frames/new/show", sync=True, kind="show"
    )
    image_saver = controller.sync_controller(save_thread)
    image_saver.ca.setup_queue_ram(settings.get("saving/max_queue_ram", 4 * 2**30))


_displayed_forms = []  # against garbage collection


@controller.exsafe
def start_app(ask_on_no_cam=True):
    """Start the application: determine the camera, create and set up frame"""
    splash.update_splash_screen(True, msg="Launching the application...")
    app = threadprop.get_app()
    settings = load_config(argvp.config_file)
    if "cameras" not in settings and ask_on_no_cam:
        missing_settings_form = MissingSettingsFrame()
        missing_settings_form.setup(argvp.config_file)
        missing_settings_form.show()
        splash.update_splash_screen(False)
        _displayed_forms.append(missing_settings_form)
        return
    cams = settings.get("cameras", {})
    if len(cams) == 0:  # Show a message and exit
        splash_screen = splash.get_splash_screen()
        QtWidgets.QMessageBox.warning(
            splash_screen,
            "No cameras",
            "No cameras detected. Make sure that the cameras are connected, turned on, and not used by other software.",
            QtWidgets.QMessageBox.Ok,
        )
        controller.stop_app(code=1)
    app.setStyleSheet(color_theme.load_style(settings.get("interface/color_theme", "dark")))
    select_cameras = [argvp.camera, settings.pop("select_camera", None)]
    if len(cams) == 1:
        select_cameras.append(list(cams)[0])
    for c in select_cameras:
        if c is not None and c in cams:
            settings["select_camera"] = c
            break

    main_form = StandaloneFrame()

    @controller.exsafe
    def start_main_form():
        splash.update_splash_screen(msg="Connecting to the camera...")
        cam_name = settings.get("select_camera")
        if cam_name is None or cam_name not in settings["cameras"]:
            raise ValueError(f"unavailable camera {cam_name}")
        if ("css", cam_name) in settings:
            settings.update(settings["css", cam_name])
        if argvp.subconfig is not None and ("scs", argvp.subconfig) in settings:
            settings.update(settings["scs", argvp.subconfig])
        app.setStyleSheet(color_theme.load_style(settings.get("interface/color_theme", "dark")))

        cam_desc_class = camera_descriptors[settings["cameras", cam_name, "kind"]]
        cam_desc = cam_desc_class(cam_name, settings=settings["cameras", cam_name])
        plugin_manager = plugins.PluginManager(settings, ext_controller_names=_ext_controller_names)
        plugin_manager.build_plugins()
        plugin_manager.sync_plugins()
        start_threads(settings, cam_desc)
        plugin_manager.unlock_barrier("plugin_preinit")
        cam_ctl = make_device_errors_non_fatal(cam_desc.make_thread(cam_thread))
        cam_ctl.start()
        main_form.setup(
            settings=settings,
            cam_name=cam_name,
            cam_desc=cam_desc,
            plugin_manager=plugin_manager,
        )
        settings_ctl = controller.sync_controller(settings_manager_thread)
        settings_ctl.ca.update_settings("software/version", version)
        settings_ctl.ca.add_source("cam", cam_ctl.cad.get_full_info, async_result=True)
        settings_ctl.ca.add_source("cam/settings", cam_ctl.cad.get_settings, async_result=True)

        def get_cam_counters():
            counters = cam_ctl.v["frames"]
            if "last_frame" in counters:
                del counters["last_frame"]
            return counters

        settings_ctl.ca.add_source("cam/cnt", get_cam_counters)
        main_form.start()
        main_form.show()
        splash.update_splash_screen(False)
        _displayed_forms.append(main_form)

    if "select_camera" in settings:
        start_main_form()
    else:
        select_form = CamSelectFrame()
        select_form.setup(settings=settings)
        select_form.camera_selected.connect(start_main_form)
        select_form.show()
        splash.update_splash_screen(False)
        _displayed_forms.append(select_form)


### Main execution ###
def prepare_app():
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    return QtWidgets.QApplication([])


def execute(app=None):
    console = app is None
    if console:
        app = prepare_app()
    gui = controller.get_gui_controller()
    if not console:
        controller.add_exception_hook("error_message", error_display.on_error, single_call=True)
    gui.started.connect(start_app)
    app.exec_()
    error_display.check_for_error()


def main(argv=None, app=None):
    """
    Run the application.

    If `app` is given (e.g. a QApplication already created to show a splash screen), use it and
    wire up the GUI-mode exception hook; otherwise create a console-mode QApplication.
    """
    configure_logging()
    _compat.apply()
    global argvp
    argvp = _parse_args(argv)
    execute(app=app)


if __name__ == "__main__":
    main()
