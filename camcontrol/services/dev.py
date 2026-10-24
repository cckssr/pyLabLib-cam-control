import datetime
import os

from pylablib.core.fileio import savefile
from pylablib.core.gui import QtCore
from pylablib.core.gui import utils as gui_utils
from pylablib.core.thread import controller


def crop(img, left=0, right=None, top=0, bottom=None):
    """Crop QPixmap to the given rectangle"""
    w, h = img.size().width(), img.size().height()
    left = left % w
    right = right % w if right is not None else w
    top = top % h
    bottom = bottom % h if bottom is not None else h
    rect = QtCore.QRect(left, top, right - left, bottom - top)
    return img.copy(rect)


def take_screenshots(src):
    """Take screenshots for the documentation"""
    sfolder = "screenshots/"
    os.makedirs(sfolder, exist_ok=True)
    gui_utils.get_screenshot(window=src.window).save(sfolder + "overview.png")
    src.c["plot_tabs"].set_by_name("standard_frame")
    src.ctl.sleep(0.1)
    gui_utils.get_screenshot(widget=src.c["plot_tabs"]).save(
        sfolder + "interface_image_display.png"
    )
    for name, dock in src.docks.items():
        if dock.isVisible():
            dock.raise_()
            src.ctl.sleep(0.1)
            gui_utils.get_screenshot(widget=dock.widget()).save(sfolder + f"interface_{name}.png")
    gui_utils.get_screenshot(widget=src.window.statusBar()).save(
        sfolder + "interface_status_bar.png"
    )
    src.call_extra("settings_editor")
    src.settings_editor.tabs.setCurrentIndex(1)
    src.ctl.sleep(0.1)
    gui_utils.get_screenshot(window=src.settings_editor).save(sfolder + "interface_preferences.png")
    src.settings_editor.close()
    if "show_attributes_window" in src.c["cam_controller/settings"].advanced_params:
        window = src.c["cam_controller/settings"].advanced_params.c["attributes_window"]
        window.show()
        window.tabs.set_by_name("value")
        src.ctl.sleep(0.1)
        gui_utils.get_screenshot(window=window).save(sfolder + "interface_camera_attributes.png")
        window.tabs.set_by_name("value_props")
        src.ctl.sleep(0.1)
        gui_utils.get_screenshot(window=window).save(
            sfolder + "interface_camera_attributes_settings.png"
        )
        window.close()


def on_key_press(src, event):
    """Execute dev functions based on the pressed keys"""
    if (
        event.modifiers() & QtCore.Qt.ControlModifier
        and event.modifiers() & QtCore.Qt.ShiftModifier
        and event.key() == QtCore.Qt.Key_S
    ):
        take_screenshots(src)
    if (
        event.modifiers() & QtCore.Qt.ControlModifier
        and event.modifiers() & QtCore.Qt.ShiftModifier
        and event.key() == QtCore.Qt.Key_E
    ):
        controller._debug_mode = True
        raise RuntimeError
    if (
        event.modifiers() & QtCore.Qt.ControlModifier
        and event.modifiers() & QtCore.Qt.ShiftModifier
        and event.key() == QtCore.Qt.Key_D
    ):
        cam = controller.sync_controller("camera")
        fi = cam.cs.get_full_info("all")
        with open("cam_info.dat", "a") as f:
            f.write(f"Camera settings on {datetime.datetime.now():on %Y/%m/%d at %H:%M:%S}\n\n")
            savefile.save_dict(fi, f)
            f.write("\n" * 4)
        controller._debug_mode = True
        raise RuntimeError
