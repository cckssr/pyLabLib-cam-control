# pyLabLib cam-control

A PyQt5-based GUI application for controlling and streaming from scientific
cameras, built on top of [pylablib](https://pylablib.readthedocs.io/). This
repo is a fork of `AlexShkarin/pylablib-cam-control` v2.2.1, restructured
into an installable `camcontrol` package.

## Branches and provenance

- `dev` is the active branch (use this). Its last upstream commit is
  `f2472e1` (2024-11-18).
- `main` is a stale upstream snapshot from October 2022 — do not base new work
  on it.
- Upstream (`AlexShkarin/pylablib-cam-control`) is a low-but-nonzero-activity
  project (~15 commits/year in 2023-2024), not abandoned. Keep restructuring
  commits isolated (pure rename/move, no behavior change) so future upstream
  cherry-picks stay reviewable.

## Running it

Install with `pip install -e .` (or `.[dev]` for ruff/pytest), then:

```bash
# GUI with splash screen (the normal entry point)
cam-control --config-file settings.cfg
# equivalently:
python -m camcontrol --config-file settings.cfg

# GUI without splash screen, console-mode exceptions instead of dialogs
cam-control-console --config-file settings_test.cfg --camera sim1

# Camera autodetection (writes/updates settings.cfg)
cam-control-detect
```

`settings.cfg`, log files (`logout.txt`/`logerr.txt`), and `defaults.cfg`/
`locals.cfg` all resolve relative to the **current working directory** at
launch, not the package install location — run these commands from wherever
you keep your instrument's config, like any normal installed CLI tool.

A minimal hardware-free config for development (uses `camcontrol/cameras/sim.py`,
the only camera backend needing no vendor SDK/DLL):

```
cameras/sim1/kind	simulated
cameras/sim1/display_name	"Simulated camera"
cameras/sim1/params/size	(1024, 1024)
interface/color_theme	dark
```

For headless testing, set `QT_QPA_PLATFORM=offscreen`.

## Qt binding — hard constraint

Qt binding selection is **not done in this repo** — it's delegated to
`pylablib.core.gui`, which only supports **PyQt5 or PySide2** (see its
`pylablib/core/gui/__init__.py`). There is currently zero PyQt6/PySide6
support anywhere in pylablib 1.4.5. Do not attempt a Qt6 migration without
first patching pylablib itself. This repo standardizes on PyQt5 everywhere
(including `camcontrol/splash.py`, which must import Qt before pylablib is
importable).

On Python 3.14, PySide2/shiboken2 have no distribution at all — use PyQt5.

## Dependencies

Declared in `pyproject.toml` with lower-bound pins (not `==`): `pylablib>=1.4.5`,
`PyQt5>=5.15.11`, `pyqtgraph>=0.14`, `numpy>=2`, `pandas`, `scipy`,
`numba>=0.61`, `imageio`, `tifffile`, `Pillow`, `rpyc`, `qdarkstyle`, `plumbum`, plus
`pywin32` on Windows only. Verified installable and working end-to-end on
Python 3.14 (macOS arm64; Windows expected but not independently verified
in this environment). `pyusb>=1.2.1,<1.4` is an optional extra (`.[thorlabs-tsi]`),
needed only for the `ThorlabsTSIDirect` camera backend — see below.

## Architecture

### Package layout

```
camcontrol/
  __init__.py       # version = "2.2.1", compare_version()
  __main__.py        # `python -m camcontrol` -> splash.main()
  app.py              # main window, thread wiring (formerly control.py)
  detect.py           # camera autodetection CLI
  splash.py           # splash screen entry point, hands off to app.main()
  cameras/            # vendor camera backends (formerly utils/cameras/)
  gui/                # widgets (formerly utils/gui/)
  services/           # background thread classes (formerly utils/services/)
  plugins/            # plugin framework + built-ins (formerly top-level plugins/)
    filters/          # frame filter framework + built-ins
  resources/           # icon.ico, splash.png, button icons + resource_path() helper
  thorlabs_tsi/        # vendored direct-USB driver for Thorlabs TSI cameras (no vendor SDK)
```

`camcontrol/app.py`, `detect.py`, `splash.py` are side-effect-free on import:
argument parsing (`_parse_args`) and stdout/stderr log redirection
(`configure_logging`) only run inside each module's `main()`, not at module
level. `import camcontrol` from any directory does not chdir, does not touch
`sys.argv`, and does not redirect stdout/stderr — verified by importing it
from an unrelated directory.

### Threading model

Everything background runs as a `pylablib.core.thread.controller.QTaskThread`
— message-passing thread controllers with commands, variables, jobs, and
multicast pub/sub between named threads. `camcontrol/app.py` wires the frame
pipeline as a chain of stream subscriptions:

```
camera → frame_preprocess (binning) → frame_slowdown → frame_process (bkg subtraction) → GUI / channel_accumulator
                                    → frame_save (main saver)
                                    → channel_accumulator (raw)
any → frames/new/snap → frame_save_snap (snapshots)
```

Cross-thread calls use the `ctl.ca` (async), `ctl.cs` (sync), `ctl.csi`
(sync, ignore result) accessor idiom. GUI-thread safety uses pylablib
decorators: `@controller.exsafe`, `@controller.exsafeSlot()`,
`@controller.toploopSlot()`, `@controller.call_in_gui_thread`. To inspect
running thread controllers without blocking, read
`pylablib.core.thread.controller._running_threads` (a plain dict) rather than
`sync_controller(name)`, which blocks indefinitely if `name` never registers.

### Camera abstraction (`camcontrol/cameras/`)

Each vendor module defines one or more `ICameraDescriptor` subclasses
(`cameras/base.py`) with a `_cam_kind` string key and `make_thread()` /
`make_gui_control()` / `make_gui_status()`. A descriptor can `_expands` a
more generic one (e.g. `AndorSDK2IXON` expands `AndorSDK2`) to add
vendor-specific specialization after generic detection.

`cameras/loader.py` dynamically imports every `.py` in the directory via
`importlib.util.spec_from_file_location` (using `__file__`-relative paths, so
it works regardless of install location) and collects all `ICameraDescriptor`
subclasses into `camera_descriptors`. Each module is imported inside a
`try`/`except`: a module that fails (e.g. pylablib's PCO SC2 extension, which
is Windows-only and fails to `dlopen` on macOS/Linux) is logged and skipped
rather than aborting the whole registry — verified all 22 other kinds
(including `simulated`) still register when `PCOSC2` fails.

`cameras/sim.py` (`simulated` kind) is the only backend requiring no vendor
DLLs — it's the natural target for local development and tests.

#### Thorlabs TSI: two independent backends for the same hardware

`cameras/ThorlabsTLCam.py` (`_cam_kind = "ThorlabsTLCam"`) wraps pylablib's own
`pylablib.devices.Thorlabs.ThorlabsTLCamera`, which in turn needs the vendor
SDK/DLL (ThorCam) installed. `cameras/ThorlabsTSIDirect.py`
(`_cam_kind = "ThorlabsTSIDirect"`) is a second, independent descriptor for the
same camera families (Zelux CS165MU/CU; Kiralux/Quantalux CS-series) that talks
to the camera directly over USB via `camcontrol/thorlabs_tsi/` — a vendored,
reverse-engineered driver (ASCII command channel + bulk image endpoint, no
vendor SDK/DLL, `pyusb`/libusb only) — needs the `thorlabs-tsi` extra
(`pip install -e .[thorlabs-tsi]`). Both register and coexist; pick per-camera
in `settings.cfg`. The direct-USB backend covers free-running streaming with exposure
(camera-reported limits), frame-rate control (`frame_period`; 0 = off), gain in dB
(`gain_db`; the CS-series' discrete `gain.table` steps are resolved to the nearest one),
black level (Zelux only, detected from the firmware's `discover` list), ROI, on-camera
binning (CS-series only; the ROI width step keeps the *binned* width a multiple of the
4-pixel group the 10-bit unpacker needs), per-frame info (`framestamp` = camera frame
counter, `timestamp_us`), and a `color_mode` setting on color bodies (`raw` Bayer mosaic or
`rgb` bilinear demosaic in the poll thread, phase-corrected for odd ROI origins, skipped when
binned; RGB frames flow through the whole pipeline with `chandim=1`). No hardware
triggering. The status panel (`Status_GUI`) shows model/serial/firmware/sensor and the
stream counters. Driver errors are re-raised as `ThorlabsTSIError` (a pylablib
`DeviceError`), so a failed connect shows as "disconnected" instead of ending the thread;
stream failures surface as `FrameTransferError`, which pylablib's acquisition loop answers
by restarting acquisition.
`ThorlabsTSICamera` (in `ThorlabsTSIDirect.py`) adapts the driver's pull-based
frame queue to pylablib's `IROICamera`/`IExposureCamera` interface with a poll
thread feeding a ring buffer + `FrameNotifier`, mirroring the callback-driven
buffer `ThorlabsTLCamera` builds around the vendor SDK's push callback.
**Verified against a real camera** (a CS165CU, direct USB, macOS): streams
through the full app pipeline (`ThorlabsTSICamera` → `GenericCameraThread` →
GUI), not just the loader/discovery paths. Two real bugs surfaced by that test
and fixed: `_get_sensor_size()` was combining the wrong two fields from the
camera's `roi.max` reply (its four fields are each an independent per-argument
max, not "the largest legal ROI box" — see the fix's comment) and returning a
tiny (80, 4) instead of the sensor's true (1440, 1080), which then clamped the
ROI to a near-invisible sliver; and the vendored driver had a real upstream
bug (`stream.py`'s `_on_transfer_error` used a bare `from bulkreader import
...` instead of `from .bulkreader import ...`, so it raised `ModuleNotFoundError`
on every single non-clean USB transfer completion, silently retiring that
transfer from rotation instead of keeping it in play — untested upstream since
nothing exercises that path without live hardware). A `stream_stats` status
variable now surfaces the underlying reader's counters (frames missed/
truncated/dropped, read errors) for exactly this kind of "no image, no error"
diagnosis. One consequence worth knowing: the wrong detector size got baked
into a running app's persisted GUI state (`defaults.cfg`, gitignored, keyed
per camera name as `<name>/cam/cam/roi`) before the fix — a stale tiny ROI
there will keep getting re-applied on connect even after upgrading, until
that cached value is cleared (delete the `<name>/cam/cam/roi` line, or use
the GUI's "Set full ROI") or `defaults.cfg` is removed outright.

#### Emulated Thorlabs TSI camera (`ThorlabsTSIMock`)

`camcontrol/thorlabs_tsi/fake.py`'s `FakeTsiDevice` emulates a camera at the pyusb `Device`
level: it answers the ASCII command channel statefully (snapping values, in-band errors
such as "Camera Running") and streams frames in the exact wire format of either family
(`family="zelux"` or `"cs"`, optionally `color=True` or a custom `sensor`), so the real
transport, chunk assembler, decoder, `TsiCamera` and `ThorlabsTSICamera` run unmodified on
top of it. `FakeFaults` injects dropped chunks, truncated frames, junk/trailers, counter
gaps, endpoint stalls, command errors, late replies and unplugging. It cannot drive
`AsyncBulkReader` (no libusb handle), so it needs `TsiCamera(..., synchronous_stream=True)`;
`FakeTsiBus([...]).find` replaces `usb.core.find`. `cameras/ThorlabsTSIMock.py` registers
it as camera kind `ThorlabsTSIMock` (never autodetected; params `family`, `color`, `model`,
`serial`, `sensor`, `faults/<FakeFaults field>`), reusing the TSIDirect thread and GUI:

```
cameras/tsi_mock/kind	ThorlabsTSIMock
cameras/tsi_mock/params/family	cs
cameras/tsi_mock/params/color	True
```

### Plugins and filters (two independent extension systems)

Both use the same discovery pattern: scan the package's own bundled
directory (via `__file__`, so it works when pip-installed from anywhere),
plus optionally an extra directory for user-supplied additions — skipped if
that directory doesn't exist or turns out to be the bundled one itself (e.g.
running from a source checkout). Each discovered module is imported inside a
`try`/`except` like the camera loader.

- **Plugins** (`camcontrol/plugins/base.py`, `IPlugin`, `find_plugins()`):
  each runs in its own thread controller, declared in `settings.cfg`
  (`plugins/<name>/class <classname>`). Lifecycle hooks: `preinit()`,
  `setup()`, `cleanup()`, `postcleanup()`, `setup_gui()`. Startup is
  synchronized across plugins and the main app via four named barriers:
  `plugin_create → plugin_preinit → plugin_setup → plugin_start`. Shipped
  plugins: `filter`, `server` (TCP/IP JSON control server, off by default,
  unauthenticated when enabled — see security note below), `trigger_save`.
  The extra directory is `<runtime/root_folder>/plugins` (next to the
  settings file) if it exists.
- **Frame filters** (`camcontrol/plugins/filters/`, `IFrameFilter`,
  `find_filters()`): only classes with a non-`None` `_class_name` are
  registered — `template.py` and `examples.py` intentionally comment it out,
  the documented pattern for adding your own filter without it
  auto-activating. The extra directory is
  `<runtime/root_folder>/plugins/filters`.

Both `_load_modules`/`_discover_subclasses` helpers live in
`camcontrol/plugins/base.py`; `filter.py`'s `find_filters()` reuses them via
`from . import base`.

### GUI (`camcontrol/gui/`)

Built entirely on `pylablib.core.gui.widgets`. The top-level window is `MainWindow`
(a `QMainWindow`) in `camcontrol/app.py`; its central widget is `StandaloneFrame`, which is
both the image area (`plots_tabs`) and the root of the GUI values tree. All other panels
(`camera`, `processing`, `image`, `saving`, `status`, `time_series`) are frame containers
added to `StandaloneFrame` with `location="skip"` (so their GUI values paths -- `cam/...`,
`proc`, `preproc/*`, `plotting`, `disp`, ... -- and therefore `defaults.cfg` files from
older versions are unchanged) and displayed in `QDockWidget`s. Plugins keep using
`plots_tabs` and `control_tabs` (now inside the Processing dock). Menu bar, toolbar
(Connect/Disconnect, Start/Stop, Snap, Record, Auto levels), keyboard shortcuts (Help →
Keyboard shortcuts) and a status bar (connection/acquisition state, camera/display FPS,
saving progress, horizontal activity indicators) are built in `StandaloneFrame._setup_*`.
The dock layout is persisted as `defaults/window/dock_state` (hex `saveState`); View →
Reset layout restores the default (`interface/compact` tabs all right-hand docks into one
group). The old position-based tutorial was removed. Base classes every camera-specific
GUI subclasses: `base_cam_ctl_gui.ICameraSettings_GUI` / `GenericCameraSettings_GUI` /
`GenericCameraStatus_GUI`.

### Error handling and image view

pylablib stops the whole application on any exception in a thread controller. The camera
thread created in `app.py` gets `cameras/base.py`'s `DeviceErrorTolerantThreadMixin` (via
`make_device_errors_non_fatal`, which swaps in a subclass on the not-yet-started thread
object, so it applies to every backend): a `self.DeviceError` during acquisition, the
periodic parameter poll or `apply_parameters` ends the acquisition, is logged and stored in
the `device_error` thread variable (shown in the status bar), and closes the camera via a
queued `close` command (closing from inside the acquisition job would re-enter pylablib's
batch-job machinery). The poll's `open()` then reconnects automatically when possible.
Backends must therefore raise their device's `Error` class (a `DeviceError`) for anything
recoverable -- see `ThorlabsTSIDirect`'s `_translate_errors`.

`gui/image_view.py`'s `ImageViewController` wraps pylablib's `ImagePlotter`: mouse
zoom/pan re-enabled, `ImageView.setImage` wrapped so the view only auto-fits new frames
until the user zooms/pans (Fit view, Ctrl+0, re-enables it), and the value under the cursor
reported to the status bar in displayed-image coordinates (same as the crosshair lines).
The colormap selector is the `view/colormap` value in the Image dock.

### Resources (`camcontrol/resources/`)

`resource_path(name)` (in `camcontrol/resources/__init__.py`) resolves a
bundled file (`icon.ico`, `splash.png`, `cog.png`, `play.png`, `stop.png`,
`rec.png`) via `importlib.resources`, so icon lookups work regardless of
install location. `pyproject.toml`'s `[tool.setuptools.package-data]`
ships these files with the wheel.

## Known rough edges (not yet fixed)

- `pack.py`'s Windows packaging/build script (embedded-interpreter zip
  distribution) has been patched to import cleanly on Python 3.12+ and to
  reference the new `camcontrol/` layout, but its actual packaging output has
  **not been validated end-to-end on Windows** since this restructuring —
  treat it as needing a dedicated verification pass before relying on it. The
  bundled `BFModule-1.0.1-cp38-cp38-win_amd64.whl` (`installdep.py`) is also
  still cp38-locked and vendor-supplied; unfixable from here.
- `plugins/server.py`'s control server, when enabled, accepts unauthenticated
  commands over TCP and uses peer-supplied `dtype`/`shape` to build a numpy
  array from the payload — bind to loopback or add auth before exposing it on
  a shared network.


## Testing

CI (`.github/workflows/ci.yml`) runs ruff check, ruff format check, an import smoke test and
pytest on Linux/macOS/Windows x Python 3.12-3.14 (`requires-python >=3.12`). The vendored
`camcontrol/thorlabs_tsi/` is excluded from `ruff format` (kept in upstream's formatting so
syncs stay small) but still linted. pytest turns `DeprecationWarning`s raised from
`camcontrol.*` into errors. `camcontrol/_compat.py::apply()` (called from the entry points'
`main()`, not at import) patches third-party incompatibilities, currently pylablib's use of
the removed `np.asscalar` when serializing settings.

`tests/` (pytest, install with `pip install -e .[dev]`). `tests/conftest.py`
sets `QT_QPA_PLATFORM=offscreen` if not already set, since it must happen
before Qt is first imported.

```bash
pytest
```

- `test_camera_registry.py` — loader robustness (a broken vendor backend must
  not take down the whole registry)
- `test_plugin_discovery.py` — built-in plugins/filters found, external-directory
  hook works, no double-loading when the external dir is the bundled one
- `test_numba_kernels.py` — the two 12-bit packing kernels agree
- `test_saving.py` — TIFF/BigTIFF round-trip via imageio
- `test_tsi_frames.py`, `test_tsi_assembler.py`, `test_tsi_commands.py`,
  `test_tsi_adapter.py` — the Thorlabs TSI driver and adapter against `FakeTsiDevice`
  (wire format round trips, every fault type, >256-chunk frames, command channel,
  acquisition/stop/restart/unplug through pylablib's camera API)
- `test_acquisition_tsi_mock.py` — full app on `ThorlabsTSIMock`, each case in a fresh
  process via `tests/app_probe.py` (prints a JSON result line), sidestepping the
  one-app-per-process limitation described below
- `test_acquisition.py` — full app through the `simulated` camera with the
  `filter`/`trigger_save` plugins enabled; `test_main_window.py` — dock window
  structure and layout persistence across two runs.

**Never run the app or create pylablib plot widgets inside the pytest process.**
pylablib's thread-controller registry and the `QApplication` are per-process state, and
pylablib/pyqtgraph widgets created in-process segfault at interpreter shutdown (exit code
139, a "Python quit unexpectedly" dialog on macOS). Full-app tests go through the
`run_app_probe` fixture (`tests/conftest.py`), which runs `tests/app_probe.py` in a fresh
process and returns its JSON result line. `controller.get_gui_controller().stop()` raises
`threadprop.InterruptExceptionStop` by design (it unwinds the GUI event loop); the probe
catches it. Tests run with `QT_QPA_PLATFORM=offscreen` and `CAMCONTROL_USE_OPENGL=0`
(offscreen Qt has no GL context); the offscreen screen is only 800x600.
