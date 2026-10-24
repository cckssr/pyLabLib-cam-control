.. _thorlabs_tsi:

Thorlabs TSI cameras over USB, and hardware-free cameras
==========================================================

.. _thorlabs_tsi_direct:

Direct-USB backend (no vendor software)
-------------------------------------------

Thorlabs Zelux (CS165MU/CU) and Kiralux/Quantalux CS-series cameras can be used in two ways:

- ``ThorlabsTLCam`` uses the vendor SDK, which requires `ThorCam <https://www.thorlabs.com/software_pages/ViewSoftwarePage.cfm?Code=ThorCam>`__ to be installed (Windows).
- ``ThorlabsTSIDirect`` talks to the camera directly over USB through a reverse-engineered driver (``camcontrol.thorlabs_tsi``), with no vendor software or DLL at all. It only needs ``pyusb`` and a libusb library (install with ``pip install -e ".[thorlabs-tsi]"``; on macOS, libusb comes from e.g. Homebrew, ``brew install libusb``).

``cam-control-detect`` finds cameras for both backends; pick one per camera in ``settings.cfg``. A direct-USB camera entry looks like this::

    cameras/thorlabs_tsi_direct_0/kind	ThorlabsTSIDirect
    cameras/thorlabs_tsi_direct_0/display_name	"CS165CU 12345 (direct USB)"
    cameras/thorlabs_tsi_direct_0/params/serial	12345
    cameras/thorlabs_tsi_direct_0/params/pid	16386

The direct-USB backend supports free-running acquisition with:

- exposure (within the limits the camera reports) and frame rate control (``Frame period``; 0 runs as fast as possible)
- gain in dB (continuous in 0.1 dB steps on the Zelux; the nearest of the few discrete gains on the CS-series)
- black level (Zelux only)
- ROI (the width step is 16 pixels on the CS-series) and on-camera binning (CS-series only)
- color bodies: ``Color output`` shows either the raw Bayer mosaic or an RGB image (bilinear demosaicing; not available while binning)
- frame info (camera frame counter and timestamp) with ``Acquire frame info``

Hardware triggering is not supported. The status panel shows the camera model, serial number, firmware, sensor, and the USB stream counters: frames missed by the camera, truncated or dropped frames and USB read errors; non-zero values are highlighted.


.. _thorlabs_mock:

Simulated and emulated cameras
-------------------------------------------

Two camera kinds need no hardware at all, which is useful to try the software, to develop plugins and filters, or to test a setup's configuration:

- ``simulated``: a generic camera with a physically shaped image model: a choice of test patterns (moving spot, gradient, moving checkerboard, flat field), exposure- and gain-dependent signal with shot and read noise, saturation at a chosen bit depth, binning, frame period, and internal or (simulated) external triggering::

      cameras/sim1/kind	simulated
      cameras/sim1/display_name	"Simulated camera"
      cameras/sim1/params/size	(1280, 1024)
      cameras/sim1/params/pattern	gaussian
      cameras/sim1/params/bit_depth	12

- ``ThorlabsTSIMock``: an emulated Thorlabs TSI camera. It answers the camera's own USB command protocol and streams frames in the camera's exact wire format, so the complete direct-USB backend runs on it unmodified, including the settings and status panels. It can also inject faults (dropped or truncated data, USB stalls, command errors, unplugging) to see how the software copes::

      cameras/tsi_mock/kind	ThorlabsTSIMock
      cameras/tsi_mock/display_name	"Emulated CS135CU"
      cameras/tsi_mock/params/family	cs
      cameras/tsi_mock/params/color	True
      cameras/tsi_mock/params/faults/drop_chunk_every	50

  ``family`` is ``zelux`` or ``cs``; ``sensor`` (e.g. ``(2448, 2048)``), ``model`` and ``serial`` are optional. The available faults are the fields of ``camcontrol.thorlabs_tsi.fake.FakeFaults``.

Neither kind is ever autodetected; add them to ``settings.cfg`` by hand.
