"""Interactive viewing on top of pylablib's ``ImagePlotter``: zoom/pan, fit-to-view, pixel readout.

pylablib disables mouse interaction on its image view and re-fits the view on every frame,
so the image is always shown whole. :class:`ImageViewController` re-enables zoom (mouse
wheel) and pan (drag), keeps fitting the view to new frames only until the user zooms or
pans, and reports the value under the mouse cursor.
"""

import math

import numpy as np
from pylablib.core.gui import QtCore, Signal

# pylablib's own maps plus pyqtgraph's perceptual ones; any name ``ImagePlotter.set_colormap``
# accepts can be used
COLORMAPS = {
    "gray_sat": "Gray (red saturation)",
    "gray": "Gray",
    "gray_doublesat": "Gray (green/red saturation)",
    "hot": "Hot",
    "hot_sat": "Hot (blue saturation)",
    "viridis": "Viridis",
    "inferno": "Inferno",
    "magma": "Magma",
    "thermal": "Thermal",
}


def format_pixel_value(value):
    """Format a pixel value (scalar or color tuple) for display"""
    value = np.asarray(value)
    if value.ndim:
        return "(" + ", ".join(format_pixel_value(v) for v in value) + ")"
    if np.issubdtype(value.dtype, np.integer):
        return str(int(value))
    return f"{float(value):.4g}"


class ImageViewController(QtCore.QObject):
    """
    Zoom/pan, fit-to-view and pixel readout for a pylablib ``ImagePlotter``.

    Coordinates are those of the displayed image (after the plotter's transpose, flip and
    display binning), which is also the coordinate system of the plotter's crosshair lines.
    """

    pixel_hovered = Signal(str)  # "x, y: value", or "" when the cursor leaves the image

    def __init__(self, plotter):
        super().__init__(plotter)
        self.plotter = plotter
        self.image_view = plotter.image_window
        self.view_box = self.image_view.getView()
        self.image_item = self.image_view.getImageItem()
        self.auto_fit = True
        self.view_box.setMouseEnabled(x=True, y=True)
        self.view_box.sigRangeChangedManually.connect(self._on_manual_range_change)
        # pyqtgraph's ImageView.setImage re-fits the view by default, which pylablib calls
        # for every frame; only do that while the view hasn't been zoomed or panned
        set_image = self.image_view.setImage

        def set_image_keeping_view(img, *args, **kwargs):
            kwargs.setdefault("autoRange", self.auto_fit)
            return set_image(img, *args, **kwargs)

        self.image_view.setImage = set_image_keeping_view
        self.image_view.scene.sigMouseMoved.connect(self._on_mouse_moved)

    def _on_manual_range_change(self, *args):
        self.auto_fit = False

    def fit(self):
        """Show the whole image, and keep doing so for new frames until the next zoom/pan"""
        self.auto_fit = True
        self.view_box.autoRange()

    def _on_mouse_moved(self, scene_pos):
        image = self.image_item.image
        if image is None or not self.view_box.sceneBoundingRect().contains(scene_pos):
            self.pixel_hovered.emit("")
            return
        pos = self.image_item.mapFromScene(scene_pos)
        x, y = math.floor(pos.x()), math.floor(pos.y())
        # pyqtgraph's (default column-major) image axis order: image[x, y]
        if 0 <= x < image.shape[0] and 0 <= y < image.shape[1]:
            self.pixel_hovered.emit(f"x={x}, y={y}: {format_pixel_value(image[x, y])}")
        else:
            self.pixel_hovered.emit("")
