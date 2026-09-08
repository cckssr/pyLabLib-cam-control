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
`numba>=0.61`, `imageio`, `Pillow`, `rpyc`, `qdarkstyle`, `plumbum`, plus
`pywin32` on Windows only. Verified installable and working end-to-end on
Python 3.14 (macOS arm64; Windows expected but not independently verified
in this environment).

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

Built entirely on `pylablib.core.gui.widgets`. Main window is
`StandaloneFrame` in `camcontrol/app.py`. Base classes every camera-specific
GUI subclasses: `base_cam_ctl_gui.ICameraSettings_GUI` /
`GenericCameraSettings_GUI` / `GenericCameraStatus_GUI`.

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
- No CI yet (no `.github/workflows/`).

## Testing

There is currently no committed test suite. When adding one, drive it through
the `simulated` camera under `QT_QPA_PLATFORM=offscreen` — this has been
verified to work end-to-end (app boots, builds plugins, acquires frames,
saves TIFF/BigTIFF, shuts down cleanly). A useful non-GUI smoke check:

```python
from camcontrol.cameras.loader import camera_descriptors
assert len(camera_descriptors) == 23  # 22 on a platform missing PCOSC2's C extension
```
