from collections import deque
import io
import subprocess

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


def test_running_requires_new_output_frames_and_duplicate_counts_do_not_hide_a_stall():
    now = [100.0]
    progress = frame.FrameProgress(clock=lambda: now[0])
    progress.read(io.StringIO("frame=0\nfps=18\nprogress=continue\n"))
    now[0] += frame.STARTUP_TIMEOUT
    assert progress.status() == (0, frame.STARTUP_TIMEOUT, True)

    progress.read(io.StringIO("frame=   1\nframe=invalid\n"))
    assert progress.status() == (1, 0, False)
    now[0] += frame.FRAME_TIMEOUT
    progress.read(io.StringIO("frame=1\nframe=0\n"))
    assert progress.status() == (1, frame.FRAME_TIMEOUT, True)
    progress.read(io.StringIO("frame=8\n"))
    assert progress.status() == (8, 0, False)


def test_noisy_ffmpeg_stream_retains_only_the_most_recent_diagnostic_lines():
    tail = deque(maxlen=10)
    lines = (f"warning {n}\n" for n in range(5000))
    frame.read_errors(lines, tail)
    frame.read_errors(["Last message repeated 32 times\n", "\n"], tail)
    assert list(tail) == [f"warning {n}" for n in range(4990, 5000)]


def test_worker_reports_and_recovers_from_a_wedged_writer_without_changing_the_crop(tmp_path, monkeypatch):
    now = [0.0]
    statuses, commands, signals = [], [], {}
    config = {**frame.DEFAULTS, "zone": [0.2, 0.2, 0.5, 0.5]}
    monkeypatch.setattr(frame, "STATE_DIR", tmp_path)
    monkeypatch.setattr(frame, "PREVIEW", tmp_path / "preview.jpg")
    monkeypatch.setattr(frame, "load_config", lambda: config.copy())
    monkeypatch.setattr(frame, "loopback_ready", lambda: True)
    monkeypatch.setattr(frame, "find_camera", lambda _: "/dev/video2")
    monkeypatch.setattr(frame, "config_stamp", lambda: 1)
    monkeypatch.setattr(frame, "preview_wanted", lambda: False)
    monkeypatch.setattr(frame, "write_status", lambda **status: statuses.append(status))
    monkeypatch.setattr(frame.os, "umask", lambda _: 0)
    monkeypatch.setattr(frame.signal, "signal", lambda sig, callback: signals.update({sig: callback}))
    monkeypatch.setattr(frame.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(frame.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    clocked_progress = frame.FrameProgress
    monkeypatch.setattr(frame, "FrameProgress", lambda: clocked_progress(clock=lambda: now[0]))

    # The stand-in writer stays alive but never reports an output frame.
    class WedgedWriter:
        def __init__(self, command, **kwargs):
            self.returncode = None
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")
            commands.append(command)
            if len(commands) == 2:
                signals[frame.signal.SIGTERM](None, None)

        def poll(self): return self.returncode
        def terminate(self): self.returncode = -1
        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            return self.returncode

    monkeypatch.setattr(frame.subprocess, "Popen", WedgedWriter)
    frame.run()

    assert [s["state"] for s in statuses] == ["starting", "error", "starting", "stopped"]
    assert len(commands) == 2 and commands[0] == commands[1]
    assert "crop=" in commands[1][commands[1].index("-filter_complex") + 1]
    assert commands[1][commands[1].index("-progress") + 1] == "pipe:1"
    assert str(frame.PREVIEW) not in commands[1]
