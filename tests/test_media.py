import subprocess, time, pytest
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

def test_extract_audio_span(clip, tmp_path):
    # task-9: per-shot audio slice for transcription, named by shot_id under
    # out_dir (mirrors extract_keyframes's directory-based signature) rather
    # than extract_audio's single explicit-path signature.
    shot = media.Shot(shot_id="shot_0", t_start=0.5, t_end=1.5)
    out_dir = tmp_path / "audio"
    result = media.extract_audio_span(clip, shot, out_dir)
    assert result == out_dir / "shot_0.wav"
    assert result.exists() and result.stat().st_size > 0
    assert abs(media.probe_duration(result) - 1.0) < 0.2

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

# --- fix-round coverage: review findings on the loop-based edit functions ---

def _sample_rgb(path, t):
    # single averaged pixel at time t, via ffmpeg's own frame-accurate -ss seek
    r = subprocess.run([
        "ffmpeg", "-y", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-vf", "scale=1:1", "-",
    ], capture_output=True, timeout=60)
    px = r.stdout[:3]
    return tuple(px) if len(px) == 3 else None

def test_overlay_image_audio_less_source_completes_fast(solid_clip, png, tmp_path):
    # regression for review finding: -shortest alone only bounds the encode via a
    # mapped audio stream reaching EOF; an audio-less source has no such stream,
    # so the infinite -loop input previously ran the encode to the 60s timeout.
    out = tmp_path / "overlay_solid.mp4"
    started = time.monotonic()
    result = media.overlay_image(solid_clip, png, 0.2, 0.8, out)
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, f"took {elapsed:.1f}s, expected well under the 60s timeout"
    assert result == out
    assert out.exists() and out.stat().st_size > 0
    orig = media.probe_duration(solid_clip)
    new = media.probe_duration(out)
    assert abs(new - orig) < 0.3

def test_replace_segment_video_audio_less_source_completes_fast(solid_clip, frames_dir, tmp_path):
    # same regression as above, for the other loop-based edit function.
    out = tmp_path / "segment_solid.mp4"
    started = time.monotonic()
    result = media.replace_segment_video(solid_clip, 0.2, 0.8, frames_dir, out)
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, f"took {elapsed:.1f}s, expected well under the 60s timeout"
    assert result == out
    assert out.exists() and out.stat().st_size > 0
    orig = media.probe_duration(solid_clip)
    new = media.probe_duration(out)
    assert abs(new - orig) < 0.3

def test_replace_segment_video_frame_order_at_window_start(clip, frames_dir, tmp_path):
    # regression for review finding: the replacement frame shown right at t_start
    # must be the first frame in file order (f0.png, magenta), not whichever frame
    # the loop's own free-running clock happens to land on at that global time.
    out = tmp_path / "segment_order.mp4"
    media.replace_segment_video(clip, 0.5, 1.5, frames_dir, out)
    rgb = _sample_rgb(out, 0.55)
    assert rgb is not None
    r, g, _ = rgb
    # f0.png is magenta (high red, low green, high blue); f1.png is cyan (low
    # red, high green, high blue) -- red is the discriminator between them.
    assert r > 128 and g < 128, f"expected the first frame (magenta) at t_start, sampled {rgb}"
