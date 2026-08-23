import subprocess, pytest
from customs import media

@pytest.fixture(scope="session")
def clip(tmp_path_factory):
    p = tmp_path_factory.mktemp("m") / "clip.mp4"
    # two visually distinct halves so scene detection has one cut
    subprocess.run([
        "ffmpeg","-y","-f","lavfi","-i","color=red:s=320x240:d=2",
        "-f","lavfi","-i","color=blue:s=320x240:d=2",
        "-f","lavfi","-i","anullsrc=r=16000:cl=mono",
        "-filter_complex","[0:v][1:v]concat=n=2:v=1[v]",
        "-map","[v]","-map","2:a","-t","4","-pix_fmt","yuv420p", str(p)],
        check=True, capture_output=True)
    return p

def test_duration(clip):
    assert 3.5 < media.probe_duration(clip) < 4.5

def test_shots_split_on_cut(clip):
    shots = media.detect_shots(clip)
    assert len(shots) == 2
    assert abs(shots[0].t_end - 2.0) < 0.5

def test_keyframes(clip, tmp_path):
    shots = media.detect_shots(clip)
    frames = media.extract_keyframes(clip, shots[0], tmp_path)
    assert len(frames) == 2 and frames[0].exists()

# --- additional coverage beyond the brief's mandated three ---

@pytest.fixture(scope="session")
def solid_clip(tmp_path_factory):
    # single unchanging color, no scene cut anywhere in it
    p = tmp_path_factory.mktemp("m") / "solid.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=green:s=320x240:d=1.5",
        "-pix_fmt", "yuv420p", str(p)],
        check=True, capture_output=True, timeout=60)
    return p

@pytest.fixture(scope="session")
def png(tmp_path_factory):
    # small PNG built offline via lavfi, used as an overlay patch
    p = tmp_path_factory.mktemp("img") / "patch.png"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=yellow:s=320x240:d=1",
        "-frames:v", "1", str(p)],
        check=True, capture_output=True, timeout=60)
    return p

@pytest.fixture(scope="session")
def wav(tmp_path_factory):
    # short beep tone built offline via lavfi sine source
    p = tmp_path_factory.mktemp("aud") / "beep.wav"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=1:sample_rate=16000",
        "-ac", "1", str(p)],
        check=True, capture_output=True, timeout=60)
    return p

@pytest.fixture(scope="session")
def frames_dir(tmp_path_factory):
    # a small directory of replacement frames, built offline via lavfi
    d = tmp_path_factory.mktemp("frames")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=magenta:s=320x240:d=1",
        "-frames:v", "1", str(d / "f0.png")],
        check=True, capture_output=True, timeout=60)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=cyan:s=320x240:d=1",
        "-frames:v", "1", str(d / "f1.png")],
        check=True, capture_output=True, timeout=60)
    return d

def test_shots_no_cuts_returns_one_shot(solid_clip):
    duration = media.probe_duration(solid_clip)
    shots = media.detect_shots(solid_clip)
    assert len(shots) == 1
    assert shots[0].t_start == 0.0
    assert abs(shots[0].t_end - duration) < 0.05

def test_extract_audio(clip, tmp_path):
    out = tmp_path / "audio.wav"
    result = media.extract_audio(clip, out)
    assert result == out
    assert out.exists() and out.stat().st_size > 0
    assert 3.5 < media.probe_duration(out) < 4.5

def test_overlay_image_preserves_duration(clip, png, tmp_path):
    out = tmp_path / "overlay.mp4"
    result = media.overlay_image(clip, png, 0.5, 1.5, out)
    assert result == out
    assert out.exists() and out.stat().st_size > 0
    orig = media.probe_duration(clip)
    new = media.probe_duration(out)
    assert abs(new - orig) < 0.3

def test_replace_audio_span_preserves_duration(clip, wav, tmp_path):
    out = tmp_path / "audio_replaced.mp4"
    result = media.replace_audio_span(clip, wav, 0.5, 1.5, out)
    assert result == out
    assert out.exists() and out.stat().st_size > 0
    orig = media.probe_duration(clip)
    new = media.probe_duration(out)
    assert abs(new - orig) < 0.3

def test_replace_segment_video_preserves_duration(clip, frames_dir, tmp_path):
    out = tmp_path / "segment_replaced.mp4"
    result = media.replace_segment_video(clip, 0.5, 1.5, frames_dir, out)
    assert result == out
    assert out.exists() and out.stat().st_size > 0
    orig = media.probe_duration(clip)
    new = media.probe_duration(out)
    assert abs(new - orig) < 0.3

def test_media_error_on_bad_input(tmp_path):
    with pytest.raises(media.MediaError):
        media.probe_duration(tmp_path / "does_not_exist.mp4")
