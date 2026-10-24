.. _overview:

Overview
=========================

.. _overview_install:

Installation
------------------------- 

Cam-control is a Python package (Python 3.12 or newer). Install it into a virtual environment from a checkout of the repository::

    python -m venv .venv
    .venv/bin/pip install -e .                  # on Windows: .venv\Scripts\pip install -e .
    .venv/bin/pip install -e ".[thorlabs-tsi]"  # optional: direct-USB Thorlabs TSI cameras

This installs three commands:

- ``cam-control``: the application (with a splash screen); equivalent to ``python -m camcontrol``.
- ``cam-control-console``: the application without the splash screen, reporting errors on the console.
- ``cam-control-detect``: camera autodetection, which writes the found cameras into ``settings.cfg``.

The settings file (``settings.cfg``), the stored GUI state (``defaults.cfg``, ``locals.cfg``) and the logs (``logout.txt``, ``logerr.txt``) are all looked up in the **current working directory**, so run the commands from the folder where you keep the configuration of your setup.

On the first run without a settings file, the software suggests to either import a settings file (``settings.cfg``) from a previous version, or to detect all connected supported cameras and store their connection parameters. Make sure that at this point all cameras are connected, turned on, and not used in any other software (e.g., Andor Solis, Hokawo, or NI MAX). If new cameras are added to the PC, they can be re-discovered by running ``cam-control-detect``.

If only one camera is found, ``cam-control`` automatically connects to it. Otherwise, a dropdown menu shows up allowing selection of specific cameras.

To try the software without any camera, use the :ref:`simulated or emulated cameras <thorlabs_mock>`.

The Windows zip distribution of earlier versions (with a bundled interpreter and ``control.exe``) is built by ``pack.py``; it has not been re-validated since the move to the ``camcontrol`` package.


.. _overview_software_requirements:

Software requirements
-------------------------

Cam-control is built using `pylablib <https://github.com/AlexShkarin/pyLabLib/>`__ and includes all of the cameras supported there. All of these cameras need some kind of drivers and API installed:

- Andor cameras: `Andor Solis <https://andor.oxinst.com/products/solis-software/>`__ or `Andor SKD <https://andor.oxinst.com/products/software-development-kit/>`__.
- Hamamatsu/DCAM cameras: Hamamatsu-supplied software such as Hokawo, or the freely available `DCAM API <https://dcam-api.com/downloads/>`__. Keep in mind, that you also need to install the drivers for the corresponding camera type (USB, Ethernet, IEEE 1394). These drivers are in the same installer, but need to be installed separately.
- Thorlabs uc480 and Thorlabs scientific imaging cameras: freely available `ThorCam <https://www.thorlabs.com/software_pages/ViewSoftwarePage.cfm?Code=ThorCam>`__ software (no older than v3.5.0 for the scientific imaging cameras). Zelux and Kiralux/Quantalux CS-series cameras can alternatively be used without any vendor software through the :ref:`direct-USB backend <thorlabs_tsi>`.
- IDS uEye cameras: freely available (upon registration) `IDS Software Suite <https://en.ids-imaging.com/ids-software-suite.html>`__.
- PCO cameras: freely available `pco.camware <https://www.pco.de/software/camera-control-software/pcocamware/>`__ software.
- IMAQdx cameras: all the necessary code is contained in the freely available `Vision Acquisition Software <https://www.ni.com/en-us/support/downloads/drivers/download.vision-acquisition-software.html>`__. However, the IMAQdx part of the software is proprietary, and needs to be purchased to use.
- IMAQ frame grabbers: freely available `Vision Acquisition Software <https://www.ni.com/en-us/support/downloads/drivers/download.vision-acquisition-software.html>`__. In addition, you would also need to specify the correct camera file, which describes the camera communication protocol details.
- Silicon Software frame grabbers: freely available (upon registration) `Silicon Software Runtime Environment <https://www.baslerweb.com/en/sales-support/downloads/software-downloads/#type=framegrabbersoftware;language=all;version=all;os=windows64bit>`__ (the newest version for 64-bit Windows is `5.7.0 <https://www.baslerweb.com/en/sales-support/downloads/software-downloads/complete-installation-for-windows-64bit-ver-5-7-0/>`__).
- PhotonFocus: on top of IMAQ or Silicon Software requirements, it needs freely available (upon registration) `PFInstaller <https://www.photonfocus.com/support/software/>`__ software.
- PICam (Princeton Instruments) cameras: freely available `PICam <https://www.princetoninstruments.com/products/software-family/pi-cam>`__ software.
- PVCAM (Photometrics) cameras: freely available (upon registration) `PVCAM software <https://www.photometrics.com/support/download/pvcam>`__.
- Basler cameras: freely available (upon registration) `Basler pylon Camera Software Suite <https://www.baslerweb.com/en/downloads/software-downloads/>`__ (the current latest version is `7.1.0 <https://www.baslerweb.com/en/downloads/software-downloads/software-pylon-7-1-0-windows/>`__).
- Mightex S series cameras: freely available `S-series camera software package <https://www.mightexsystems.com/product/s-series-ultra-compact-usb2-0-color-3mp-cmos-cameras/>`__ (the current latest version is from `2019.01.04 <https://mightex.wpenginepowered.com/wp-content/uploads/2019/04/Mightex_SCX_CDROM_20190104.zip>`__). This software does not require installation, and the two required DLLs ``MT_USBCamera_SDK_DS.dll`` and ``MtUsbLib.dll`` are contained in the ``DirectShow/MightexClassicCameraEngine`` folder withing the archive (do not confuse them with ``MT_USBCamera_SDK.dll`` library, which is similar, but has some downsides regarding threading). Both of these files should be copied into the main ``cam-control`` folder (it should contain files like ``settings.cfg``) before autodetection is run.
- BitFlow Axion frame grabbers require several steps:

  - First, you need to install freely available `BitFlow SDK 6.5 <https://www.bitflow.com/downloads/bfsdk65.zip>`__, preferrably into its default folder or, at least, using the default folder name ``BitFlow SDK 6.5``.
  - Second, you have to download manufacturer-provided `BitFlow Python 3.8 package <https://www.bitflow.com/downloads/BFPython38_Release.zip>`__, extract ``BFModule-1.0.1-cp38-cp38-win_amd64.whl`` file from there to the ``python`` folder within ``cam-control`` (it should contain files like ``python.exe``), and run ``install-dependencies.bat`` script contained in the same folder.
  - Next, you need to specify the correct camera file for your camera using ``SysReg`` utility located in the ``Bin64`` folder of your BitFlow installation (by default, ``C:\BitFlow SDK 6.5\Bin64``).
  - Afterwards, you should copy this camera file into the main ``cam-control`` folder (it should contain files like ``settings.cfg``). The camera file is located in ``Config\Axn`` folder within the BitFlow installation (by default, ``C:\BitFlow SDK 6.5\Config\Axn``) and has ``.bfml`` extension, e.g., ``PhotonFocus-MV1-D1024E-160-CL.bfml``.
  - You can now search for the cameras by running either ``cam-control`` for the first time, or ``cam-control-detect`` at any point.

.. note::

    Cam-control is only available in 64-bit version, which in most cases means that you must install 64-bit versions of the software described above.

.. note::

    It is strongly recommended that you install the manufacturer software (especially Andor Solis and ThorCam) into their default locations. Otherwise, cam-control might not be able to find the required DLLs and communicate with the cameras. In this case, you would need to specify the location of the necessary DLLs in the :ref:`settings file <settings_file_system>`.

In most cases, you already have the necessary software installed to communicate with the cameras to begin with. As a rule of thumb, if you can open the camera in the manufacturer-supplied software, you can open it in the cam-control as well.

In rare cases you might also need to install Microsoft Visual C++ Redistributable Package. You can obtain it on the `Microsoft website <https://aka.ms/vs/16/release/vc_redist.x64.exe>`__.

Specifying camera files
~~~~~~~~~~~~~~~~~~~~~~~~~

Cameras using NI-IMAQ frame grabbers also need the correct ``.icd`` camera file for the connected camera, which describes the communication protocol details, sensor size, possible bit depths, etc. Some of the common camera files are already provided when you install Vision Acquisition Software, others need to be obtained from the manufacturer. To specify them, follow the following steps:

1) If you are using a manufacturer-provided file, copy it into the NI-IMAQ camera file storage. By default it is located at ``C:\Users\Public\Documents\National Instruments\NI-IMAQ\Data`` (the folder should already exist and have many ``.icd`` files).
2) Run NI MAX, and there find the camera entry in ``Devices and Interfaces`` -> ``NI-IMAQ Devices`` -> ``img<n>: <Frame grabber name>`` (e.g., ``img0: NI PCIe-1433``) -> ``Channel <n>`` (0 for single-channel frame grabbers).
3) Right-click on the camera entry and there select ``Camera`` -> ``<Manufacturer>`` -> ``<Camera model corresponding to the file>``.

PhotonFocus provides camera files with PFRemote, and they can be found in ``<Photon Focus folder>\PFRemote\fg_files``. There are several files with names like ``pfFg_ni_2tap_8bit.icd``, which should be selected based on the desired bit depth (usually 12 bit is preferable, if it is available for your camera) and the number of CameraLink taps (specified in the technical specification found in the camera manual; e.g., MV1-D1024E-160-CL has 2 taps). After specifying the file, you need to also specify the camera pixel depth using PFRemote. The correct setting is located at ``Data Output`` -> ``Output Mode`` -> ``Resolution``.


.. _overview_layout:

General layout
-------------------------

.. image:: overview.png

The image takes the center of the window; all controls are in panels ("docks") around it: ``Camera``, ``Processing`` (with the plugin controls) and ``Image`` on the right, ``Saving`` and ``Status`` below them, and the ``Time series`` plot under the image while it is enabled. Every panel can be moved, stacked into tabs, detached into its own window, or closed (and reopened in the ``View`` menu); the layout is remembered between runs, and ``View`` -> ``Reset layout`` restores the default one. The toolbar and the ``Camera`` menu hold the most frequent actions, and the status bar at the bottom shows the camera state, frame rates, saving progress and the pixel value under the mouse cursor.

The acquisition is started by pressing ``Start acquisition`` (or ``Ctrl+Space``). Note that you might need to adjust camera parameters (e.g., specify exposure, ROI, binning, pixel readout rate) to get reasonable image quality and performance.

All of the entered values are automatically saved on exit and restored on the next restart. It is also possible to :ref:`save the settings to a file <interface_footer>` and load them later, which is helpful for working in several different regimes.

You can find more information on the :ref:`interface page <interface>`.

With the ``Compact interface`` option in the :ref:`preferences <interface_preferences>`, all right-hand panels share a single tab group, which leaves more room for the image.

The software uses a dark color theme by default. You can change it in the :ref:`preferences <interface_preferences>`.

.. _overview_feedback:

Support and feedback
-------------------------

If you have any issues, suggestions, or feedback, you can either raise an issue on GitHub at https://github.com/AlexShkarin/pylablib-cam-control/issues, or send an e-mail to pylablib@gmail.com.