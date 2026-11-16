import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import bambu, camera


def _prn(model, code="ab/c+1"):
    p = bambu.BambuPrinter.__new__(bambu.BambuPrinter)
    p.name, p.ip, p.access_code, p.model = "T", "192.168.1.50", code, model
    import threading
    p._camera, p._camera_guard = None, threading.Lock()
    return p


def test_h2_and_x1_use_rtsps_builtin():
    for m in ("Bambu Lab H2D", "H2S", "Bambu Lab X1 Carbon", "X1E"):
        p = _prn(m)
        assert p.has_camera and p.uses_rtsp_camera
        cam = p.camera()
        assert isinstance(cam, camera.RtspCameraStream)
        assert p.builtin_camera_url() == "rtsps://bblp:ab%2Fc%2B1@192.168.1.50:322/streaming/live/1"


def test_p1_a1_keep_jpeg_6000():
    for m in ("Bambu Lab P1S", "A1", "A1 mini", None, ""):
        p = _prn(m)
        assert p.has_camera and not p.uses_rtsp_camera
        assert isinstance(p.camera(), camera.CameraStream)


def test_typed_camera_address_still_wins():
    p = camera.apply_camera_url(_prn("Bambu Lab H2D"), "rtsp://10.0.0.5/live")
    assert p.has_camera and p._override_url == "rtsp://10.0.0.5/live"
