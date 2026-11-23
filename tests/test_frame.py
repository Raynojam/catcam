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
