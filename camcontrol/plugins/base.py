from pylablib.core.thread import controller, synchronizing
from pylablib.core.utils import files as file_utils, string as string_utils
from pylablib.core.gui.widgets import container
from pylablib.core.gui import QtWidgets, utils
from pylablib import widgets

import importlib
import os
import sys
import collections


_plugin_init_order = ["plugin_create", "plugin_preinit", "plugin_setup", "plugin_start"]


class PluginThreadController(controller.QTaskThread):
    """
    Plugin thread controller.

    Takes care of setting up and tearing down the plugin
    and provides it with means to set up jobs, commands, multicast subscriptions, etc.

    Setup args:
        - ``name``: plugin name
        - ``plugin_cls``: plugin controller class (subclass of :cls:`IPlugin`)
        - ``parameters``: additional parameters passed to the plugin on creation
        - ``ext_controller_names``: dictionary with aliases and real names of additional controllers (camera, saver, etc)
    """

    def __init__(self, name=None, args=None, kwargs=None, multicast_pool=None):
        super().__init__(
            name=name, args=args, kwargs=kwargs, multicast_pool=multicast_pool
        )
        self.plugin = None
        self.main_frame = None
        self.barriers = kwargs.pop("barriers", {})
        self._passed_barriers = 0
        self._unlocked_barriers = []

    def setup_task(self, name, plugin_cls, parameters=None, ext_controller_names=None):
        self._next_init_step("plugin_create")
        self.plugin = plugin_cls(
            name, self, parameters=parameters, ext_controller_names=ext_controller_names
        )
        self._next_init_step("plugin_preinit")
        self.plugin._sync_extctls()
        self.plugin.preinit()
        self._next_init_step("plugin_setup")
        self.plugin._sync_camctl()
        gui_ctl = self._make_manager(
            self.main_frame, "{}.{}".format(self.plugin.get_class_name(), name)
        )
        self.plugin._set_gui(gui_ctl)
        self.plugin._open()
        self._next_init_step("plugin_start")
        self.main_frame.initialize_plugin(self.plugin)  # GUI values are set here
        self._next_init_step()

    def _wait_barrier(self, name):
        if name in self.barriers:
            self.barriers[name].wait()

    def _next_init_step(self, barrier=None):
        if (
            barrier is not None
            and self._passed_barriers < len(_plugin_init_order)
            and barrier != _plugin_init_order[self._passed_barriers]
        ):
            raise ValueError(
                "expected to unlock next barrier {}; got {} instead".format(
                    _plugin_init_order[self._passed_barriers], barrier
                )
            )
        if self._passed_barriers > 0 and self._passed_barriers <= len(
            _plugin_init_order
        ):
            self.notify_exec_point(_plugin_init_order[self._passed_barriers - 1])
        if self._passed_barriers < len(_plugin_init_order):
            self._wait_barrier(_plugin_init_order[self._passed_barriers])
        self._passed_barriers += 1

    def unlock_barrier(self, name):
        """Unlock the barrier with the given name"""
        if name in self.barriers:
            self.barriers[name].notify()
        self._unlocked_barriers.append(name)

    def sync_latest_barrier(self):
        """Synchronize with all threads up to the latest unlocked barrier"""
        if self._unlocked_barriers:
            self.sync_exec_point(self._unlocked_barriers[-1])

    def set_main_frame(self, main_frame):
        """Set the main frame object; necessary to proceed past ``"plugin_setup"`` barrier"""
        self.main_frame = main_frame

    @controller.call_in_gui_thread
    def _make_manager(self, main_frame, full_name):
        manager = PluginGUIManager(main_frame)
        manager.setup(main_frame, name_prefix=full_name + "/")
        return manager

    @controller.call_in_gui_thread
    def get_all_values(self):  # called in GUI thread to avoid potential deadlocks
        """Get all plugin GUI values"""
        return self.plugin.get_all_values()

    @controller.call_in_gui_thread
    def set_all_values(self, values):
        """Set all plugin GUI values; executed in GUI thread"""
        return self.plugin.set_all_values(values)

    def get_all_indicators(self):
        """Get all plugin GUI indicators; executed in GUI thread"""
        return self.plugin.get_all_indicators()

    def is_plugin_setup(self):
        """Check if the plugin has been set up"""
        return bool(self.get_exec_counter("plugin_setup"))

    def is_plugin_running(self):
        """Check if the plugin is running"""
        return bool(self.get_exec_counter("run"))

    def finalize_task(self):
        if self.plugin is not None:
            if self.main_frame is not None:
                self.main_frame.finalize_plugin(self.plugin)
            self.plugin._close()


class PluginGUIManager(container.QContainer):
    """
    A collection of all GUI-managing objects accessible to a plugin thread

    Args:
        main_frame: main GUI :cls:`.QFrame` object
    """

    def setup(self, main_frame, name_prefix=""):
        self.main_frame = main_frame
        self.settings = main_frame.settings
        self.all_gui_values = main_frame.gui_values
        self.plot_tabs = main_frame.plots_tabs
        self.control_tabs = main_frame.control_tabs
        self.plugin_tab = main_frame.control_tabs.c["plugins"]
        self.name_prefix = name_prefix
        self._container_boxes = {}
        self.main_frame.add_child(
            self.name_prefix + "__controller__",
            self,
            gui_values_path="plugins/" + self.name_prefix,
        )

    def start(self):
        if self._running:
            return
        self._running = True
        # starting of sub-widgets is done through their normal GUI parents
        for n in self._timers:
            self.start_timer(n)

    def stop(self):
        if not self._running:
            return
        self._running = False
        # stopping of sub-widgets is done through their normal GUI parents
        for n in self._timers:
            self.stop_timer(n)

    def remove_child(self, name, clear=True):
        name = self._normalize_name(name)
        super().remove_child(name, clear=clear)
        if not clear:
            return
        gui_name = self.name_prefix + name
        if gui_name in self.control_tabs.c:
            self.control_tabs.remove_tab(gui_name)
        if gui_name in self.plot_tabs.c:
            self.plot_tabs.remove_tab(gui_name)
        if gui_name in self._container_boxes:
            box = self._container_boxes.pop(gui_name)
            self.plugin_tab.remove_layout_element(box)

    def _add_tab(
        self,
        dst,
        name,
        caption,
        kind="empty",
        index=None,
        layout="vbox",
        add_as_child=True,
        **kwargs,
    ):
        if isinstance(kind, QtWidgets.QWidget):
            widget = kind
        elif isinstance(kind, type) and issubclass(kind, QtWidgets.QWidget):
            widget = kind(self.main_frame)
        elif kind == "params":
            widget = widgets.ParamTable(self.main_frame)
            kwargs.setdefault("gui_thread_safe", True)
            kwargs.setdefault("cache_values", True)
        elif kind == "line_plot":
            widget = widgets.LinePlotter(self.main_frame)
        elif kind == "trace_plot":
            widget = widgets.TracePlotterCombined(self.main_frame)
        elif kind == "image_plot":
            widget = widgets.ImagePlotterCombined(self.main_frame)
        elif kind == "empty":
            widget = None
        else:
            raise ValueError("unrecognized tab kind: {}".format(kind))
        name = self._normalize_name(name)
        tab = dst.add_tab(
            self.name_prefix + name,
            caption,
            widget=widget,
            index=index,
            layout=layout,
            gui_values_path=False,
        )
        if add_as_child:
            self.add_child(name, tab, gui_values_path=name)
        if kind in ["params", "line_plot", "trace_plot", "image_plot"]:
            tab.setup(**kwargs)
        return tab

    def _add_box(
        self,
        dst,
        name,
        caption,
        kind="empty",
        layout="vbox",
        index=None,
        add_as_child=True,
        **kwargs,
    ):
        if isinstance(kind, QtWidgets.QWidget):
            widget = kind
        elif isinstance(kind, type) and issubclass(kind, QtWidgets.QWidget):
            widget = kind(self.main_frame)
        elif kind == "params":
            widget = widgets.ParamTable(self.main_frame)
            kwargs.setdefault("gui_thread_safe", True)
            kwargs.setdefault("cache_values", True)
        elif kind == "empty":
            widget = None
        else:
            raise ValueError("unrecognized tab kind: {}".format(kind))
        name = self._normalize_name(name)
        if dst.get_sublayout_kind() == "grid":
            if index is None:
                index = utils.get_first_empty_row(dst.get_sublayout())
            location = (index, 0, 1, "end")
        else:
            location = (-1, 0) if index is None else (index, 0)
        box = dst.add_group_box(
            self.name_prefix + name,
            caption,
            layout=layout,
            location=location,
            gui_values_path=False,
        )
        if widget is None:
            widget = box
        else:
            box.add_child("c", widget, gui_values_path=False)
        self._container_boxes[self.name_prefix + name] = box
        if add_as_child:
            self.add_child(name, widget, gui_values_path=name)
        if kind == "params":
            widget.setup(**kwargs)
        return widget

    def add_control_tab(
        self,
        name,
        caption,
        kind="params",
        index=None,
        layout="vbox",
        add_as_child=True,
        **kwargs,
    ):
        """
        Add a new tab to the control (right) tab group.

        Args:
            name: tab object name
            caption: tab caption
            kind: tab kind; can be ``"empty"`` (a simple empty :class:`.QFrameContainer` panel), ``"params"`` (:class:`.ParamTable` panel),
                an already created widget, or a widget class (which is instantiated upon addition)
            index: index of the new tab; add to the end by default
            layout: if `kind` is ``"empty"``, specifies the layout of the new tab
            kwargs: keyword arguments passed to the widget ``setup`` method when ``kind=="params"``
        """
        return self._add_tab(
            self.control_tabs,
            name,
            caption,
            kind=kind,
            index=index,
            layout=layout,
            add_as_child=add_as_child,
            **kwargs,
        )

    def add_plot_tab(
        self,
        name,
        caption,
        kind="image_plot",
        index=None,
        layout="vbox",
        add_as_child=True,
        **kwargs,
    ):
        """
        Add a new tab to the plot (left) tab group.

        Args:
            name: tab object name
            caption: tab caption
            kind: tab kind; can be ``"empty"`` (a simple empty :class:`.QFrameContainer` panel), ``"params"`` (:class:`.ParamTable` panel),
                ``"line_plot"`` (a simple :class:`.LinePlotter` plotter), ``"trace_plot"`` (a more advanced :class:`.TracePlotterCombined` plotter),
                ``"image_plot"`` (a standard :class:`.ImagePlotterCombined` plotter),
                an already created widget, or a widget class (which is instantiated upon addition)
            index: index of the new tab; add to the end by default
            layout: if `kind` is ``"empty"``, specifies the layout of the new tab
            kwargs: keyword arguments passed to the widget ``setup`` method when the new tab is a parameter table or a plotter
        """
        return self._add_tab(
            self.plot_tabs,
            name,
            caption,
            kind=kind,
            index=index,
            layout=layout,
            add_as_child=add_as_child,
            **kwargs,
        )

    def add_plugin_box(
        self,
        name,
        caption,
        kind="params",
        layout="vbox",
        index=None,
        add_as_child=True,
        parent=None,
        **kwargs,
    ):
        """
        Add a new box to the plugins tab.

        Args:
            name: box object name
            caption: box caption
            kind: tab kind; can be ``"empty"`` (a simple empty :class:`.QFrameContainer` panel), ``"params"`` (:class:`.ParamTable` panel),
                an already created widget, or a widget class (which is instantiated upon addition)
            layout: if `kind` is ``"empty"``, specifies the layout of the new tab
            kwargs: keyword arguments passed to the widget ``setup`` method when ``kind=="params"``
        """
        return self._add_box(
            parent or self.plugin_tab,
            name,
            caption,
            kind=kind,
            layout=layout,
            index=index,
            add_as_child=add_as_child,
            **kwargs,
        )


class IPlugin:
    """
    A base class for a plugin.

    Provides some supporting code, basic implementation of some methods, and some helpful methods.

    Attributes which can be used in implementation:
        ctl: plugin thread controller (instance of :cls:`PluginThreadController`);
            used for threading activity such as setting up jobs, commands, subscribing to signals, etc.
        guictl: main (GUI) thread controller;
            used mainly for calling predefined thread methods (which access widgets, and therefore automatically execute in GUI thread)
        gui: GUI controller (instance of :cls:`PluginGUIManager`);
            used to set up GUI controls, e.g., add plotting or control tabs, or control boxes for small plugins
        extctls: dictionary of controller for additional thread;
            used to further access different parts of the system;
            threads include ``"camera"`` (camera thread), ``"saver"`` (main saver thread), ``"snap_saver"`` (snapshot saver thread),
            ``"processor"`` (default frame processing thread), ``'preprocessor"`` (frame preprocessor thread), ``"plot_accumulator`` (main plot accumulator thread)

    Args:
        name: plugin instance name
        ctl: plugin thread controller (instance of :cls:`PluginThreadController`)
        gui: plugin GUI manager
        parameters: additional parameters supplied in the settings file (``None`` of no parameters)
        ext_controller_names: dictionary with external controller names, which ties purposes (such as ``"camera"`` or ``"saver"``) with the controller names
    """

    def __init__(self, name, ctl, parameters=None, ext_controller_names=None):
        self.name = name
        self.full_name = "{}.{}".format(self.get_class_name(), self.name)
        self.ctl = ctl
        self.ca = self.ctl.ca
        self.cs = self.ctl.cs
        self.csi = self.ctl.csi
        self.guictl = controller.get_gui_controller()
        self.gui = None
        self.extctl_names = ext_controller_names or {}
        self.extctls = None
        self.parameters = parameters or {}
        self._opened = False
        self._running = False
        self._gui_started = False

    _class_name = None  # default class name (by default, the class name)
    _class_caption = None  # default class caption (by default, same as name)
    _default_start_order = 0  # default starting order for plugins of this class

    @classmethod
    def get_class_name(cls, kind="name"):
        """
        Get plugin class name.

        `kind` can be ``"name"`` (code-friendly identifiers to use in, e.g., settings file)
        or ``"caption"`` (formatted name to be used in GUI lists, etc.)
        """
        if kind == "name":
            if cls._class_name is not None:
                return cls._class_name
            return cls.__name__
        elif kind == "caption":
            if cls._class_caption is not None:
                return cls._class_caption
            return cls.get_class_name(kind="name")

    def get_instance_name(self, kind="name"):
        """Get plugin instance name"""
        if kind == "name":
            return self.name

    def preinit(self):
        """
        Pre-initialize plugin.

        Called after all auxiliary threads are started, but before the camera thread is started.

        To be overloaded.
        Executed in the plugin thread.
        """

    def setup(self):
        """
        Setup plugin (define attributes, jobs, etc).

        To be overloaded.
        Executed in the plugin thread.
        """

    def cleanup(self):
        """
        Cleanup plugin (close handlers, etc).

        Called upon the plugin unloading, but only if it reached the setup point (i.e., the ``setup`` method has executed successfully)

        To be overloaded.
        Executed in the plugin thread.
        """

    def postcleanup(self):
        """
        Post-cleanup plugin.

        Called after ``cleanup`` unconditionally (i.e., even if the ``setup`` did not run).

        To be overloaded.
        Executed in the plugin thread.
        """

    def _set_gui(self, gui):
        self.gui = gui

    def _sync_extctls(self):
        self.extctls = {
            a: controller.sync_controller(n)
            for a, n in self.extctl_names.items()
            if a != "camera"
        }

    def _sync_camctl(self):
        if "camera" in self.extctl_names:
            self.extctls["camera"] = controller.sync_controller(
                self.extctl_names["camera"]
            )

    def _open(self):
        self._opened = True
        self.setup()
        self._running = True

    def _close(self):
        self._running = False
        if self._opened:
            self.cleanup()
            if self.gui is not None:
                controller.call_in_gui_thread(self.gui.clear)()
        self._opened = False
        self.postcleanup()

    def is_running(self):
        """Check if the plugin is still running"""
        return self._running

    @controller.exsafe
    def exit(self):
        """Stop the plugin thread and close the plugin"""
        self.ctl.stop()

    @controller.call_in_gui_thread
    def setup_gui_sync(self):
        """Setup GUI (simply calls :meth:`setup_gui` in the GUI thread)"""
        self.setup_gui()

    def setup_gui(self):
        """
        Setup GUI.

        To be overloaded.
        Executed in the plugin thread, if called via :meth:`setup_gui_sync` method.
        """

    def get_all_values(self):
        """
        Get all GUI values.

        Can be overloaded.
        Executed in the GUI thread.
        """
        return self.gui.get_all_values()

    def set_all_values(self, values):
        """
        Set all GUI values.

        Can be overloaded.
        Executed in the GUI thread.
        """
        self.gui.set_all_values(values)

    def get_all_indicators(self):
        """
        Get all GUI indicators as a dictionary.

        Can be overloaded.
        Executed in the GUI thread.
        """
        return self.gui.get_all_indicators()

    def start_gui(self):
        """
        Start GUI operation.

        Called after all GUI values are set.
        Can be overloaded.
        Executed in the GUI thread.
        """
        self._gui_started = True


class PluginManager:
    """
    Plugin manager.

    Controls plugins creation, initialization, and finalization according the their start order.

    Args:
        settings: settings dictionary with the possible ``"plugins"`` branch describing the plugins
        ext_controller_names: dictionary with external controller names, which ties purposes (such as ``"camera"`` or ``"saver"``) with the controller names
    """

    def __init__(self, settings, ext_controller_names=None):
        self.settings = settings
        extra_dir = os.path.join(settings.get("runtime/root_folder", default=""), "plugins")
        self.plugin_classes = {
            p.get_class_name(): p for p in find_plugins(extra_dir=extra_dir)
        }
        self._running_plugins = {}
        self._ext_controller_names = ext_controller_names

    def _ordered_plugins(self):
        return sorted(self._running_plugins.items(), key=lambda v: v[1].start_order)

    def sync_plugins(self):
        for plugin in self._running_plugins.values():
            plugin.ctl.sync_latest_barrier()

    def unlock_barrier(self, name):
        """Unlock the barrier with the given name to allow the plugins to proceed with the next initialization stage"""
        last_order = None
        for _, plugin in self._ordered_plugins():
            if last_order is not None and plugin.start_order != last_order:
                self.sync_plugins()
            plugin.ctl.unlock_barrier(name)
            last_order = plugin.start_order

    def build_plugins(self):
        """Start all plugin threads described in the configuration file"""
        plugins_list = []
        if "plugins" in self.settings:
            for p in sorted(self.settings["plugins"]):
                if "class" in self.settings["plugins", p]:
                    class_name = self.settings["plugins", p, "class"]
                    name = self.settings.get(("plugins", p, "name"), p)
                    parameters = self.settings.get(("plugins", p, "parameters"), None)
                    plugin_class = self.plugin_classes[class_name]
                    start_order = self.settings.get(
                        ("plugins", p, "start_order"), plugin_class._default_start_order
                    )
                    plugins_list.append((plugin_class, name, parameters, start_order))
        plugins_list.sort(key=lambda v: v[-1])
        for plugin_class, name, parameters, start_order in plugins_list:
            self.start_plugin(
                plugin_class, name=name, parameters=parameters, start_order=start_order
            )
        self.unlock_barrier("plugin_create")

    def set_main_frame(self, main_frame):
        """Set the main GUI frame object for the plugins"""
        for plugin in self._running_plugins.values():
            plugin.ctl.set_main_frame(main_frame)

    PluginInfo = collections.namedtuple("PluginInfo", ("ctl", "start_order"))

    @controller.call_in_gui_thread
    def start_plugin(
        self,
        plugin_class,
        name="__default__",
        parameters=None,
        start_order=0,
        barriers=None,
    ):
        """
        Start plugin thread.

        Args:
            plugin_class: class of the plugin (subclass of :cls:`.IPlugin`)
            name: plugin name (for the cases of several plugins of the same class)
            parameters: additional plugin parameters
            start_order: start order among other plugins at the given stage
            barriers: list of barriers to include in the initialization (by default, all of them)
        """
        full_name = plugin_class.get_class_name(), name
        if full_name in self._running_plugins:
            raise RuntimeError("plugin {}.{} is already running".format(*full_name))
        if barriers is None:
            barriers = _plugin_init_order
        barriers = {n: synchronizing.QThreadNotifier() for n in barriers}
        plugin_ctl = PluginThreadController(
            "plugin.{}.{}".format(*full_name),
            kwargs={
                "name": name,
                "barriers": barriers,
                "plugin_cls": plugin_class,
                "ext_controller_names": self._ext_controller_names,
                "parameters": parameters,
            },
        )
        self._running_plugins[full_name] = self.PluginInfo(plugin_ctl, start_order)
        plugin_ctl.start()

    def remove_plugin(self, name):
        """Remove the plugin with the given name from the list"""
        del self._running_plugins[name]

    def get_running_plugins(self, ordered=False):
        """
        Get all running plugins.

        If ``ordered==False``, return the dictionary ``{name: (plugin_ctl,start_order)}`` of the plugin controllers;
        otherwise, return a list of corresponding tuples ``[(name, (plugin_ctl,start_order))]`` ordered according to the start order.
        """
        if ordered:
            return self._ordered_plugins()
        return self._running_plugins.copy()


root_module_name = __name__.rsplit(".", maxsplit=1)[0]


def _load_modules(directory, namespace):
    """
    Dynamically import every ``.py`` file under `directory` (recursively), registering each
    under ``<namespace>.<relative dotted path>`` in :data:`sys.modules` (reusing an existing
    entry of that name if one is already loaded).

    A module that fails to import (e.g., because an optional vendor dependency is missing)
    is skipped with a warning instead of aborting discovery of the rest.

    Return the list of module names this call covers (whether freshly loaded or already cached).
    """
    module_names = []
    if not os.path.isdir(directory):
        return module_names
    files = file_utils.list_dir_recursive(
        directory,
        file_filter=r".*\.py$",
        visit_folder_filter=string_utils.get_string_filter(exclude="__pycache__"),
    ).files
    for f in files:
        if f == "__init__.py":
            continue
        rel = os.path.splitext(f)[0].replace("\\", ".").replace("/", ".")
        module_name = "{}.{}".format(namespace, rel)
        module_names.append(module_name)
        if module_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(module_name, os.path.join(directory, f))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            print("Could not load module {}: {}".format(module_name, e), file=sys.stderr)
            module_names.remove(module_name)
            continue
        sys.modules[module_name] = mod
    return module_names


def find_plugins(extra_dir=None):
    """
    Find all plugin classes.

    Scans the plugins bundled with this package. If `extra_dir` is given and exists (and is
    not the bundled plugins directory itself, e.g. when running from a source checkout), it is
    also scanned for user-supplied plugins -- this is the hook for adding custom plugins
    without modifying the package, typically a ``plugins`` folder placed next to the settings
    file.

    Plugin class is any subclass of :cls:`IPlugin` which is not :cls:`IPlugin` itself.
    """
    builtin_dir = os.path.dirname(__file__)
    _load_modules(builtin_dir, root_module_name)
    if extra_dir and os.path.isdir(extra_dir):
        if os.path.realpath(extra_dir) != os.path.realpath(builtin_dir):
            _load_modules(extra_dir, root_module_name + ".external")
    plugins = []
    for module_name in list(sys.modules):
        if module_name == root_module_name or module_name.startswith(root_module_name + "."):
            mod = sys.modules[module_name]
            for v in mod.__dict__.values():
                if isinstance(v, type) and issubclass(v, IPlugin) and v is not IPlugin:
                    plugins.append(v)
    return plugins
