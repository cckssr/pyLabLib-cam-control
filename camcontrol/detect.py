# Copyright (C) 2021  Alexey Shkarin

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import sys
import argparse

from pylablib.core.utils import dictionary, general as general_utils
from pylablib.core.fileio.loadfile import load_dict
from pylablib.core.fileio.savefile import save_dict
import pylablib

import time
import threading
import datetime

from camcontrol.cameras.loader import camera_descriptors

### Redirecting console / errors to file logs ###
log_lock = threading.RLock()


class StreamLogger(general_utils.StreamFileLogger):
    def __init__(self, path, stream=None):
        super().__init__(path, stream=stream, lock=log_lock)
        self.start_time = datetime.datetime.now()

    def write_header(self, f):
        f.write("\n\n" + "-" * 50)
        f.write(
            "\nStarting {} {:on %Y/%m/%d at %H:%M:%S}\n\n".format(
                os.path.split(sys.argv[0])[1], self.start_time
            )
        )


def configure_logging():
    """Redirect stdout/stderr to log files in the current working directory."""
    sys.stderr = StreamLogger("logerr.txt", sys.stderr)
    sys.stdout = StreamLogger("logout.txt", sys.stdout)
    detect_logger = StreamLogger("logdetect.txt")
    sys.stderr.add_stream(detect_logger)
    sys.stdout.add_stream(detect_logger)


def detect_all(verbose=False):
    cams = dictionary.Dictionary()
    root_descriptors = [d for d in camera_descriptors.values() if d._expands is None]
    for c in root_descriptors:
        cams.update(
            c.detect(
                verbose=verbose, camera_descriptors=list(camera_descriptors.values())
            )
            or {}
        )
    if cams:
        for c in cams:
            if "display_name" not in cams[c]:
                cams[c, "display_name"] = c
    return dictionary.Dictionary({"cameras": cams})


def update_settings_file(
    cfg_path="settings.cfg", verbose=False, do_save=True, confirm=False, wait=False
):
    settings = detect_all(verbose=verbose)
    if not settings:
        if verbose:
            print("Couldn't detect any supported cameras")
    elif do_save:
        if os.path.exists(cfg_path):
            ans = (
                input("Configuration file already exists. Modify? [y/N] ").strip()
                if confirm
                else "y"
            )
            if ans.lower() != "y":
                do_save = False
            else:
                curr_settings = load_dict(cfg_path)
                if "cameras" in curr_settings:
                    del curr_settings["cameras"]
                curr_settings["cameras"] = settings["cameras"]
                settings = curr_settings
        if do_save:
            save_dict(settings, cfg_path)
            if verbose:
                print("Successfully generated config file {}".format(cfg_path))
        else:
            return
    else:
        if verbose:
            print("Skipping updating the config file")
        return
    if confirm and not do_save:
        input()
    elif wait:
        time.sleep(3.0)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Camera autodetection")
    parser.add_argument("--silent", "-s", help="silent execution", action="store_true")
    parser.add_argument(
        "--yes",
        "-y",
        help="automatically confirm settings file overwrite",
        action="store_true",
    )
    parser.add_argument(
        "--nosave",
        "-n",
        help="skip saving detected cameras to the settings file (only used for diagnostics)",
        action="store_true",
    )
    parser.add_argument(
        "--show-errors",
        help="show errors raised on camera detection",
        action="store_true",
    )
    parser.add_argument(
        "--wait",
        help="show waiting message for 3 seconds in the end",
        action="store_true",
    )
    parser.add_argument(
        "--config-file",
        "-cf",
        help="configuration file path",
        metavar="FILE",
        default="settings.cfg",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    configure_logging()
    if not args.silent:
        print("Detecting cameras...\n")
    if os.path.exists(args.config_file):
        settings = load_dict(args.config_file)
        if "dlls" in settings:
            for k, v in settings["dlls"].items():
                pylablib.par["devices/dlls", k] = v
    if args.silent:
        verbose = False
    else:
        verbose = "full" if args.show_errors else True
    update_settings_file(
        cfg_path=args.config_file,
        verbose=verbose,
        confirm=not (args.silent or args.yes),
        wait=args.wait,
        do_save=not args.nosave,
    )


if __name__ == "__main__":
    main()
