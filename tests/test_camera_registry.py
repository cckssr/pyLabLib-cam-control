"""
Camera descriptor registry.

Guards against the bug fixed in phase 2: one vendor backend failing to import
(e.g. a Windows-only C extension on macOS/Linux) used to take down the whole
registry. A loose lower bound is used instead of an exact count, since the
count is expected to grow as backends are added and shrink on platforms
missing a particular vendor SDK.
"""

from camcontrol.cameras.loader import camera_descriptors


def test_simulated_camera_always_registers():
    assert "simulated" in camera_descriptors


def test_most_camera_kinds_register():
    # As of writing there are 23 kinds; on a platform missing one vendor's C
    # extension (e.g. pylablib's PCO SC2 extension on macOS/Linux) there are 22.
    # A single broken backend must not take the count anywhere near zero.
    assert len(camera_descriptors) >= 15, sorted(camera_descriptors)


def test_descriptors_are_camera_descriptor_subclasses():
    from camcontrol.cameras.base import ICameraDescriptor

    for kind, desc in camera_descriptors.items():
        assert issubclass(desc, ICameraDescriptor), kind
