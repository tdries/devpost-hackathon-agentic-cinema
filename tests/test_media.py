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


def test_splice_clip_actually_replaces_the_span(tmp_path):
    """The bridge's whole point is that the generated seconds land. The first
    version normalised the patch's timestamps and also offset the input,
    which cancelled out: the patch ended before the overlay window opened and
    the original passed through, so a master cost a Veo generation and came
    back identical. Measured in pixels, not trusted from a log line."""
    base = tmp_path / "base.mp4"
    patch = tmp_path / "patch.mp4"
    out = tmp_path / "out.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:s=320x240:d=4",
                    "-pix_fmt", "yuv420p", str(base)], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=blue:s=320x240:d=4",
                    "-pix_fmt", "yuv420p", str(patch)], check=True, capture_output=True)

    media.splice_clip(base, patch, 1.0, 2.0, out)
    assert out.exists() and out.stat().st_size > 0

    def channel_at(second):
        frame = tmp_path / f"f{second}.png"
        subprocess.run(["ffmpeg", "-y", "-ss", str(second), "-i", str(out),
                        "-frames:v", "1", str(frame)], check=True, capture_output=True)
        from PIL import Image
        return Image.open(frame).convert("RGB").getpixel((160, 120))

    inside = channel_at(1.5)
    before = channel_at(0.4)
    after = channel_at(3.0)
    assert inside[2] > inside[0], f"the span was not replaced: {inside}"   # blue
    assert before[0] > before[2], f"before the span changed: {before}"     # red
    assert after[0] > after[2], f"after the span changed: {after}"         # red


def test_splice_retimes_the_clip_so_the_landing_frame_arrives(tmp_path):
    """The last frame Veo was conditioned to reach must reach the screen.

    Veo will not emit less than four seconds, so a short span comes back
    over-long. splice_clip used to keep the first `span` seconds and drop
    the rest: the motion played in slow motion against untouched audio, and
    the corrected tail anchor never appeared, so the patch jump-cut back to
    the original at the out point. Measured on this instance's own bridges:
    18%, 21%, 40% and 99% shown, and the landing frame in none of them.

    A green->blue clip spliced into a 1s hole must therefore end BLUE.
    Trimming the first second of a 4s clip would leave it green.
    """
    base = tmp_path / "base.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:s=160x120:d=4",
        "-pix_fmt", "yuv420p", str(base)], check=True, capture_output=True, timeout=60)

    # 4 seconds: green for the first three, blue for the last one
    clip = tmp_path / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=green:s=160x120:d=3",
        "-f", "lavfi", "-i", "color=blue:s=160x120:d=1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
        "-pix_fmt", "yuv420p", str(clip)], check=True, capture_output=True, timeout=60)

    out = tmp_path / "out.mp4"
    media.splice_clip(base, clip, 1.0, 2.0, out)   # a 1s hole for a 4s clip

    def rgb_at(when):
        png = tmp_path / f"f{when}.png"
        subprocess.run(["ffmpeg", "-y", "-ss", str(when), "-i", str(out),
                        "-frames:v", "1", str(png)], check=True,
                       capture_output=True, timeout=60)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(png), "-f",
                              "rawvideo", "-pix_fmt", "rgb24", "-"],
                             check=True, capture_output=True, timeout=60).stdout
        mid = (len(raw) // 3 // 2) * 3
        return raw[mid], raw[mid + 1], raw[mid + 2]

    r, g, b = rgb_at(1.9)          # near the end of the patched span
    assert b > 120 and r < 90, f"landing frame never arrived: rgb={(r, g, b)}"

    r, g, b = rgb_at(1.1)          # the start of the span is still the clip
    assert g > 90, f"the patch did not start on the clip: rgb={(r, g, b)}"

    r, g, b = rgb_at(3.0)          # outside the span: the brand's own footage
    assert r > 120 and b < 90, f"original footage was disturbed: rgb={(r, g, b)}"


def test_a_dissolve_is_a_cut_even_though_no_two_frames_differ_by_much():
    """A hard cut scores high in one frame. A dissolve spreads the same
    change over twenty, so no single pair differs by much and a fixed
    threshold sees nothing at all.

    These numbers are not invented. They are the measured scene-score
    profile of a real 1990s Marlboro commercial, 15.2s at 640x480: the
    highest score in the entire film is 0.1906, and the changes arrive in
    clusters of adjacent frames -- 2.33/2.37/2.43 and 14.30/14.37/14.43 --
    which is the signature of a dissolve rather than a cut.

    At the hard-cut threshold that ad is ONE shot. Every observation in it
    then spanned the whole commercial, every finding inherited that span,
    and the fix picker offered to regenerate 15.2 seconds of video, which
    Veo refuses because its ceiling is 8. The button was greyed out for a
    film that was never one shot.

    Driven as data rather than as a video, because ffmpeg's scene metric
    on a synthetic cross-fade is far gentler than on real grainy footage
    -- a fixture built from lavfi sources would test the fixture, not this.
    """
    duration = 15.17
    profile = [
        (2.33, 0.0922), (2.37, 0.0788), (2.43, 0.0829),      # dissolve one
        (4.37, 0.0222), (5.27, 0.0204), (5.37, 0.0208), (5.67, 0.0291),
        (6.93, 0.0202), (8.10, 0.0174), (8.30, 0.0185), (8.80, 0.0191),
        (10.13, 0.1906), (10.17, 0.0908), (10.20, 0.0192),   # dissolve two
        (14.10, 0.0201), (14.30, 0.0453), (14.37, 0.0207), (14.43, 0.0162),
        (14.80, 0.0192),                                      # fade to black
    ]

    # the old behaviour, and the whole problem
    assert media._cuts_at(profile, 0.30, duration) == []

    cuts = None
    for threshold in media._SCENE_LADDER:
        cuts = media._cuts_at(profile, threshold, duration)
        longest = max(b - a for a, b in zip([0.0] + cuts, cuts + [duration]))
        if longest <= media._LONGEST_USEFUL_S:
            break
    assert cuts, "the ladder never found the dissolves"
    longest = max(b - a for a, b in zip([0.0] + cuts, cuts + [duration]))
    assert longest <= media._LONGEST_USEFUL_S, (
        f"longest take {longest:.1f}s is still past what Veo will bridge")

    # the boundaries land on the transitions, not on the noise between them
    assert any(abs(c - 2.33) < 0.2 for c in cuts), cuts
    assert any(abs(c - 10.13) < 0.2 for c in cuts), cuts


def test_one_transition_yields_one_boundary_not_one_per_frame():
    """A dissolve trips several adjacent frames. They are one transition
    and deserve one boundary, at its strongest frame -- otherwise a soft
    cut becomes four micro-shots that merge_micro_shots has to sweep up.
    """
    cluster = [(2.33, 0.09), (2.37, 0.08), (2.43, 0.11), (2.47, 0.07)]
    cuts = media._cuts_at(cluster, 0.05, 20.0)
    assert cuts == [2.43], f"expected the strongest frame of the cluster, got {cuts}"


def test_a_film_of_hard_cuts_still_stops_on_the_first_rung(tmp_path):
    """Walking down the ladder is for material that needs it. Sharp
    footage must not be re-thresholded into confetti just because the
    option exists -- the first rung is the hard-cut threshold, and a film
    whose takes are already short enough never leaves it.
    """
    from customs import media

    if True:
        clip = tmp_path / "cuts.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-v", "quiet",
            "-f", "lavfi", "-i", "color=c=red:s=320x240:d=3:r=25",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=3:r=25",
            "-f", "lavfi", "-i", "color=c=green:s=320x240:d=3:r=25",
            "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0",
            str(clip),
        ], check=True)

        scores = media._scene_scores(clip)
        assert max(s for _, s in scores) > 0.30, "hard cuts should score high"
        shots = media.detect_shots(clip)
        # three colours, two cuts -- not two hundred
        assert len(shots) == 3, [(round(s.t_start, 2), round(s.t_end, 2)) for s in shots]


def _frame_at(path, t, out_png):
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-ss", f"{t:.3f}", "-i", str(path),
                    "-frames:v", "1", str(out_png)], check=True)
    return out_png


def test_a_matte_leaves_every_pixel_outside_it_alone(tmp_path):
    """The contract the whole remediation path rests on.

    An image model asked to change a bottle does not change only the
    bottle: one real relettering edit moved 37.9% of the frame to alter
    something occupying 1.3% of it. Cropping the answer back to the
    question is what makes "the rest of the scene is untouched" a
    measurement instead of a hope.

    Measured against a CONTROL rather than against the original, because
    h264 is lossy and re-encoding moves every pixel a little whether or
    not anything was edited. The control is the same encode with no patch
    at all, so any difference between it and the composite is the PATCH,
    which is the thing this actually has to bound. (Getting rid of the
    encode loss itself is a different job: render once from an edit list.)
    """
    from PIL import Image, ImageChops, ImageDraw
    from customs import media

    base = tmp_path / "base.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", "testsrc2=s=320x240:d=4:r=25", str(base)], check=True)

    # a patch that is loudly different everywhere, so any leak is obvious
    patch = tmp_path / "patch.png"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", "color=c=magenta:s=320x240", "-frames:v", "1", str(patch)],
                   check=True)

    box = [250, 250, 750, 750]           # the middle, 0-1000 normalised
    out = tmp_path / "out.mp4"
    # crf=0 is lossless: this test is about where the FILTER puts pixels,
    # and a lossy encode redistributes bits across the whole frame in
    # response to a change anywhere in it, which would drown the signal.
    media.composite_matte(base, patch, box, 1.0, 3.0, out, crf=0)

    # the control: identical encode, patch cropped to a corner that does
    # not overlap the box under test
    control = tmp_path / "control.mp4"
    media.composite_matte(base, patch, [0, 0, 20, 20], 1.0, 3.0, control, crf=0)

    w, h = media.probe_resolution(base)
    x, y, bw, bh = media.box_to_pixels(box, w, h)

    a = Image.open(_frame_at(control, 2.0, tmp_path / "a.png")).convert("RGB")
    b = Image.open(_frame_at(out,     2.0, tmp_path / "b.png")).convert("RGB")
    assert a.size == b.size, "the composite must not resize the film"

    diff = ImageChops.difference(a, b)
    pad = media.MATTE_FEATHER_PX + 2
    ImageDraw.Draw(diff).rectangle(
        [x - pad, y - pad, x + bw + pad, y + bh + pad], fill=(0, 0, 0))
    # also blank the control's own tiny patch, which is not under test here
    cx, cy, cw, ch = media.box_to_pixels([0, 0, 20, 20], w, h)
    ImageDraw.Draw(diff).rectangle(
        [cx - pad, cy - pad, cx + cw + pad, cy + ch + pad], fill=(0, 0, 0))

    worst = diff.convert("L").getextrema()[1]
    assert worst == 0, (
        f"the patch reached {worst} levels outside its matte; it must reach none")

    # ...and it did do something inside it
    inside_a = a.crop((x, y, x + bw, y + bh))
    inside_b = b.crop((x, y, x + bw, y + bh))
    assert ImageChops.difference(inside_b, inside_a).convert("L").getextrema()[1] > 60, \
        "nothing changed inside the matte -- the patch never landed"


def test_a_matte_only_applies_inside_its_span(tmp_path):
    """Outside [t_start, t_end) the film is the film, including at the
    in-point -- the frame that used to be left showing the violation."""
    from PIL import Image, ImageChops
    from customs import media

    base = tmp_path / "base.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", "testsrc2=s=320x240:d=4:r=25", str(base)], check=True)
    patch = tmp_path / "patch.png"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", "color=c=magenta:s=320x240", "-frames:v", "1", str(patch)],
                   check=True)
    out = tmp_path / "out.mp4"
    media.composite_matte(base, patch, [250, 250, 750, 750], 1.0, 3.0, out)

    for t in (0.2, 3.6):
        a = Image.open(_frame_at(base, t, tmp_path / f"a{t}.png")).convert("RGB")
        b = Image.open(_frame_at(out,  t, tmp_path / f"b{t}.png")).convert("RGB")
        # re-encode is lossy, so allow a small delta rather than demanding equality
        stat = ImageChops.difference(a, b).convert("L").getextrema()
        assert stat[1] < 40, f"frame at {t}s changed outside the span (max delta {stat[1]})"


def test_box_to_pixels_pads_clamps_and_stays_even(tmp_path):
    """libx264 refuses an odd crop in yuv420p, and a box drawn tight to a
    bottle clips the shadow the edit also has to deal with."""
    from customs import media

    x, y, w, h = media.box_to_pixels([250, 250, 750, 750], 1280, 720)
    assert x % 2 == 0 and y % 2 == 0 and w % 2 == 0 and h % 2 == 0
    # padded outward past the raw 25%..75%
    assert x < 320 and y < 180 and w > 640 and h > 360

    # a box at the very edge stays inside the frame
    x, y, w, h = media.box_to_pixels([0, 0, 1000, 1000], 1280, 720)
    assert x == 0 and y == 0 and x + w <= 1280 and y + h <= 720

    import pytest as _pytest
    with _pytest.raises(media.MediaError):
        media.box_to_pixels([500, 500, 500, 500], 1280, 720)


def test_relight_carries_a_colour_change_without_carrying_the_lighting(tmp_path):
    """The clean-plate trick, checked on the property that makes it work.

    A model edit is one frame under one lighting condition. Dividing the
    edited frame by the original divides that lighting out and leaves the
    albedo change; multiplying it back into a DIFFERENT frame re-applies
    the change under that frame's own light.

    So: build an "original" and an "edited" that differ only in hue, and
    a second frame that is the original at half brightness -- a later
    moment in the same shot as the light falls. Applying the ratio to the
    dim frame must give the NEW hue at the DIM brightness. If the ratio
    were carrying lighting instead of albedo, it would drag the bright
    exposure along with it.
    """
    from PIL import Image
    from customs import media

    box = [200, 200, 800, 800]
    orig = tmp_path / "orig.png"
    edit = tmp_path / "edit.png"
    Image.new("RGB", (160, 120), (200, 100, 100)).save(orig)   # reddish
    Image.new("RGB", (160, 120), (100, 200, 100)).save(edit)   # greenish

    ratio = media.relight_ratio(orig, edit, box, tmp_path / "ratio.png")
    r = Image.open(ratio).convert("RGB")
    px = r.load()[r.size[0] // 2, r.size[1] // 2]

    # encoded as 128 == 1.0, so: red halves (0.5 -> 64), green doubles (2.0 -> 255)
    assert 55 <= px[0] <= 75, f"red ratio {px[0]} should encode ~0.5"
    assert px[1] >= 250, f"green ratio {px[1]} should encode >=2.0 (clipped)"

    # and the ratio is dimensionless: the SAME ratio applied to a frame at
    # half the brightness must land at half the brightness, new hue
    dim = (100, 50, 50)
    out = tuple(min(255, int(dim[c] * (px[c] / 128) * 1.0)) for c in range(3))
    assert 45 <= out[0] <= 55, f"red should stay dim ({out[0]})"
    assert out[1] >= 95, f"green should have risen but stayed dim-ish ({out[1]})"


def test_relight_leaves_near_black_pixels_alone(tmp_path):
    """A pixel that is nearly black carries no reliable colour, and its
    ratio explodes. Clamp it to 1 rather than let it blow out."""
    from PIL import Image
    from customs import media

    orig = tmp_path / "o.png"
    edit = tmp_path / "e.png"
    Image.new("RGB", (80, 60), (2, 2, 2)).save(orig)      # essentially black
    Image.new("RGB", (80, 60), (200, 200, 200)).save(edit)

    ratio = media.relight_ratio(orig, edit, [0, 0, 1000, 1000], tmp_path / "r.png")
    px = Image.open(ratio).convert("RGB").load()[10, 10]
    assert px == (128, 128, 128), f"near-black should encode 1.0, got {px}"


def test_relight_applies_only_inside_the_matte(tmp_path):
    """Same contract as composite_matte: outside the box, nothing moves."""
    from PIL import Image, ImageChops, ImageDraw
    from customs import media

    base = tmp_path / "base.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", "testsrc2=s=320x240:d=3:r=25", str(base)], check=True)
    ratio = tmp_path / "ratio.png"
    Image.new("RGB", (320, 240), (255, 64, 64)).save(ratio)   # loud, non-neutral

    box = [300, 300, 700, 700]
    out = tmp_path / "out.mp4"
    media.apply_relight(base, ratio, box, 0.5, 2.5, out, crf=0)
    control = tmp_path / "ctl.mp4"
    media.apply_relight(base, ratio, [0, 0, 20, 20], 0.5, 2.5, control, crf=0)

    w, h = media.probe_resolution(base)
    x, y, bw, bh = media.box_to_pixels(box, w, h)
    a = Image.open(_frame_at(control, 1.5, tmp_path / "a.png")).convert("RGB")
    b = Image.open(_frame_at(out, 1.5, tmp_path / "b.png")).convert("RGB")

    diff = ImageChops.difference(a, b)
    pad = media.MATTE_FEATHER_PX + 2
    ImageDraw.Draw(diff).rectangle([x - pad, y - pad, x + bw + pad, y + bh + pad], fill=(0, 0, 0))
    cx, cy, cw, ch = media.box_to_pixels([0, 0, 20, 20], w, h)
    ImageDraw.Draw(diff).rectangle([cx - pad, cy - pad, cx + cw + pad, cy + ch + pad], fill=(0, 0, 0))
    assert diff.convert("L").getextrema()[1] == 0, "relight leaked outside its matte"


def test_craft_check_passes_a_matte_edit_and_catches_a_shortened_film(tmp_path):
    """The questions an editor asks before accepting a delivery, none of
    which anything in this system asked before.

    verify.confirm only ever re-asks whether the RULE still fires. It
    never asked whether the commercial survived the fix -- and a measured
    remediation chain silently lost 15 frames (0.649s) of running length,
    which for a broadcast spot is a broken deliverable however good the
    picture looks.
    """
    from PIL import Image
    from customs import media

    base = tmp_path / "base.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", "testsrc2=s=320x240:d=4:r=25", str(base)], check=True)

    # a real matte edit should pass every question
    ratio = tmp_path / "ratio.png"
    Image.new("RGB", (320, 240), (150, 128, 128)).save(ratio)
    edited = tmp_path / "edited.mp4"
    media.apply_relight(base, ratio, [300, 300, 700, 700], 1.0, 3.0, edited)

    report = media.craft_check(base, edited)
    assert report["failures"] == [], report["failures"]
    assert report["frames_before"] == report["frames_after"]
    assert report["resolution_before"] == report["resolution_after"]
    assert report["drift"] <= media.QC_MAX_DRIFT_S

    # a film that came back shorter is caught and named
    short = tmp_path / "short.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-i", str(base),
                    "-t", "3.4", "-c", "copy", str(short)], check=True)
    bad = media.craft_check(base, short)
    assert not bad["ok"]
    assert any("running length" in f for f in bad["failures"]), bad["failures"]


def test_craft_check_catches_a_resample(tmp_path):
    """A resolution change is a quality loss nobody asked for."""
    from customs import media

    base = tmp_path / "base.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", "testsrc2=s=320x240:d=2:r=25", str(base)], check=True)
    small = tmp_path / "small.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-i", str(base),
                    "-vf", "scale=160:120", str(small)], check=True)

    report = media.craft_check(base, small)
    assert not report["ok"]
    assert any("resolution changed" in f for f in report["failures"]), report["failures"]


def test_craft_check_notices_footage_that_was_disturbed(tmp_path):
    """The whole point of the matte is that untouched footage stays
    untouched. This is the measurement that would catch it not being so."""
    from customs import media

    base = tmp_path / "base.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", "testsrc2=s=320x240:d=2:r=25", str(base)], check=True)
    # a heavy full-frame change, the thing a matte exists to prevent
    mangled = tmp_path / "mangled.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-i", str(base),
                    "-vf", "hue=h=120,eq=brightness=0.2", str(mangled)], check=True)

    report = media.craft_check(base, mangled)
    assert not report["ok"]
    assert any("should not have touched" in f for f in report["failures"]), report["failures"]
    assert report["psnr"] is not None and report["psnr"] < media.QC_MIN_PSNR_DB




def test_a_film_does_not_rot_while_you_work_on_it(tmp_path):
    """Every remediation re-encodes the whole master, so the untouched
    footage is re-compressed once per fix.

    Measured at libx264's default: 42.85 dB after one edit, 40.68 after
    two, 39.45 after three, 33.44 after eleven. The commercial decays
    while it is being corrected, and nothing anywhere was watching.

    Five generations here, which is more fixes than a market usually
    needs, held against the craft gate's own floor.
    """
    from PIL import Image
    from customs import media

    original = tmp_path / "gen0.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                    "-i", "testsrc2=s=320x240:d=3:r=25", str(original)], check=True)
    ratio = tmp_path / "ratio.png"
    Image.new("RGB", (320, 240), (140, 128, 128)).save(ratio)

    current = original
    for gen in range(1, 6):
        nxt = tmp_path / f"gen{gen}.mp4"
        media.apply_relight(current, ratio, [400, 400, 600, 600], 0.5, 1.0, nxt)
        current = nxt

    report = media.craft_check(original, current, span=(0.5, 1.0))
    assert report["frames_before"] == report["frames_after"], \
        f"five edits changed the frame count: {report['failures']}"
    assert report["drift"] <= media.QC_MAX_DRIFT_S, \
        f"five edits moved the running length by {report['drift']:.3f}s"
    assert report["psnr"] is not None and report["psnr"] >= media.QC_MIN_PSNR_DB, (
        f"the film decayed to {report['psnr']:.2f} dB over five edits "
        f"(floor {media.QC_MIN_PSNR_DB})")


def test_pasting_a_box_leaves_the_rest_of_the_frame_bit_identical(tmp_path):
    """At PNG level there is no encode between the two images, so the
    matte's contract is exact rather than close: outside the feathered
    box, the result IS the original."""
    from PIL import Image, ImageChops
    from customs import media

    orig = tmp_path / "o.png"
    edit = tmp_path / "e.png"
    Image.new("RGB", (200, 160), (30, 90, 200)).save(orig)
    Image.new("RGB", (200, 160), (240, 40, 40)).save(edit)

    box = [300, 300, 700, 700]
    out = media.paste_box(orig, edit, box, tmp_path / "out.png", feather=0)

    a = Image.open(orig).convert("RGB")
    b = Image.open(out).convert("RGB")
    x, y, w, h = media.box_to_pixels(box, 200, 160)

    diff = ImageChops.difference(a, b)
    from PIL import ImageDraw
    ImageDraw.Draw(diff).rectangle([x, y, x + w, y + h], fill=(0, 0, 0))
    assert diff.getbbox() is None, "the paste reached outside its box"

    # and it really landed inside
    assert b.getpixel((x + w // 2, y + h // 2)) != a.getpixel((x + w // 2, y + h // 2))


@pytest.fixture(scope="session")
def audio_outruns_video(tmp_path_factory):
    """A film whose audio runs 47ms past its last video frame.

    Ordinary, and exactly the shape of the Chanel spot: 376 frames at 25fps
    is 15.040s of picture under 15.087s of sound, so format=duration reads
    the audio's length and probe_frames reads the picture's.
    """
    d = tmp_path_factory.mktemp("outrun")
    v, a, p = d / "v.mp4", d / "a.m4a", d / "base.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:r=25",
                    "-frames:v", "376", "-pix_fmt", "yuv420p", "-an", str(v)],
                   check=True, capture_output=True, timeout=120)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=440:r=44100",
                    "-t", "15.087", "-c:a", "aac", str(a)],
                   check=True, capture_output=True, timeout=120)
    subprocess.run(["ffmpeg", "-y", "-i", str(v), "-i", str(a), "-c", "copy",
                    "-map", "0:v", "-map", "1:a", str(p)],
                   check=True, capture_output=True, timeout=120)
    assert media.probe_frames(p) == 376
    assert abs(media.probe_duration(p) - 15.087) < 0.005
    return p


def test_an_edit_keeps_the_audio_that_runs_past_the_last_video_frame(
        audio_outruns_video, clip, png, tmp_path):
    """The bridge that failed the craft gate for 0.047s of nothing.

    -frames:v bounded the video and stopped the whole mux with it, so the
    stream-copied audio was cut 64ms short of the master's own. The picture
    was frame-exact, format=duration fell from the audio's length to the
    picture's, and craft_check refused a Veo generation that was correct.
    Every filtergraph encode in this module carried that bound, so every
    remedy failed the same way on the same film.
    """
    base = audio_outruns_video
    for label, out, run in (
        ("splice_clip", tmp_path / "sc.mp4",
         lambda o: media.splice_clip(base, clip, 0.0, 2.0, o)),
        ("crop_span", tmp_path / "cs.mp4",
         lambda o: media.crop_span(base, 0.0, 2.0, o)),
        ("overlay_image", tmp_path / "oi.mp4",
         lambda o: media.overlay_image(base, png, 0.0, 2.0, o)),
    ):
        run(out)
        craft = media.craft_check(base, out, span=(0.0, 2.0))
        assert craft["ok"], f"{label} failed the craft gate: {craft['failures']}"
        assert craft["frames_after"] == 376, label


def test_the_graph_frame_cap_ends_an_encode_fed_by_an_infinite_loop(
        audio_outruns_video, tmp_path):
    """replace_segment_video loops its PNG input forever, and -frames:v used
    to be what ended the encode. With no audio track [v] is the only output
    stream, so the graph's own cap has to end it -- or this hangs."""
    silent = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-i", str(audio_outruns_video), "-an",
                    "-c:v", "copy", str(silent)],
                   check=True, capture_output=True, timeout=120)
    frames = tmp_path / "frames"
    frames.mkdir()
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:s=320x240:d=1:r=25",
                    str(frames / "f_%03d.png")],
                   check=True, capture_output=True, timeout=120)
    started = time.monotonic()
    out = media.replace_segment_video(silent, 1.0, 2.0, frames, tmp_path / "rsv.mp4")
    assert time.monotonic() - started < 60, "the looped input was never bounded"
    assert media.probe_frames(out) == 376


def test_a_graph_that_does_not_end_in_v_is_refused_rather_than_mangled():
    with pytest.raises(media.MediaError):
        media._cap_frames("[0:v]scale=2:2[wrong]", 376)
