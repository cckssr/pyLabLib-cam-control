from pylablib.devices import Thorlabs
from pylablib.thread.devices.Thorlabs import ThorlabsTLCameraThread

from .base import ICameraDescriptor
from ..gui import cam_gui_parameters
from ..gui.base_cam_ctl_gui import GenericCameraSettings_GUI, GenericCameraStatus_GUI


class GainParameter(cam_gui_parameters.FloatGUIParameter):
    """
    ThorlabsTLCam gain parameter.

    Receives values range from the camera.
    """

    def __init__(self, settings):
        super().__init__(settings, "gain", "Gain (dB)")

    def setup(self, parameters, full_info):
        super().setup(parameters, full_info)
        if "gain_range" in full_info:
            rng = full_info["gain_range"]
            self.base.w[self.gui_name].set_limiter(rng)


class Settings_GUI(GenericCameraSettings_GUI):
    _bin_kind = "both"
    _frame_period_kind = "value"

    def get_basic_parameters(self, name):
        if name == "gain":
            return GainParameter(self)
        return super().get_basic_parameters(name)

    def setup_settings_tables(self):
        super().setup_settings_tables()
        self.add_builtin_parameter("gain", "advanced")


class ThorlabsTLCamCameraDescriptor(ICameraDescriptor):
    _cam_kind = "ThorlabsTLCam"

    @classmethod
    def iterate_cameras(cls, verbose=False):
        if verbose:
            print("Searching for Thorlabs TSI cameras")
        try:
            cam_infos = Thorlabs.list_cameras_tlcam()
        except (Thorlabs.ThorlabsTLCameraError, OSError):
            if verbose:
                print(
                    "Error loading or running the Thorlabs TSI library: required software (ThorCam) must be missing\n"
                )
            if verbose == "full":
                cls.print_error()
            return
        cam_num = len(cam_infos)
        if not cam_num:
            if verbose:
                print("Found no Thorlabs TLCam cameras\n")
            return
        if verbose:
            print(
                "Found {} Thorlabs TLCam camera{}".format(
                    cam_num, "s" if cam_num > 1 else ""
                )
            )
        for serial in cam_infos:
            try:
                if verbose:
                    print("Found Thorlabs TSI camera serial={}".format(serial))
                with Thorlabs.ThorlabsTLCamera(serial) as cam:
                    yield cam, serial
            except Thorlabs.ThorlabsTLCameraError:
                if verbose == "full":
                    cls.print_error()

    @classmethod
    def generate_description(cls, idx, cam=None, info=None):
        device_info = cam.get_device_info()
        cam_desc = cls.build_cam_desc(params={"serial": info})
        cam_desc["display_name"] = "{} {}".format(
            device_info.model, device_info.serial_number
        )
        cam_name = "thorlabs_tlcam_{}".format(idx)
        return cam_name, cam_desc

    def get_kind_name(self):
        return "Thorlabs Scientific Camera"

    def make_thread(self, name):
        return ThorlabsTLCameraThread(
            name=name, kwargs=self.settings["params"].as_dict()
        )

    def make_gui_control(self, parent):
        return Settings_GUI(parent, cam_desc=self)

    def make_gui_status(self, parent):
        return GenericCameraStatus_GUI(parent, cam_desc=self)
