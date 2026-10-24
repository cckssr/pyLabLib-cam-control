"""Camera autodetection CLI helpers (camcontrol.detect)."""

from pylablib.core.fileio.loadfile import load_dict
from pylablib.core.fileio.savefile import save_dict
from pylablib.core.utils import dictionary

from camcontrol import detect


class _FailingDescriptor:
    _cam_kind = "Failing"
    _expands = None

    @classmethod
    def detect(cls, verbose=False, camera_descriptors=None):
        raise RuntimeError("vendor SDK exploded")


class _WorkingDescriptor:
    _cam_kind = "Working"
    _expands = None

    @classmethod
    def detect(cls, verbose=False, camera_descriptors=None):
        return {"cam1": {"kind": "Working"}}


def test_no_cameras_keeps_existing_settings(tmp_path, monkeypatch):
    cfg = tmp_path / "settings.cfg"
    save_dict(dictionary.Dictionary({"cameras": {"old": {"kind": "simulated"}}}), str(cfg))
    monkeypatch.setattr(detect, "camera_descriptors", {})
    detect.update_settings_file(str(cfg))
    assert load_dict(str(cfg))["cameras/old/kind"] == "simulated"


def test_failing_backend_does_not_abort_detection(monkeypatch):
    monkeypatch.setattr(
        detect, "camera_descriptors", {"Failing": _FailingDescriptor, "Working": _WorkingDescriptor}
    )
    cams = detect.detect_all()["cameras"]
    assert list(cams.keys()) == ["cam1"]
    assert cams["cam1", "display_name"] == "cam1"
