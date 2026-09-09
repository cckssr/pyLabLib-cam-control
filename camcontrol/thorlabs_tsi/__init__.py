"""Portable USB driver for Thorlabs TSI scientific cameras (Zelux, Kiralux/Quantalux CS-series).

Vendored, with import paths adjusted for this package, from the ``tsi-camera`` project
(https://github.com/, see that project's ``protocol.md`` for the reverse-engineered on-wire
protocol this implements). Talks to the camera directly over USB (``pyusb``/libusb) using its
ASCII command channel and bulk image endpoint -- no vendor SDK or DLL required at runtime.

``camcontrol.cameras.ThorlabsTSIDirect`` is the ``ICameraDescriptor`` that wires this driver
into the app as an alternative to the vendor-SDK-based ``ThorlabsTLCam`` backend. Covers basic
streaming (ROI, exposure, gain); does not implement binning, hardware triggering, or the
vendor's color pipeline (see ``frames.decode_frame`` and ``camera.TsiCamera`` docstrings).
"""

from .camera import TsiCamera
from .transport import StreamDesyncError, TsiCameraTransport

__all__ = ["TsiCamera", "TsiCameraTransport", "StreamDesyncError"]
