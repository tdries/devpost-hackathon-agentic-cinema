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


# --- task 14: deterministic flash detection, image fitting, reframe crop ---

@pytest.fixture(scope="session")
def strobe_clip(tmp_path_factory):
    """3s dark clip carrying a deterministic 6 flashes/second white strobe
    between t=1.0 and t=2.5.

    Same recipe as the strobe planted in docs/samples/test_ad.mp4 by
    scripts/make_test_ad.py (a full-frame white source overlaid on the
    2-frames-on/2-frames-off phase of a 24fps clock, i.e. one flash every 4
    frames = 6 per second), built here from lavfi so the unit test never
    depends on the sample asset.
    """
    p = tmp_path_factory.mktemp("strobe") / "strobe.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=0x102030:s=320x240:r=24:d=3",
        "-f", "lavfi", "-i", "color=c=white:s=320x240:r=24:d=3",
        "-filter_complex",
        "[0:v][1:v]overlay=enable='between(t,1.0,2.5)*lt(mod(floor(t*24),4),2)'"
        ",format=yuv420p[v]",
        "-map", "[v]", "-t", "3", str(p)],
        check=True, capture_output=True, timeout=60)
    return p

def test_detect_flashes_measures_the_planted_rate(strobe_clip):
    windows = media.detect_flashes(strobe_clip)
    assert len(windows) == 1, f"expected one strobe window, got {windows}"
    w = windows[0]
    assert 0.95 <= w.t_start <= 1.15
    assert 2.35 <= w.t_end <= 2.6
    # the planted strobe is exactly 6 flashes/second; allow half a flash of
    # slack for the first/last edge landing on a frame boundary.
    assert abs(w.flashes_per_second - 6.0) < 0.5, w

def test_detect_flashes_ignores_a_plain_cut(clip):
    # one red-to-blue cut at t=2.0 is a single luminance step, not flashing.
    assert media.detect_flashes(clip) == []

def test_detect_flashes_ignores_a_still_clip(solid_clip):
    assert media.detect_flashes(solid_clip) == []

def test_probe_resolution(clip):
    assert media.probe_resolution(clip) == (320, 240)

def test_fit_image_matches_the_video_resolution(clip, tmp_path):
    # a deliberately wrong-sized PNG (the shape an image model hands back)
    src = tmp_path / "wrong_size.png"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=yellow:s=1024x1024:d=1",
        "-frames:v", "1", str(src)], check=True, capture_output=True, timeout=60)
    out = media.fit_image(src, clip, tmp_path / "fitted.png")
    assert media.probe_resolution(out) == (320, 240)

def test_crop_span_preserves_duration_and_resolution(clip, tmp_path):
    out = tmp_path / "reframed.mp4"
    result = media.crop_span(clip, 0.5, 1.5, out)
    assert result == out and out.exists()
    assert media.probe_resolution(out) == media.probe_resolution(clip)
    assert abs(media.probe_duration(out) - media.probe_duration(clip)) < 0.3


def test_long_takes_are_sampled_more_densely_than_short_cuts(clip, tmp_path):
    """The fixed two-frames-per-shot rate is what let a 43 second cigarette
    ad clear the EU: judged on four stills, none showing tobacco. Frame
    count now follows the shot's own duration."""
    assert media.frames_for(1.0) == 2          # a quick cut still gets two
    assert media.frames_for(25.0) == 8         # a long take gets the cap
    assert media.frames_for(9.0) == 3
    assert media.frames_for(120.0) == 8        # capped, not unbounded

    short = media.Shot(shot_id="shot_s", t_start=0.0, t_end=1.0)
    long_take = media.Shot(shot_id="shot_l", t_start=0.0, t_end=25.0)
    assert len(media.extract_keyframes(clip, short, tmp_path / "a")) == 2
    assert len(media.extract_keyframes(clip, long_take, tmp_path / "b")) > 2
    # an explicit count still wins: remediation edits exactly one frame
    assert len(media.extract_keyframes(clip, long_take, tmp_path / "c", per_shot=1)) == 1
