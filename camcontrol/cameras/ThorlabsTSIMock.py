"""Emulated Thorlabs TSI camera, running the full ``ThorlabsTSIDirect`` stack without hardware.

The camera is a ``camcontrol.thorlabs_tsi.fake.FakeTsiDevice``: it answers the camera's
ASCII command protocol and streams correctly packed frames, so the vendored driver, the
``ThorlabsTSIDirect`` adapter and its GUI all run unmodified on top of it. Never
autodetected; add it to ``settings.cfg`` by hand, e.g.::

    cameras/tsi_mock/kind	ThorlabsTSIMock
    cameras/tsi_mock/display_name	"Emulated CS165CU"
    cameras/tsi_mock/params/family	zelux
    cameras/tsi_mock/params/color	True

Parameters (all optional): ``family`` (``zelux`` or ``cs``), ``color``, ``model``,
``serial``, ``sensor`` (``(width, height)``), and ``faults``, a section of
``FakeFaults`` fields (e.g. ``params/faults/drop_chunk_every 50``).
"""

import functools

from ..thorlabs_tsi.camera import TsiCamera
from ..thorlabs_tsi.fake import FakeFaults, FakeTsiBus, FakeTsiDevice
from .base import ICameraDescriptor
from .ThorlabsTSIDirect import Settings_GUI, Status_GUI, ThorlabsTSICamera, ThorlabsTSICameraThread


class ThorlabsTSIMockCameraThread(ThorlabsTSICameraThread):
    """Device thread for an emulated Thorlabs TSI camera"""

    def make_device(self):
        factory = functools.partial(
            TsiCamera.open_camera, find=self.bus.find, synchronous_stream=True
        )
        return ThorlabsTSICamera(serial=self.serial, camera_factory=factory)

    def setup_task(  # pylint: disable=arguments-differ
        self,
        family="zelux",
        color=False,
        model=None,
        serial="00001",
        sensor=None,
        faults=None,
        remote=None,
        misc=None,
    ):
        # One device for the thread's lifetime, so its state survives reconnects (and an
        # injected disconnect stays disconnected, as an unplugged camera would)
        self.bus = FakeTsiBus(
            [
                FakeTsiDevice(
                    family=family,
                    serial=str(serial),
                    model=model,
                    sensor=tuple(sensor) if sensor else None,
                    color=bool(color),
                    faults=FakeFaults.from_dict(faults),
                )
            ]
        )
        super().setup_task(serial=str(serial), remote=remote, misc=misc)


class ThorlabsTSIMockCameraDescriptor(ICameraDescriptor):
    _cam_kind = "ThorlabsTSIMock"

    @classmethod
    def iterate_cameras(cls, verbose=False):
        yield from []  # emulated; never discovered

    def get_kind_name(self):
        return "Thorlabs Scientific Camera (emulated)"

    def make_thread(self, name):
        return ThorlabsTSIMockCameraThread(name=name, kwargs=self.settings["params"].as_dict())

    def make_gui_control(self, parent):
        return Settings_GUI(parent, cam_desc=self)

    def make_gui_status(self, parent):
        return Status_GUI(parent, cam_desc=self)
