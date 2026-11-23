import frame


def test_validate_clamps_a_zone_inside_the_frame():
    c = frame.validate({**frame.DEFAULTS, "zone": [0.9, -0.2, 0.5, 0.5]})
    x, y, w, h = c["zone"]
    assert (x, y, w, h) == (0.5, 0.0, 0.5, 0.5)


def test_validate_rejects_rubbish():
    c = frame.validate({**frame.DEFAULTS, "zone": ["a"], "strength": "x", "output": "800x600", "fps": 999})
    assert c["zone"] is None and c["strength"] == 0.5 and c["output"] == "1280x720" and c["fps"] == 30


def test_crop_is_on_even_pixels():
    assert frame.crop_filter([0.1, 0.2, 0.5, 0.5], "1920x1080") == "crop=960:540:192:216"
    assert frame.crop_filter(None, "1920x1080") is None


def test_filter_graph_crops_brightens_and_previews():
    c = frame.validate({**frame.DEFAULTS, "zone": [0.25, 0.25, 0.5, 0.5]})
    graph, snap = frame.filter_graph(c, preview=False)
    assert "crop=960:540:480:270" in graph and "normalize" in graph and "hqdn3d" in graph and not snap
    assert graph.endswith("format=yuyv422[out]")
    graph, snap = frame.filter_graph({**c, "low_light": False}, preview=True)
    assert snap and "split=2" in graph and "normalize" not in graph


def test_command_only_writes_a_preview_when_asked():
    c = frame.validate(dict(frame.DEFAULTS))
    assert str(frame.PREVIEW) not in frame.command("/dev/video2", c, False)
    assert str(frame.PREVIEW) in frame.command("/dev/video2", c, True)


def test_camera_node_matches_the_akaso():
    listing = "HD Webcam: HD Webcam (usb-0000:00:14.0-13):\n\t/dev/video0\n\nBrave 7 LE (usb-0000:00:14.0-4):\n\t/dev/video2\n\t/dev/video3\n"
    assert frame.camera_node(listing, "brave 7 le") == "/dev/video2"
    assert frame.camera_node(listing, "sony") is None


LISTING = ("HD Webcam: HD Webcam (usb-0000:00:14.0-13):\n\t/dev/video0\n\t/dev/video1\n\t/dev/media0\n\n"
           "ZV-1F (usb-0000:00:14.0-3):\n\t/dev/video2\n\t/dev/video3\n\t/dev/media1\n\n"
           "Brave 7 LE (usb-0000:00:14.0-4):\n\t/dev/video4\n\t/dev/video5\n")

SONY_FORMATS = """ioctl: VIDIOC_ENUM_FMT
\tType: Video Capture

\t[0]: 'MJPG' (Motion-JPEG, compressed)
\t\tSize: Discrete 1280x720
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t[1]: 'YUYV' (YUYV 4:2:2)
\t\tSize: Discrete 1280x720
\t\t\tInterval: Discrete 0.033s (30.000 fps)
"""


def test_the_sony_wins_over_the_akaso_and_the_laptop_webcam_is_never_used():
    assert frame.pick_camera(LISTING, "auto") == ("/dev/video2", "zv-1f")
    assert frame.pick_camera(LISTING, "brave 7 le") == ("/dev/video2", "zv-1f")  # old default means auto
    without_sony = LISTING.replace("ZV-1F (usb-0000:00:14.0-3):\n\t/dev/video2\n\t/dev/video3\n\t/dev/media1\n\n", "")
    assert frame.pick_camera(without_sony, "auto") == ("/dev/video4", "brave 7 le")
    only_webcam = LISTING.split("\n\n")[0]
    assert frame.pick_camera(only_webcam, "auto") == (None, None)


def test_the_sony_runs_at_its_own_720p30_and_is_slowed_to_the_stream_rate():
    config = frame.validate(dict(frame.DEFAULTS))
    assert frame.choose_mode(frame.parse_modes(SONY_FORMATS), config) == ("mjpeg", "1280x720", 30.0)
    yuyv_only = SONY_FORMATS.split("\t[0]")[0] + "\t[0]" + SONY_FORMATS.split("\t[1]")[1]
    assert frame.choose_mode(frame.parse_modes(yuyv_only), config) == ("yuyv422", "1280x720", 30.0)
    sony = {**config, "input_format": "mjpeg", "capture": "1280x720", "capture_fps": 30.0}
    cmd = frame.command("/dev/video2", sony, False)
    assert cmd[cmd.index("-video_size") + 1] == "1280x720" and cmd[cmd.index("-framerate") + 1] == "30"
    graph, _ = frame.filter_graph(sony, False)
    assert graph.startswith("[0:v]fps=15,scale=1280:720")


def test_the_akaso_keeps_1080p_mjpeg_at_15():
    akaso = """\t[0]: 'MJPG' (Motion-JPEG, compressed)
\t\tSize: Discrete 3840x2160
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\tSize: Discrete 1920x1080
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\t\tInterval: Discrete 0.067s (15.000 fps)
"""
    config = frame.validate(dict(frame.DEFAULTS))
    assert frame.choose_mode(frame.parse_modes(akaso), config) == ("mjpeg", "1920x1080", 15.0)
    assert frame.choose_mode({}, config) is None
