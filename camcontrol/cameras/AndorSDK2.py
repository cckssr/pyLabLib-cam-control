from pylablib.devices import Andor
from pylablib.thread.devices.Andor import (
    AndorSDK2CameraThread,
    AndorSDK2LucaThread,
    AndorSDK2IXONThread,
)

from .base import ICameraDescriptor
from ..gui import cam_gui_parameters
from ..gui.base_cam_ctl_gui import GenericCameraSettings_GUI, GenericCameraStatus_GUI


class AmpModeParameter(cam_gui_parameters.IGUIParameter):
    def __init__(self, settings):
        super().__init__(settings)
        self.disabled = False
        self.amp_modes = None
        self.curr_mode = [None] * 3

    def add(self, base):
        self.base = base
        base.add_combo_box("channel", label="Channel")
        base.add_combo_box(
            "oamp", label="Output amplifier", location={"indicator": "next_line"}
        )
        base.add_combo_box("hsspeed", label="Horiz. scan speed")
        base.add_combo_box("preamp", label="Preamp gain", options={1: "1.0"})
        self.connect_updater(["channel", "oamp", "hsspeed", "preamp"])

    def _update_options(self, widget, options):
        if widget.get_options_dict() != options:
            widget.set_options(options)

    def _display_amp_modes(self, amp_mode, update_control=False):
        valid_amp_modes = self.amp_modes
        self._update_options(
            self.base.w["channel"],
            {am[0]: "{}: {}bit".format(am[0], am[1]) for am in valid_amp_modes},
        )
        self.base.i["channel"] = amp_mode[0]
        valid_amp_modes = [am for am in valid_amp_modes if am[0] == amp_mode[0]]
        self._update_options(
            self.base.w["oamp"], {am[2]: am[3] for am in valid_amp_modes}
        )
        self.base.i["oamp"] = amp_mode[1]
        valid_amp_modes = [am for am in valid_amp_modes if am[2] == amp_mode[1]]
        self._update_options(
            self.base.w["hsspeed"],
            {am[4]: "{:.1f} MHz".format(am[5]) for am in valid_amp_modes},
        )
        self.base.i["hsspeed"] = amp_mode[2]
        valid_amp_modes = [am for am in valid_amp_modes if am[4] == amp_mode[2]]
        self._update_options(
            self.base.w["preamp"],
            {am[6]: "{:.1f}".format(am[7]) for am in valid_amp_modes},
        )
        self.base.i["preamp"] = amp_mode[3]
        self.curr_mode = amp_mode
        if update_control:
            (
                self.base.v["channel"],
                self.base.v["oamp"],
                self.base.v["hsspeed"],
                self.base.v["preamp"],
            ) = amp_mode

    def setup(self, parameters, full_info):
        super().setup(parameters, full_info)
        if "amp_modes" not in full_info:
            self.base.set_enabled(["channel", "oamp", "hsspeed", "preamp"], False)
            return
        self.amp_modes = full_info["amp_modes"]
        self._display_amp_modes(
            [self.amp_modes[0][i] for i in [0, 2, 4, 6]], update_control=True
        )

    def collect(self, parameters):
        if not self.disabled:
            for n in ["channel", "oamp", "hsspeed", "preamp"]:
                v = self.base.v[n]
                if v != -1:
                    parameters[n] = v
        return super().collect(parameters)

    def display(self, parameters):
        if self.disabled:
            return
        amp_mode = [
            (parameters[n] if n in parameters else self.base.i[n])
            for n in ["channel", "oamp", "hsspeed", "preamp"]
        ]
        self._display_amp_modes(amp_mode)


class VSSpeedParameter(cam_gui_parameters.EnumGUIParameter):
    """
    Andor SDK2 vertical speed parameter.

    Receives possible values from the camera.
    """

    def __init__(self, settings):
        super().__init__(settings, "vsspeed", "Vert. shift period", {})

    def setup(self, parameters, full_info):
        super().setup(parameters, full_info)
        if "vsspeeds" in full_info:
            vsspeeds = {
                k: "{:.1f} us".format(v) for k, v in enumerate(full_info["vsspeeds"])
            }
            self.base.w[self.gui_name].set_options(vsspeeds, index=0)
        else:
            self.disable()


class TemperatureParameter(cam_gui_parameters.IntGUIParameter):
    """
    Andor SDK2 temperature parameter.

    Receives range of values from the camera.
    """

    def __init__(self, settings):
        super().__init__(settings, "temperature", "Temperature (C)")

    def setup(self, parameters, full_info):
        super().setup(parameters, full_info)
        if "temperature_range" in full_info:
            rng = full_info["temperature_range"]
            self.base.w[self.gui_name].set_limiter(rng + ("coerce", "int"))
            self.base.v[self.gui_name] = min(rng[0] + 20, (rng[0] + rng[1]) // 2)


class Settings_GUI(GenericCameraSettings_GUI):
    _bin_kind = "both"
    _frame_period_kind = "value"

    def get_basic_parameters(self, name):
        if name == "shutter":
            return cam_gui_parameters.EnumGUIParameter(
                self,
                "shutter",
                "Shutter",
                {"open": "Opened", "closed": "Closed", "auto": "Auto"},
                default="closed",
                from_camera=lambda v: v[0],
            )
        if name == "frame_transfer":
            return cam_gui_parameters.BoolGUIParameter(
                self, "frame_transfer", "Frame transfer mode"
            )
        if name == "amp_mode":
            return AmpModeParameter(self)
        if name == "vsspeed":
            return VSSpeedParameter(self)
        if name == "EMCCD_gain":
            return cam_gui_parameters.IntGUIParameter(
                self,
                "EMCCD_gain",
                "EMCCD gain",
                limit=(0, 255),
                to_camera=lambda v: (v, False),
                from_camera=lambda v: v[0],
            )
        if name == "fan_mode":
            return cam_gui_parameters.EnumGUIParameter(
                self, "fan_mode", "Fan", {"off": "Off", "low": "Low", "full": "Full"}
            )
        if name == "cooler":
            return cam_gui_parameters.BoolGUIParameter(
                self, "cooler", "Cooler", default=True
            )
        if name == "temperature":
            return TemperatureParameter(self)
        return super().get_basic_parameters(name)

    def setup_settings_tables(self):
        super().setup_settings_tables()
        self.add_builtin_parameter("shutter", "common", row=0)
        self.add_builtin_parameter("frame_transfer", "advanced")
        self.add_builtin_parameter("amp_mode", "advanced")
        self.add_builtin_parameter("vsspeed", "advanced")
        self.add_builtin_parameter("EMCCD_gain", "advanced")
        self.add_builtin_parameter("fan_mode", "advanced")
        self.add_builtin_parameter("cooler", "advanced")
        self.add_builtin_parameter("temperature", "advanced")


class Status_GUI(GenericCameraStatus_GUI):
    def setup_status_table(self):
        self.add_text_label("temperature_status", label="Temperature status:")
        self.add_num_label(
            "temperature_monitor",
            formatter=("float", "auto", 1, True),
            label="Temperature (C):",
        )

    def show_parameters(self, params):
        super().show_parameters(params)
        if "temperature_monitor" in params:
            self.v["temperature_monitor"] = params["temperature_monitor"]
        temp_status_text = {
            "off": "Cooler off",
            "not_reached": "Approaching...",
            "not_stabilized": "Stabilizing...",
            "drifted": "Drifted",
            "stabilized": "Stable",
        }
        if "temperature_status" in params:
            self.v["temperature_status"] = temp_status_text[
                params["temperature_status"]
            ]


class AndorSDK2CameraDescriptor(ICameraDescriptor):
    _cam_kind = "AndorSDK2"

    @classmethod
    def iterate_cameras(cls, verbose=False):
        if verbose:
            print("Searching for Andor SDK2 cameras")
        try:
            cam_num = Andor.get_cameras_number_SDK2()
        except (Andor.AndorError, OSError):
            if verbose:
                print(
                    "Error loading or running the Andor SDK2 library: required software (Andor Solis) must be missing\n"
                )
            if verbose == "full":
                cls.print_error()
            return
        if not cam_num:
            if verbose:
                print("Found no Andor SDK2 cameras\n")
            return
        if verbose:
            print(
                "Found {} Andor SDK2 camera{}".format(
                    cam_num, "s" if cam_num > 1 else ""
                )
            )
        for i in range(cam_num):
            try:
                if verbose:
                    print("Found Andor SDK2 camera idx={}".format(i))
                with Andor.AndorSDK2Camera(idx=i) as cam:
                    device_info = cam.get_device_info()
                    if verbose:
                        print("\tModel {}".format(device_info.head_model))
                    yield cam, None
            except Andor.AndorError:
                if verbose == "full":
                    cls.print_error()

    @classmethod
    def generate_description(cls, idx, cam=None, info=None):
        device_info = cam.get_device_info()
        cam_desc = cls.build_cam_desc(params={"idx": idx})
        cam_desc["display_name"] = "Andor {} {}".format(
            device_info.head_model, device_info.serial_number
        )
        cam_name = "andor_sdk2_{}".format(idx)
        return cam_name, cam_desc

    def get_kind_name(self):
        return "Generic Andor SDK2"

    def make_thread(self, name):
        return AndorSDK2CameraThread(
            name=name, kwargs=self.settings["params"].as_dict()
        )

    def make_gui_control(self, parent):
        return Settings_GUI(parent, cam_desc=self)

    def make_gui_status(self, parent):
        return Status_GUI(parent, cam_desc=self)


class AndorSDK2IXONCameraDescriptor(AndorSDK2CameraDescriptor):
    _cam_kind = "AndorSDK2IXON"
    _expands = "AndorSDK2"

    @classmethod
    def generate_description(cls, idx, cam=None, info=None):
        if cam.get_capabilities()["cam_type"] == "AC_CAMERATYPE_IXON":
            return super().generate_description(idx, cam=cam, info=info)

    def get_kind_name(self):
        return "Andor iXON"


class AndorSDK2LucaCameraDescriptor(AndorSDK2CameraDescriptor):
    _cam_kind = "AndorSDK2Luca"
    _expands = "AndorSDK2"

    @classmethod
    def generate_description(cls, idx, cam=None, info=None):
        if cam.get_capabilities()["cam_type"] == "AC_CAMERATYPE_LUCA":
            return super().generate_description(idx, cam=cam, info=info)

    def get_kind_name(self):
        return "Andor Luca"
