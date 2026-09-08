# pyLabLib cam-control

A PyQt5-based GUI application for controlling and streaming from scientific
cameras, built on top of [pylablib](https://pylablib.readthedocs.io/). This
repo is a fork of `AlexShkarin/pylablib-cam-control` v2.2.1.

## Branches and provenance

- `dev` is the active branch (use this). Its last upstream commit is
  `f2472e1` (2024-11-18). Only one commit on top of upstream is fork-original.
- `main` is a stale upstream snapshot from October 2022 — do not base new work
  on it.
- Upstream (`AlexShkarin/pylablib-cam-control`) is a low-but-nonzero-activity
  project (~15 commits/year in 2023-2024), not abandoned. Keep restructuring
  commits isolated (pure rename/move, no behavior change) so future upstream
  cherry-picks stay reviewable.

## Running it

There is no installed package yet (`pyproject.toml` / restructuring into a
`camcontrol` package is planned but not done). Run directly from the repo root:

```bash
# GUI, camera selection prompted interactively
python control.py --config-file settings.cfg

# GUI, specific camera from settings.cfg, e.g. a hardware-free simulated camera
python control.py --config-file settings_test.cfg --camera sim1

# Camera autodetection (writes/updates settings.cfg)
python detect.py
```

A minimal hardware-free config for development (uses `utils/cameras/sim.py`,
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
first patching pylablib itself. `splash.py` has its own `try: PyQt5 / except:
PySide2` fallback since it must run before pylablib is imported.

On Python 3.14, PySide2/shiboken2 have no distribution at all — use PyQt5.

## Dependencies

`requirements.txt` currently pins Windows/Python-3.8-era versions
(`PySide2==5.15.2`, `numba==0.53.1`, `llvmlite==0.36.0`, etc.) that are
**not installable on modern Python**. The real minimum stack that works
(verified on Python 3.14, both macOS arm64 and — expected, not yet
independently verified — Windows) is: `pylablib>=1.4.5`, `PyQt5>=5.15.11`,
`pyqtgraph>=0.14`, `numpy>=2`, `pandas`, `scipy`, `numba>=0.67`, `imageio`,
`Pillow`, `rpyc`, `qdarkstyle`, `plumbum`, plus `pywin32` on Windows only.

Note `pylablib` itself is not listed in `requirements.txt` at all — it's
mentioned only in `docs/expanding.rst`. Any dependency file rewrite must add
it explicitly.

## Architecture

### Threading model

Everything background runs as a `pylablib.core.thread.controller.QTaskThread`
— message-passing thread controllers with commands, variables, jobs, and
multicast pub/sub between named threads. `control.py` wires the frame
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
`@controller.toploopSlot()`, `@controller.call_in_gui_thread`.

### Camera abstraction (`utils/cameras/`)

Each vendor module defines one or more `ICameraDescriptor` subclasses
(`utils/cameras/base.py`) with a `_cam_kind` string key and
`make_thread()` / `make_gui_control()` / `make_gui_status()`. A descriptor
can `_expands` a more generic one (e.g. `AndorSDK2IXON` expands `AndorSDK2`)
to add vendor-specific specialization after generic detection.

`utils/cameras/loader.py` dynamically imports every `.py` in the directory
via `importlib.util.spec_from_file_location` and collects all
`ICameraDescriptor` subclasses into `camera_descriptors`. **This loader has
no per-module error guard** — one vendor module failing to import (e.g. a
Windows-only C extension failing to `dlopen` on macOS/Linux) currently
kills camera detection entirely. If you see the whole app fail to start
with an `ImportError` from one camera module, that's why.

`utils/cameras/sim.py` (`simulated` kind) is the only backend requiring no
vendor DLLs — it's the natural target for local development and tests.

### Plugins and filters (two independent extension systems)

- **Plugins** (`plugins/base.py`, `IPlugin`): each runs in its own thread
  controller, declared in `settings.cfg` (`plugins/<name>/class <classname>`).
  Lifecycle hooks: `preinit()`, `setup()`, `cleanup()`, `postcleanup()`,
  `setup_gui()`. Startup is synchronized across plugins and the main app via
  four named barriers: `plugin_create → plugin_preinit → plugin_setup →
  plugin_start`. Shipped plugins: `filter`, `server` (TCP/IP JSON control
  server, off by default, unauthenticated when enabled — see security note
  below), `trigger_save`.
- **Frame filters** (`plugins/filters/`, `IFrameFilter` and specializations):
  loaded the same dynamic-import way from `plugins/filters/*.py`. Only
  classes with a non-`None` `_class_name` are registered — `template.py` and
  `examples.py` intentionally comment it out, which is the documented pattern
  for adding your own filter without it auto-activating.

### GUI (`utils/gui/`)

Built entirely on `pylablib.core.gui.widgets`. Main window is
`StandaloneFrame` in `control.py`. Base classes every camera-specific GUI
subclasses: `base_cam_ctl_gui.ICameraSettings_GUI` /
`GenericCameraSettings_GUI` / `GenericCameraStatus_GUI`.

## Known rough edges (not yet fixed)

- `utils/cameras/loader.py` — no per-module import guard (see above).
- `control.py`, `detect.py`, `splash.py` all do `os.chdir()` +
  `sys.path.append(".")` and parse `sys.argv` / hijack `sys.stdout`/`stderr`
  at **import time**, not inside `if __name__ == "__main__"` in all cases —
  this makes the app hard to import as a library. Restructuring into an
  importable package is planned.
- `pack.py` imports `distutils.ccompiler`, removed in Python 3.12+; it's the
  Windows packaging/build script only, not the runtime app.
- Icon/resource paths (`icon.ico`, `splash.png`, `resources/*.png`) are
  resolved relative to CWD / `runtime/root_folder`, not the package.
- `plugins/server.py`'s control server, when enabled, accepts unauthenticated
  commands over TCP and uses peer-supplied `dtype`/`shape` to build a numpy
  array from the payload — bind to loopback or add auth before exposing it on
  a shared network.

## Testing

There is currently no test suite. When adding one, drive it through the
`simulated` camera under `QT_QPA_PLATFORM=offscreen` — this has been verified
to work end-to-end (app boots, acquires frames, saves TIFF/BigTIFF).
