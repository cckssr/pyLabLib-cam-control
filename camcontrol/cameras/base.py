import sys
import traceback

from pylablib.core.utils import dictionary


class ICameraDescriptor:
    """
    Base camera descriptor.

    Includes method to detect cameras of the given type, start threads, and create GUI (settings control and status display).
    """

    _cam_kind = None
    _expands = None

    def __init__(self, name, settings=None):
        self.name = name
        self.settings = settings or {}

    @classmethod
    def print_added_camera(cls, name, desc):
        """Print information about a newly detected camera"""
        print(f"Adding camera under name {name}:")
        print("\tkind = '{}'".format(desc["kind"]))
        if "params" in desc:
            print("\tparams = '{}'".format(desc["params"].as_dict()))
        if "display_name" in desc:
            print("\tdisplay_name = '{}'".format(desc["display_name"]))
        print("")

    @classmethod
    def print_skipped_camera(cls):
        """Print information about a skipped camera"""
        print("Skipping the camera\n")

    @classmethod
    def print_error(cls):
        """Print an exception traceback"""
        traceback.print_exc()
        print("", file=sys.stderr)

    @classmethod
    def iterate_cameras(cls, verbose=False):
        """Iterate over all cameras of the given type"""
        raise NotImplementedError

    @classmethod
    def generate_description(cls, idx, cam=None, info=None):
        """
        Return camera description dictionary for the given camera index, camera class, and additional info.

        Return either tuple ``(cam_name, cam_desc)`` or ``None`` (camera can not be added).
        """

    @classmethod
    def build_cam_desc(cls, params, cam_kind=None):
        return dictionary.Dictionary({"kind": cam_kind or cls._cam_kind, "params": params or {}})

    @classmethod
    def can_expand(cls, cam_kind):
        return cls._expands == cam_kind

    @classmethod
    def find_description(cls, idx, cam=None, info=None, camera_descriptors=None):
        """
        Find the most specific description for the given camera index, camera class, and additional info.

        Return tuple ``(cls, desc)``, where ``desc`` is the result of :meth:`generate_description` call
        and ``cls`` is the class which generated this description.
        """
        desc = cls.generate_description(idx, cam=cam, info=info)
        if desc and camera_descriptors:
            for d in camera_descriptors:
                if d.can_expand(cls._cam_kind):
                    exp_cls, exp_desc = d.find_description(
                        idx, cam=cam, info=info, camera_descriptors=camera_descriptors
                    )
                    if exp_desc is not None:
                        return exp_cls, exp_desc
        return cls, desc

    @classmethod
    def detect(cls, verbose=False, camera_descriptors=None):
        """Detect all cameras of the given type"""
        cameras = dictionary.Dictionary()
        for i, (cam, info) in enumerate(cls.iterate_cameras(verbose=verbose)):
            desc_cls, desc = cls.find_description(
                i, cam=cam, info=info, camera_descriptors=camera_descriptors
            )
            if desc is not None and desc[0] is not None:
                if verbose:
                    desc_cls.print_added_camera(*desc)
                cameras[desc[0]] = desc[1]
            else:
                if verbose:
                    desc_cls.print_skipped_camera()
        return cameras

    def get_kind_name(self):
        """Get user-friendly name to be displayed in the GUI"""
        raise NotImplementedError

    def get_camera_labels(self):
        """Get label ``(kind_name, cam_name)``"""
        return self.get_kind_name(), self.settings.get("display_name", self.name)

    @classmethod
    def get_class_settings(cls):
        """Get dictionary with generic class settings"""
        return dictionary.Dictionary({"gc/enabled": True})

    def get_control_settings(self):
        """Get dictionary with specific control settings (same structure as :meth:`get_class_settings`, but can be expanded in the settings file)"""
        settings = self.get_class_settings()
        return settings.updated(self.settings.get("control", {}))

    def make_thread(self, name):
        """Create camera thread with the given name"""
        raise NotImplementedError

    def make_gui_control(self, parent):
        """Create GUI settings control with the given parent widget"""
        raise NotImplementedError

    def make_gui_status(self, parent):
        """Create GUI status table with the given parent widget"""
        raise NotImplementedError


class DeviceErrorTolerantThreadMixin:
    """
    Mixin for camera device threads: a device error closes the camera instead of the application.

    pylablib stops the whole application on any exception in a thread controller. With this
    mixin, a device error (``self.DeviceError``, e.g. a camera that was unplugged) raised
    while acquiring, polling or applying parameters ends the acquisition, is printed to the
    error log and stored in the ``device_error`` thread variable, and the camera is closed
    ("Disconnected"). The thread's periodic parameter update then keeps trying to reopen it,
    so a camera that comes back reconnects by itself. Device calls made on behalf of other
    threads (``get_full_info``, ``_device_method``) are covered the same way and return
    nothing. Use :func:`make_device_errors_non_fatal` to apply it to an existing thread object.
    """

    _failed_acquisition_error = None

    def _on_device_error(self, err):
        msg = f"{type(err).__name__}: {err}"
        print(f"Camera '{self.name}' error, disconnecting: {msg}", file=sys.stderr)
        self.v["device_error"] = msg
        # Queued as a command rather than called here: this may run inside the acquisition
        # job, which closing the device would have to stop and finalize re-entrantly
        try:
            self.ca.close()
        except Exception as exc:  # e.g. the thread is already shutting down (and closing)
            print(f"Camera '{self.name}': could not schedule closing: {exc!r}", file=sys.stderr)

    def setup_open_device(self):
        result = super().setup_open_device()
        self.v["device_error"] = None
        return result

    def acq_loop(self, name, **kwargs):
        try:
            yield from super().acq_loop(name, **kwargs)
        except self.DeviceError as err:
            self._failed_acquisition_error = err  # reported once the loop is finalized

    def acq_finalize(self, name, **kwargs):
        err, self._failed_acquisition_error = self._failed_acquisition_error, None
        if err is None:
            try:
                return super().acq_finalize(name, **kwargs)
            except self.DeviceError as exc:
                err = exc
        # the device is broken: skip the regular finalization, which would talk to it
        self._running_loop = None
        self._reset_frame_counters()
        self._set_acquisition_status("stopped")
        self._on_device_error(err)

    def update_parameters(self):
        try:
            return super().update_parameters()
        except self.DeviceError as err:
            self._on_device_error(err)
            parameters = self._get_disconnected_parameters()
            self.set_variable("parameters", parameters, update=True, notify=True)
            return parameters

    def apply_parameters(self, *args, **kwargs):
        try:
            return super().apply_parameters(*args, **kwargs)
        except self.DeviceError as err:
            self._on_device_error(err)

    def get_full_info(self, *args, **kwargs):
        try:
            return super().get_full_info(*args, **kwargs)
        except self.DeviceError as err:
            self._on_device_error(err)
            return dictionary.Dictionary()

    def _device_method(self, *args, **kwargs):
        try:
            return super()._device_method(*args, **kwargs)
        except self.DeviceError as err:
            self._on_device_error(err)
            return None

    def close(self, *args, **kwargs):
        try:
            return super().close(*args, **kwargs)
        except self.DeviceError as err:
            # a broken device may fail to close cleanly; it is gone either way
            print(f"Camera '{self.name}' error while closing: {err}", file=sys.stderr)
            self.update_status("connection", "closed", "Disconnected")


# Methods pylablib's ``DeviceThread.__init__`` registers as thread commands; the registration
# binds the method of the class the thread had at that point
_COMMANDS_BOUND_AT_INIT = (
    "update_parameters",
    "apply_parameters",
    "get_full_info",
    "_device_method",
    "close",
)


def make_device_errors_non_fatal(thread):
    """Give a (not yet started) camera thread object the :class:`DeviceErrorTolerantThreadMixin`"""
    cls = type(thread)
    if issubclass(cls, DeviceErrorTolerantThreadMixin):
        return thread
    thread.__class__ = type(cls.__name__, (DeviceErrorTolerantThreadMixin, cls), {})
    # The thread's constructor already registered some methods as commands, bound to the
    # original class; a queued call to one of them would bypass the mixin, so rebind them
    for name in _COMMANDS_BOUND_AT_INIT:
        command = thread._commands.get(name)
        if command is None:
            continue
        thread.__dict__.pop(name, None)  # the per-instance wrapper made at registration
        thread._commands[name] = command._replace(command=getattr(thread, name))
        thread._override_command_method(name)
    return thread
