from PyQt5 import QtWidgets, QtCore, QtGui

import sys
import ctypes

from camcontrol import version
from camcontrol.resources import resource_path


def prepare_app():
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_Use96Dpi, True)
    return QtWidgets.QApplication([])


class SplashScreen(QtWidgets.QSplashScreen):
    """
    Splash screen widget.

    Shows logo, message, and version label.
    """

    def __init__(self):
        pixmap = QtGui.QPixmap(resource_path("splash.png"))
        super().__init__(pixmap)
        self.setObjectName("camControlSplash")
        self.current_style = QtCore.QCoreApplication.instance().style()
        self.setStyle(self.current_style)
        self.current_font = self.font()
        self.current_font.setPixelSize(16)
        self.setFont(self.current_font)
        self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint)
        self.setLayout(QtWidgets.QVBoxLayout(self))
        self.layout().addItem(
            QtWidgets.QSpacerItem(
                1,
                1,
                QtWidgets.QSizePolicy.MinimumExpanding,
                QtWidgets.QSizePolicy.MinimumExpanding,
            )
        )
        self.layout().setContentsMargins(5, 5, 5, 5)
        self.vlabel = QtWidgets.QLabel(self)
        self.vlabel.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom)
        self.vlabel.setText("PyLabLib cam-control\nVersion {}".format(version))
        self.vlabel.setStyle(self.current_style)
        self.layout().addWidget(self.vlabel)
        self.vlabel.setFont(self.current_font)

    def show_message(self, text):
        self.showMessage(text, QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom)


def get_splash_screen(name="camControlSplash"):
    """Get splash screen if present"""
    widgets = QtCore.QCoreApplication.instance().topLevelWidgets()
    for w in widgets:
        if w.objectName() == name:
            return w


def update_splash_screen(show=None, msg=None):
    """Show or hide splash screen and / or change its message"""
    splash = get_splash_screen()
    if splash is not None:
        if show is True:
            splash.show()
        elif show is False:
            splash.hide()
        if msg is not None:
            splash.show_message(msg)


def main():
    """Show the splash screen, then hand off to the main application."""
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "cam-control"
        )  # fixes taskbar icon issue
    app = prepare_app()
    splash = SplashScreen()
    splash.show_message("Setting up the environment...")
    splash.show()
    app.processEvents()
    # Minimize/maximize to bring the window to the front
    splash.showMinimized()
    app.processEvents()
    splash.setWindowState(splash.windowState() & ~QtCore.Qt.WindowMinimized)
    app.processEvents()

    from camcontrol import app as camapp

    camapp.main(app=app)


if __name__ == "__main__":
    main()
