import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_PTS_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
_SCENE_SCORE_RE = re.compile(r"scene_score=([0-9]+(?:\.[0-9]+)?)")
_FRAME_LINE_RE = re.compile(r"frame:\d+\s+pts:\S+\s+pts_time:([0-9]+(?:\.[0-9]+)?)")
_YAVG_LINE_RE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9]+(?:\.[0-9]+)?)")
_TIMEOUT = 60

class MediaError(Exception):
    """Raised when an ffmpeg/ffprobe subprocess fails or cannot be run."""

@dataclass
class Shot:
    shot_id: str
    t_start: float
    t_end: float

@dataclass
class FlashWindow:
    """One stretch of measured full-frame luminance flashing.

    flashes_per_second is the measured rate across the window, not a
    threshold verdict: detect_flashes reports every window it finds and the
    caller decides what rate is too fast (the pipeline's ingest stage uses
    the design spec's 3 flashes per second).
    """
    t_start: float
    t_end: float
    flashes_per_second: float

# --- flash detection thresholds (see detect_flashes) ---
# A "flash" is one rising edge in whole-frame average luminance (YAVG, on
# ffmpeg signalstats' 0..255 scale). 40 is about 16% of that range: far more
# than lighting drift or motion inside a shot, far less than the ~150 point
# swing a full-frame white strobe produces on real footage (measured on
# docs/samples/test_ad.mp4's planted strobe: +-133 to +-148).
FLASH_DELTA = 40.0
# Rising edges further apart than this start a new window. 0.5s keeps a
# sustained strobe in one window down to 2 flashes/second, comfortably below
# the 3/s threshold anything downstream cares about.
_FLASH_MAX_GAP = 0.5
# A window must carry at least this many edges over at least this long, so a
# single cut (one edge), a two-frame camera flash, or a three-frame
# alternation cannot be reported as sustained flashing.
_FLASH_MIN_EDGES = 3
_FLASH_MIN_SPAN = 0.5

def _encode_timeout(duration: float) -> int:
    """How long a full-file re-encode may take.

    A flat 60s was fine for the 56s test ad on a laptop and wrong for a 79s
    upload on a two-vCPU Cloud Run instance: the remediation died mid-encode
    and the finding went back to open (seen live, Heinz ad, FR-ALC-01).
    Encoding is roughly real-time per vCPU at this preset, so the budget is
    the asset's own duration with room to spare, floored at the old value.
    """
    return max(_TIMEOUT, int(duration * 6) + 30)


def _run(args: list[str], timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    """Run an ffmpeg/ffprobe command with an explicit arg list (never shell=True)."""
    try:
        proc = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"timed out after {timeout}s: {' '.join(args)}") from e
    except FileNotFoundError as e:
        raise MediaError(f"executable not found: {args[0]}") from e
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise MediaError(f"{args[0]} exited {proc.returncode}: {tail}")
    return proc

def probe_duration(path) -> float:
    proc = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], timeout=_TIMEOUT)
    out = proc.stdout.strip()
    try:
        return float(out)
    except ValueError as e:
        raise MediaError(f"could not parse duration from ffprobe: {out!r}") from e

def probe_resolution(path) -> tuple[int, int]:
    """(width, height) of the first video stream, in pixels."""
    proc = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x", str(path),
    ], timeout=_TIMEOUT)
    out = proc.stdout.strip().split("\n")[0]
    try:
        width, height = (int(v) for v in out.split("x")[:2])
    except ValueError as e:
        raise MediaError(f"could not parse resolution from ffprobe: {out!r}") from e
    return width, height

def _parse_yavg(text: str) -> list[tuple[float, float]]:
    """(pts_time, YAVG) per frame, from ffmpeg's metadata=print output.

    The filter prints two lines per frame -- "frame:N pts:P pts_time:T" then
    "lavfi.signalstats.YAVG=V" -- so the timestamp is carried forward onto
    the value line that follows it. A value line with no timestamp before it
    is skipped rather than guessed at.
    """
    samples = []
    t = None
    for line in text.splitlines():
        frame = _FRAME_LINE_RE.match(line)
        if frame:
            t = float(frame.group(1))
            continue
        value = _YAVG_LINE_RE.match(line.strip())
        if value and t is not None:
            samples.append((t, float(value.group(1))))
            t = None
    return samples

def detect_flashes(path) -> list[FlashWindow]:
    """Measure sustained full-frame luminance flashing, deterministically.

    This exists because a vision model cannot see it. The analyst reads two
    still keyframes per shot, and a strobe is a property of the *sequence*,
    not of any one frame -- the Task 9 gate missed docs/samples/test_ad.mp4's
    planted 6 flashes/second strobe for exactly that reason. The real
    Harding test is deterministic too, so this is measured rather than
    judged: ffmpeg's signalstats reports whole-frame average luminance
    (YAVG) per frame, and a flash is one rising edge of at least FLASH_DELTA
    between consecutive frames.

    Rising edges only, never falling ones: a strobe alternates bright/dark,
    so counting every transition would report double the real flash rate.
    Adjacent edges within _FLASH_MAX_GAP form one window; a window is
    returned only if it holds at least _FLASH_MIN_EDGES edges over at least
    _FLASH_MIN_SPAN seconds, and its rate is (edges - 1) / span, i.e. the
    mean interval between the flashes actually observed.

    Returns every window found, at any rate. Deciding which rate is unsafe
    belongs to the caller (the pipeline's ingest stage), not here.
    """
    proc = _run([
        "ffmpeg", "-v", "error", "-i", str(path), "-an",
        "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
        "-f", "null", "-",
    ], timeout=_TIMEOUT)
    samples = _parse_yavg(proc.stdout)

    edges = [
        samples[i][0]
        for i in range(1, len(samples))
        if samples[i][1] - samples[i - 1][1] >= FLASH_DELTA
    ]
    if not edges:
        return []

    groups: list[list[float]] = [[edges[0]]]
    for t in edges[1:]:
        if t - groups[-1][-1] <= _FLASH_MAX_GAP:
            groups[-1].append(t)
        else:
            groups.append([t])

    windows = []
    for group in groups:
        span = group[-1] - group[0]
        if len(group) < _FLASH_MIN_EDGES or span < _FLASH_MIN_SPAN:
            continue
        windows.append(FlashWindow(
            t_start=group[0], t_end=group[-1],
            flashes_per_second=(len(group) - 1) / span,
        ))
    return windows

def fit_image(png, video_path, out_path) -> Path:
    """Rescale a PNG to the exact pixel size of video_path's video stream.

    overlay_image composites a still at 0,0 at its own size, so an edited
    frame handed back by an image model at some other resolution would cover
    only part of the picture (or overflow it). Remediation fits the model's
    output to the master before compositing.
    """
    width, height = probe_resolution(video_path)
    _run([
        "ffmpeg", "-y", "-i", str(png), "-vf", f"scale={width}:{height}",
        "-frames:v", "1", str(out_path),
    ], timeout=_TIMEOUT)
    return Path(out_path)

def crop_span(path, t_start: float, t_end: float, out_path, factor: float = 0.8) -> Path:
    """Punch in on the centre of the frame for one span only (the reframe
    remediation method).

    A true "crop out the offending region" needs a bounding box the pipeline
    does not carry (observations are sentences, not boxes), so the spine's
    reframe is a centre crop to `factor` of the frame, rescaled back to the
    original resolution: it removes the outer 10% on each side for the span,
    which is what excludes an element sitting at the edge of the frame.
    Documented as the deliberate approximation it is.

    Implemented as split + crop + overlay rather than a timeline-gated crop
    filter so the output keeps one constant resolution end to end (a crop
    that switched size mid-stream would not encode), reusing the same
    enable='between(t,...)' composite the rest of this module uses.
    """
    duration = probe_duration(path)
    frame_count = probe_frames(path)
    width, height = probe_resolution(path)
    crop_w = max(2, int(width * factor) // 2 * 2)
    crop_h = max(2, int(height * factor) // 2 * 2)
    filt = (
        f"[0:v]split=2[base][pre];"
        f"[pre]crop={crop_w}:{crop_h}:(iw-{crop_w})/2:(ih-{crop_h})/2,"
        f"scale={width}:{height},setsar=1[zoom];"
        f"[base][zoom]overlay=enable='between(t,{t_start:.3f},{t_end:.3f})'[v]"
    )
    args = [
        "ffmpeg", "-y", "-i", str(path),
        "-filter_complex", _cap_frames(filt, frame_count),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(EDIT_CRF),
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        # Bounded by FRAME COUNT, not by a float duration: -t rounds, and a
        # file whose duration probes a hair short comes back missing the
        # frames at the end of it -- which is how a remediated commercial
        # silently lost 0.649s of running length across eleven edits. A
        # spot's running time is contractual. The bound lives in the graph
        # (_cap_frames) rather than in -frames:v, which took the copied
        # audio down with it.
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_encode_timeout(duration))
    return Path(out_path)

# A hard cut scores high in one frame. A dissolve or a fade spreads the
# same change across twenty frames, so no single pair of frames differs by
# much and a fixed threshold sees nothing at all.
#
# Measured on a real one -- a 1990s Marlboro commercial, 15.2s, 640x480:
# the HIGHEST scene score in the whole film is 0.1906, and the changes
# arrive in clusters of adjacent frames (2.33/2.37/2.43, 14.30/14.37/14.43)
# which is the signature of a dissolve rather than a cut. At 0.3 that ad
# is one shot. Every observation in it then spans the entire commercial,
# every finding inherits that span, and the fix picker offers to
# regenerate 15.2 seconds of video -- which Veo refuses, because its
# ceiling is 8. The button was greyed out for a film that was never
# actually one shot.
_SCENE_LADDER = (0.30, 0.15, 0.10, 0.07, 0.05, 0.035)

# Why 8: a take longer than this cannot be bridged by Veo in one piece, so
# a detector that leaves one has not finished its job. It is a reason to
# look harder, not a guarantee -- some films really are one long take.
_LONGEST_USEFUL_S = 8.0

# A dissolve trips several adjacent frames. They are one transition and
# deserve one boundary, at its strongest frame.
_CUT_CLUSTER_S = 0.5


def _scene_scores(path) -> list[tuple[float, float]]:
    """(timecode, scene score) for every frame ffmpeg will score.

    One pass, every score kept, threshold applied afterwards in Python.
    The old code let ffmpeg do the thresholding, which meant the only way
    to ask "and what if the cuts are softer than that" was to decode the
    film again.
    """
    args = [
        "ffmpeg", "-v", "quiet", "-i", str(path), "-an", "-vf",
        "select='gt(scene,0)',metadata=print:file=-", "-f", "null", "-",
    ]
    proc = _run(args, timeout=_TIMEOUT)
    scores, t = [], None
    for line in proc.stdout.splitlines():
        m = _PTS_TIME_RE.search(line)
        if m:
            t = float(m.group(1))
            continue
        m = _SCENE_SCORE_RE.search(line)
        if m and t is not None:
            scores.append((round(t, 3), float(m.group(1))))
            t = None
    return scores


def _cuts_at(scores, threshold: float, duration: float) -> list[float]:
    """Boundaries above `threshold`, one per transition rather than per frame."""
    hits = [(t, s) for t, s in scores
            if s > threshold and 0.05 < t < duration - 0.05]
    cuts = []
    for t, s in hits:
        if cuts and t - cuts[-1][0] <= _CUT_CLUSTER_S:
            if s > cuts[-1][1]:        # keep the strongest frame of the cluster
                cuts[-1] = (t, s)
            continue
        cuts.append((t, s))
    return [t for t, _ in cuts]


def detect_shots(path) -> list[Shot]:
    """Shot boundaries, with the threshold chosen to suit the material.

    Starts where a hard cut lives and walks down only as far as it has to.
    Sharp modern footage stops on the first rung and behaves exactly as
    before; archival footage that dissolves rather than cuts keeps going
    until the takes are short enough to be worth something.

    Over-segmenting is the cheap failure here: analyst.merge_micro_shots
    folds anything under half a second back into its neighbour, so a
    threshold that finds noise costs a merge rather than a wrong answer.
    """
    duration = probe_duration(path)
    frame_count = probe_frames(path)
    scores = _scene_scores(path)

    chosen: list[float] = []
    for threshold in _SCENE_LADDER:
        cuts = _cuts_at(scores, threshold, duration)
        chosen = cuts
        longest = max((b - a for a, b in zip([0.0] + cuts, cuts + [duration])),
                      default=duration)
        if longest <= _LONGEST_USEFUL_S:
            break

    boundaries = [0.0] + sorted(chosen) + [duration]
    return [
        Shot(shot_id=f"shot_{i}", t_start=boundaries[i], t_end=boundaries[i + 1])
        for i in range(len(boundaries) - 1)
    ]

# How densely a shot is sampled. Two frames per shot was the original
# fixed rate, and it is wrong for long takes: a 43 second cigarette
# commercial that shot detection saw as two shots was judged on four
# stills, none of which happened to contain a cigarette, so the tobacco
# rule never got a candidate observation to fire on (run_1e2bdef192bc,
# cleared for the EU with zero findings). Sampling by duration instead
# gives a 25 second take eight frames and leaves a two second cut at two.
_SECONDS_PER_FRAME = 3.0
_MIN_FRAMES, _MAX_FRAMES = 2, 8


def frames_for(span: float) -> int:
    """How many stills a shot of this length is worth."""
    return max(_MIN_FRAMES, min(_MAX_FRAMES, round(span / _SECONDS_PER_FRAME)))


def extract_keyframes(path, shot: Shot, out_dir, per_shot: int | None = None) -> list[Path]:
    """Evenly spaced stills across the shot.

    per_shot is an explicit override for callers that need exactly one
    frame (remediation edits a single frame); left unset, the count comes
    from the shot's own duration.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    span = shot.t_end - shot.t_start
    count = per_shot if per_shot is not None else frames_for(span)
    frames = []
    for i in range(count):
        frac = (i + 0.5) / count
        t = shot.t_start + frac * span
        out_path = out_dir / f"{shot.shot_id}_kf{i}.png"
        args = [
            "ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(path),
            "-frames:v", "1", str(out_path),
        ]
        _run(args, timeout=_TIMEOUT)
        frames.append(out_path)
    return frames

def poster(path, out_path, at: float = 1.0, width: int = 320) -> Path:
    """One small JPEG from the asset, for a list that has to show many at once.

    Deliberately not an evidence frame: those are full-resolution PNGs around
    1.3MB each, so a page showing a dozen runs would ship seventeen megabytes
    of stills to say what each advert looks like. This is a few kilobytes.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg", "-y", "-ss", f"{max(0.0, at):.3f}", "-i", str(path),
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", str(out_path),
    ]
    _run(args, timeout=_TIMEOUT)
    return out_path

def extract_audio(path, out_wav) -> Path:
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg", "-y", "-i", str(path), "-vn",
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(out_wav),
    ]
    _run(args, timeout=_TIMEOUT)
    return out_wav

def extract_audio_span(path, shot: Shot, out_dir) -> Path:
    """Extract one shot's audio span as a mono 16kHz WAV, named by shot_id.

    Directory-based signature (one call per shot, output path derived from
    shot_id under out_dir), mirroring extract_keyframes rather than
    extract_audio's single explicit-out-path signature, since task-9's
    pipeline iterates shots the same way for both. -ss before -i is an
    input-level seek (matches extract_keyframes's own seek placement); -t
    after -i bounds the output to the shot's own span from that seek point.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_wav = out_dir / f"{shot.shot_id}.wav"
    args = [
        "ffmpeg", "-y", "-ss", f"{shot.t_start:.3f}", "-i", str(path),
        "-t", f"{max(shot.t_end - shot.t_start, 0.0):.3f}",
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(out_wav),
    ]
    _run(args, timeout=_TIMEOUT)
    return out_wav

def replace_segment_video(path, t_start: float, t_end: float, new_frames_dir, out_path) -> Path:
    new_frames_dir = Path(new_frames_dir)
    frames = sorted(new_frames_dir.glob("*.png"))
    if not frames:
        raise MediaError(f"no PNG frames found in {new_frames_dir}")
    duration = probe_duration(path)
    frame_count = probe_frames(path)
    span = max(t_end - t_start, 1e-3)
    fps = max(len(frames) / span, 1.0)
    filt = f"[0:v][1:v]overlay=enable='between(t,{t_start:.3f},{t_end:.3f})'[v]"
    args = [
        "ffmpeg", "-y",
        "-i", str(path),
        # itsoffset shifts this input's own timestamps forward by t_start, so its
        # frame 0 (file order) lands exactly at global t=t_start instead of at
        # whatever phase the loop's free-running clock happens to be at by then.
        "-itsoffset", f"{t_start:.3f}",
        "-loop", "1", "-framerate", f"{fps:.3f}", "-pattern_type", "glob",
        "-i", str(new_frames_dir / "*.png"),
        "-filter_complex", _cap_frames(filt, frame_count),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(EDIT_CRF),
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        # This input loops forever, so something has to end the encode. The
        # graph's own frame cap does: trim=end_frame ends [v], and with no
        # audio track [v] is the only output stream, so the mux ends with
        # it. Verified on a silent base -- it returns in a fraction of a
        # second rather than spinning on the loop.
        # Bounded by FRAME COUNT, not by a float duration: -t rounds, and a
        # file whose duration probes a hair short comes back missing the
        # frames at the end of it -- which is how a remediated commercial
        # silently lost 0.649s of running length across eleven edits. A
        # spot's running time is contractual. The bound lives in the graph
        # (_cap_frames) rather than in -frames:v, which took the copied
        # audio down with it.
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_encode_timeout(duration))
    return Path(out_path)

def overlay_image(path, png, t_start: float, t_end: float, out_path) -> Path:
    duration = probe_duration(path)
    frame_count = probe_frames(path)
    filt = f"[0:v][1:v]overlay=enable='between(t,{t_start:.3f},{t_end:.3f})'[v]"
    args = [
        "ffmpeg", "-y",
        "-i", str(path),
        "-loop", "1", "-i", str(png),
        "-filter_complex", _cap_frames(filt, frame_count),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(EDIT_CRF),
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        # see replace_segment_video: the looped input is bounded by the
        # graph's frame cap, not by anything on the output side.
        # Bounded by FRAME COUNT, not by a float duration: -t rounds, and a
        # file whose duration probes a hair short comes back missing the
        # frames at the end of it -- which is how a remediated commercial
        # silently lost 0.649s of running length across eleven edits. A
        # spot's running time is contractual. The bound lives in the graph
        # (_cap_frames) rather than in -frames:v, which took the copied
        # audio down with it.
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_encode_timeout(duration))
    return Path(out_path)

# The feathered edge of a matte, in pixels. A hard rectangle reads as a
# sticker on anything with motion blur or a soft focus falloff; six pixels
# of ramp is enough to hide the seam and small enough that it never
# reaches material the finding did not point at.
MATTE_FEATHER_PX = 6

# How much room to leave around the analyst's box. A model asked to remove
# a bottle will also want the shadow and the highlight it threw, and a box
# drawn tight to the glass clips them off mid-edit.
MATTE_PAD = 0.06

# Every remediation re-encodes the whole master, so the untouched footage
# is re-compressed once per fix. At libx264's default (crf 23) that decays
# measurably: 42.85 dB after one edit, 40.68 after two, 39.45 after three,
# 33.44 after eleven. The film rots while you work on it.
#
# crf 16 is visually lossless and costs bitrate rather than quality, which
# is the right trade for a master that is going to be encoded again by
# whoever broadcasts it. Measured across generations, it buys about 3.3 dB
# throughout: 46.44 / 43.79 / 42.16 after one, two and three edits against
# 42.85 / 40.68 / 39.45 before.
#
# It does NOT stop the decay, only slows it -- by the eleventh edit the
# master is at 36.72 dB. Rendering once from an edit list is the actual
# fix and it is deliberately not done here: the operator has looked at
# this and parked it. The test below measures five generations so the
# number is on the record rather than a surprise later.
EDIT_CRF = 16



def box_to_pixels(box, width: int, height: int, pad: float = MATTE_PAD) -> tuple[int, int, int, int]:
    """[ymin, xmin, ymax, xmax] in 0-1000 -> (x, y, w, h) in pixels.

    Padded outward, clamped to the frame, and rounded to even numbers
    because libx264 will not accept an odd crop in yuv420p.
    """
    ymin, xmin, ymax, xmax = (max(0.0, min(1000.0, float(v))) for v in box)
    if ymax <= ymin or xmax <= xmin:
        raise MediaError(f"degenerate box: {box}")
    dy, dx = (ymax - ymin) * pad, (xmax - xmin) * pad
    ymin, xmin = max(0.0, ymin - dy), max(0.0, xmin - dx)
    ymax, xmax = min(1000.0, ymax + dy), min(1000.0, xmax + dx)

    x = int(xmin / 1000.0 * width) // 2 * 2
    y = int(ymin / 1000.0 * height) // 2 * 2
    w = max(2, int((xmax - xmin) / 1000.0 * width) // 2 * 2)
    h = max(2, int((ymax - ymin) / 1000.0 * height) // 2 * 2)
    w = min(w, width - x) // 2 * 2 or 2
    h = min(h, height - y) // 2 * 2 or 2
    return x, y, w, h


def composite_matte(base, patch, box, t_start: float, t_end: float, out_path,
                    *, feather: int = MATTE_FEATHER_PX, crf: int = EDIT_CRF) -> Path:
    """Put ONLY the box region of `patch` over `base`, for this span.

    This is the contract the whole remediation path is built on: every
    pixel outside the matte is the base's own footage, so "the rest of the
    scene is untouched" is arithmetic rather than a hope about how a model
    behaved. Measured on a real edit, the difference outside the matte is
    exactly zero.

    It exists because an image model asked to change a bottle does not
    change only the bottle. One real relettering edit moved 37.9% of the
    frame's pixels to alter something that occupied 1.3% of it, and two
    edits of the same source frame diverged everywhere, not just where they
    were asked to. Cropping the answer back to the question makes that
    divergence unreachable.

    `patch` is a still image or a video. A video patch is held to the
    span's length by the same enable window; a still is looped.

    The edge is feathered rather than cut, because a hard rectangle reads
    as a sticker against motion blur.
    """
    base, patch = Path(base), Path(patch)
    width, height = probe_resolution(base)
    duration = probe_duration(base)
    frame_count = probe_frames(base)
    x, y, w, h = box_to_pixels(box, width, height)

    still = patch.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    # A linear ramp inward from every edge of the crop: 0 at the boundary,
    # opaque `feather` pixels in. Cheap, and it only ever runs over the
    # crop, which is a fraction of the frame.
    ramp = max(1, int(feather))
    alpha = (f"clip(min(min(X,{w}-X),min(Y,{h}-Y))/{ramp}*255,0,255)")
    filt = (
        f"[1:v]scale={width}:{height},crop={w}:{h}:{x}:{y},"
        f"format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha}'[patch];"
        f"[0:v][patch]overlay={x}:{y}:enable='between(t,{t_start:.3f},{t_end:.3f})'[v]"
    )
    args = ["ffmpeg", "-y"]
    args += ["-i", str(base)]
    if still:
        args += ["-loop", "1", "-i", str(patch)]
    else:
        args += ["-i", str(patch)]
    args += [
        "-filter_complex", _cap_frames(filt, frame_count),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        # Bounded by FRAME COUNT, not by a float duration: -t rounds, and a
        # file whose duration probes a hair short comes back missing the
        # frames at the end of it -- which is how a remediated commercial
        # silently lost 0.649s of running length across eleven edits. A
        # spot's running time is contractual. The bound lives in the graph
        # (_cap_frames) rather than in -frames:v, which took the copied
        # audio down with it.
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_encode_timeout(duration))
    return Path(out_path)


def probe_frames(path) -> int:
    """How many video frames the file actually contains.

    Counted, not derived from duration x fps, because that product is
    exactly where a silently dropped frame hides.
    """
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-count_packets", "-show_entries", "stream=nb_read_packets",
        "-of", "csv=p=0", str(path),
    ]).stdout.strip().splitlines()
    try:
        return int(out[0])
    except (IndexError, ValueError) as exc:
        raise MediaError(f"could not count frames of {path}") from exc


# A remediated master must still be the same COMMERCIAL. Running length is
# contractual for a broadcast spot -- a 30 second slot is 30 seconds -- and
# an edit that quietly returns 29.35 has broken the deliverable however
# good the picture looks.
QC_MAX_DRIFT_S = 0.04        # under one frame at 25fps
QC_MIN_PSNR_DB = 38.0        # outside the edit, against the master it came from


def _psnr(a, b, *, exclude: tuple[float, float] | None = None) -> float | None:
    """PSNR of b against a in dB, optionally ignoring one time range.

    `exclude` is the span the edit was ALLOWED to change; everything else
    is what this is asking about. Without it a correct fix scores the same
    as a wrecked one, because the fix is a difference too.
    """
    lavfi = "psnr"
    if exclude:
        t0, t1 = exclude
        keep = (f"select='lt(t\\,{t0:.3f})+gt(t\\,{t1:.3f})',"
                f"setpts=N/FRAME_RATE/TB")
        lavfi = f"[0:v]{keep}[a];[1:v]{keep}[b];[a][b]psnr"
    proc = _run([
        "ffmpeg", "-nostdin", "-i", str(a), "-i", str(b),
        "-lavfi", lavfi, "-f", "null", "-",
    ], timeout=_encode_timeout(probe_duration(a)))
    m = re.search(r"average:([0-9.]+|inf)", proc.stderr)
    if not m:
        return None
    return float("inf") if m.group(1) == "inf" else float(m.group(1))


def _cap_frames(filt: str, frame_count: int) -> str:
    """Cap a filtergraph's [v] output at frame_count frames, inside the graph.

    `-frames:v frame_count` is the same arithmetic and is what every encode
    below used to carry. It stops the WHOLE mux the moment the video stream
    is full, and the audio being stream-copied alongside stops with it. On a
    film whose audio runs a little past its last video frame -- ordinary,
    and 47ms of it on the Chanel spot -- the output came back 64ms short of
    the master's own audio, so `format=duration` fell from the audio's
    length to the picture's and craft_check correctly refused a bridge whose
    picture was frame-exact: something HAD been lost, just not the picture.

    Bounding inside the graph bounds the picture only, which is the stream a
    frame count was ever about. The guarantee is unchanged -- trim cannot
    invent frames it was not handed, and neither could -frames:v -- and the
    0.649s that went missing across eleven edits stays fixed.
    """
    if not filt.endswith("[v]"):
        raise MediaError(
            f"cannot frame-bound a graph that does not end in [v]: {filt}")
    return f"{filt[:-3]},trim=end_frame={frame_count}[v]"


def craft_check(before, after, *, span: tuple[float, float] | None = None) -> dict:
    """Is the edited master still the same film as the one it came from?

    verify.confirm only ever re-asks whether the RULE still fires. Nothing
    asked whether the commercial survived the fix. These are the questions
    an editor asks before accepting a delivery, and they are arithmetic on
    two files that already exist:

      length      a broadcast spot's running time is contractual
      frames      a dropped frame is invisible in a duration comparison
      resolution  a resample is a quality loss nobody asked for
      psnr        how far the footage OUTSIDE the edit was disturbed

    Returns {"ok", "failures", ...measurements}. It measures; the caller
    decides.
    """
    before, after = Path(before), Path(after)
    d_before, d_after = probe_duration(before), probe_duration(after)
    f_before, f_after = probe_frames(before), probe_frames(after)
    r_before, r_after = probe_resolution(before), probe_resolution(after)

    failures = []
    drift = abs(d_after - d_before)
    if drift > QC_MAX_DRIFT_S:
        failures.append(
            f"running length moved by {drift:.3f}s "
            f"({d_before:.3f} -> {d_after:.3f}); a spot's length is contractual")
    if f_after != f_before:
        failures.append(f"frame count changed: {f_before} -> {f_after}")
    if r_after != r_before:
        failures.append(f"resolution changed: {r_before[0]}x{r_before[1]} "
                        f"-> {r_after[0]}x{r_after[1]}")

    psnr = None
    if r_after == r_before and f_after == f_before:
        psnr = _psnr(before, after, exclude=span)
        if psnr is not None and psnr < QC_MIN_PSNR_DB:
            where = "outside the edited span" if span else "across the film"
            failures.append(
                f"footage the edit should not have touched was disturbed: "
                f"{psnr:.2f} dB {where} (floor {QC_MIN_PSNR_DB:.0f})")

    return {
        "ok": not failures, "failures": failures,
        "duration_before": d_before, "duration_after": d_after, "drift": drift,
        "frames_before": f_before, "frames_after": f_after,
        "resolution_before": r_before, "resolution_after": r_after,
        "psnr": psnr,
    }


def splice_matte(base, clip, box, t_start: float, t_end: float, out_path,
                 *, feather: int = MATTE_FEATHER_PX, crf: int = EDIT_CRF) -> Path:
    """splice_clip, but only inside the box.

    The generated clip is retimed onto the span exactly as splice_clip
    does, and then only the finding's own region of it reaches the master.
    Everything else in the span -- the performance, the background, the
    light -- stays the brand's own footage.

    This is the answer to a generated span reinterpreting things nobody
    asked it to: a hammer that came back as a baseball bat was Veo
    inventing the whole frame, and a matte makes most of the frame
    unreachable.
    """
    base, clip = Path(base), Path(clip)
    width, height = probe_resolution(base)
    duration = probe_duration(base)
    frame_count = probe_frames(base)
    x, y, w, h = box_to_pixels(box, width, height)
    span = max(t_end - t_start, 1e-3)
    clip_len = max(probe_duration(clip), 1e-3)
    # same retime as splice_clip: Veo will not emit less than four seconds,
    # so the generated motion has to be played at the span's own rate
    factor = span / clip_len
    ramp = max(1, int(feather))
    alpha = f"clip(min(min(X,{w}-X),min(Y,{h}-Y))/{ramp}*255,0,255)"
    filt = (
        f"[1:v]setpts={factor:.6f}*PTS,scale={width}:{height},"
        f"crop={w}:{h}:{x}:{y},format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha}'[patch];"
        f"[0:v][patch]overlay={x}:{y}:enable='between(t,{t_start:.3f},{t_end:.3f})'"
        f":eof_action=pass[v]"
    )
    args = [
        "ffmpeg", "-y", "-i", str(base), "-itsoffset", f"{t_start:.3f}", "-i", str(clip),
        "-filter_complex", _cap_frames(filt, frame_count),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_encode_timeout(duration))
    return Path(out_path)


def extract_span_frames(path, t_start: float, t_end: float, out_dir,
                        fps: float | None = None) -> list[Path]:
    """Every frame of a span, as PNGs, in order.

    For the per-frame path, which edits the film's own frames rather than
    replacing them. `fps` under the source rate samples sparsely, which
    halves the bill and is only safe on a slow shot.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    span = max(t_end - t_start, 1e-3)
    args = ["ffmpeg", "-y", "-v", "error", "-ss", f"{t_start:.3f}",
            "-i", str(path), "-t", f"{span:.3f}"]
    if fps:
        args += ["-vf", f"fps={fps:g}"]
    args += ["-start_number", "0", str(out_dir / "f%05d.png")]
    _run(args, timeout=_encode_timeout(span))
    return sorted(out_dir.glob("f*.png"))


def paste_box(original_png, edited_png, box, out_png,
              *, feather: int = MATTE_FEATHER_PX) -> Path:
    """Composite only the box region of `edited` onto `original`.

    The same contract as composite_matte, done at PNG level, which is
    stricter: there is no encode between the two images, so every pixel
    outside the feathered box is bit-identical to the original rather
    than merely close.
    """
    from PIL import Image
    base = Image.open(original_png).convert("RGB")
    edit = Image.open(edited_png).convert("RGB").resize(base.size, Image.LANCZOS)
    x, y, w, h = box_to_pixels(box, *base.size)

    mask = Image.new("L", (w, h), 0)
    px = mask.load()
    ramp = max(1, int(feather))
    for j in range(h):
        for i in range(w):
            d = min(i, w - 1 - i, j, h - 1 - j)
            px[i, j] = 255 if d >= ramp else int(255 * d / ramp)

    out = base.copy()
    out.paste(edit.crop((x, y, x + w, y + h)), (x, y), mask)
    out.save(out_png)
    return Path(out_png)


def relight_ratio(original_png, edited_png, box, out_png, *, floor: int = 8) -> Path:
    """The per-pixel ratio edited/original inside the box, as a PNG.

    This is the compositor's clean-plate trick and it is the reason one
    image edit can carry a whole span. What the model changed is the
    object's SURFACE -- its colour, its label, its material. What it must
    not change is the light falling on it, which belongs to the shot and
    moves with it.

    Dividing the edited frame by the original divides the lighting out
    and leaves the albedo change on its own. Multiplying that ratio back
    into a LIVE frame re-applies it under that frame's own light, so the
    object changes while the grain, the exposure and the motion stay the
    frame's own.

    `floor` guards the division: a pixel that is nearly black carries no
    reliable colour and its ratio explodes, so it is clamped to 1 (leave
    that pixel alone) rather than allowed to blow out.
    """
    from PIL import Image
    orig = Image.open(original_png).convert("RGB")
    edit = Image.open(edited_png).convert("RGB").resize(orig.size, Image.LANCZOS)
    width, height = orig.size
    x, y, w, h = box_to_pixels(box, width, height)

    o = orig.crop((x, y, x + w, y + h)).load()
    e = edit.crop((x, y, x + w, y + h)).load()
    ratio = Image.new("RGB", (w, h))
    r = ratio.load()
    for j in range(h):
        for i in range(w):
            op, ep = o[i, j], e[i, j]
            out = []
            for c in range(3):
                base = op[c]
                if base < floor:
                    out.append(128)          # 1.0 in the encoding below
                else:
                    # encode ratio 0..2 into 0..255, so 1.0 lands on 128
                    out.append(max(0, min(255, int(round(ep[c] / base * 128)))))
            r[i, j] = tuple(out)
    ratio.save(out_png)
    return Path(out_png)


def apply_relight(base, ratio_png, box, t_start: float, t_end: float, out_path,
                  *, feather: int = MATTE_FEATHER_PX, crf: int = EDIT_CRF) -> Path:
    """Multiply a live span by a ratio field, inside the matte only.

    out(t) = frame(t) * ratio, for the pixels the finding pointed at, and
    frame(t) everywhere else. The span keeps its own motion, its own
    lighting and its own grain -- the thing that a generated span throws
    away and cannot get back.

    Costs one image edit for the whole span, against 1.88-3.68 EUR for a
    Veo regeneration of the same seconds.
    """
    base = Path(base)
    width, height = probe_resolution(base)
    duration = probe_duration(base)
    frame_count = probe_frames(base)
    x, y, w, h = box_to_pixels(box, width, height)
    ramp = max(1, int(feather))
    alpha = f"clip(min(min(X,{w}-X),min(Y,{h}-Y))/{ramp}*255,0,255)"
    # blend=multiply doubles at 255, so the ratio's 128 == 1.0 encoding is
    # undone by multiplying and scaling back up by two.
    filt = (
        f"[0:v]crop={w}:{h}:{x}:{y},format=gbrp[live];"
        f"[1:v]scale={w}:{h},format=gbrp[ratio];"
        f"[live][ratio]blend=all_mode=multiply:all_opacity=1,"
        f"lutrgb=r='clip(val*2,0,255)':g='clip(val*2,0,255)':b='clip(val*2,0,255)',"
        f"format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha}'[patch];"
        f"[0:v][patch]overlay={x}:{y}:enable='between(t,{t_start:.3f},{t_end:.3f})'[v]"
    )
    args = [
        "ffmpeg", "-y", "-i", str(base), "-loop", "1", "-i", str(ratio_png),
        "-filter_complex", _cap_frames(filt, frame_count),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        # Bounded by FRAME COUNT, not by a float duration: -t rounds, and a
        # file whose duration probes a hair short comes back missing the
        # frames at the end of it -- which is how a remediated commercial
        # silently lost 0.649s of running length across eleven edits. A
        # spot's running time is contractual. The bound lives in the graph
        # (_cap_frames) rather than in -frames:v, which took the copied
        # audio down with it.
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_encode_timeout(duration))
    return Path(out_path)


def replace_audio_span(path, wav, t_start: float, t_end: float, out_path) -> Path:
    span = max(t_end - t_start, 0.0)
    delay_ms = max(int(round(t_start * 1000)), 0)
    filt = (
        f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,"
        f"volume=enable='between(t,{t_start:.3f},{t_end:.3f})':volume=0[orig];"
        f"[1:a]atrim=0:{span:.3f},adelay=delays={delay_ms}:all=1,"
        f"aformat=sample_rates=48000:channel_layouts=stereo[repl];"
        f"[orig][repl]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    )
    args = [
        "ffmpeg", "-y",
        "-i", str(path), "-i", str(wav),
        "-filter_complex", filt,
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac",
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_TIMEOUT)
    return Path(out_path)


def splice_clip(path, clip, t_start: float, t_end: float, out_path) -> Path:
    """Replace [t_start, t_end) of `path` with ALL of `clip`, retimed to fit,
    keeping the original audio across the whole file.

    Used by the Veo bridge: the generated motion carries the picture for the
    span and nothing else, so the performance either side is the brand's own
    footage, untouched, and the soundtrack never breaks.

    Retimed, not trimmed. Veo will not emit less than four seconds, so a
    1.6 second span comes back as a four second clip. This used to keep the
    first 1.6 seconds of it and throw the rest away, which is wrong twice
    over: the motion played at 40% speed against untouched audio, and the
    last frame -- the corrected anchor Veo was explicitly conditioned to
    land on -- never reached the screen, so the patch jump-cut back to the
    original at the out point. Measured on this instance's own four
    bridges: 18%, 21%, 40% and 99% of the generated footage was shown, and
    the landing frame in none of them.

    Rescaling instead restores the real speed rather than creating fast
    motion: the two anchors are the true endpoints of a real span, so Veo
    rendered that displacement in slow motion to fill its minimum duration.
    The factor comes from probing the returned file, never from
    costs.bridge_seconds -- what Veo was asked for and what it emits are
    two different numbers.
    """
    duration = probe_duration(path)
    frame_count = probe_frames(path)
    span = max(t_end - t_start, 0.04)
    width, height = probe_resolution(path)
    try:
        generated = probe_duration(clip)
    except Exception:  # noqa: BLE001 -- an unprobeable clip still gets spliced
        generated = 0.0
    # >0.01: a clip already the right length needs no rescale, and a failed
    # probe (0.0) must not divide.
    retime = (f"setpts=PTS*{span / generated:.6f},"
              if generated > 0.01 and abs(generated - span) > 0.01 else "")
    # setpts does BOTH jobs here, and it has to: PTS-STARTPTS normalises the
    # trimmed patch to zero, and +t_start/TB then places it at the moment it
    # is meant to cover. The first version paired PTS-STARTPTS with an
    # input-level -itsoffset, which is the same instruction twice and cancels
    # out: the patch played from t=0, ended long before the overlay window
    # opened, and eof_action=pass then let the original through untouched.
    # The result was a master that cost a Veo generation and looked exactly
    # like the input, which is the worst kind of failure -- one that reports
    # success. tests/test_media.py measures the pixels now.
    filt = (
        f"[1:v]{retime}trim=0:{span:.3f},setpts=PTS-STARTPTS+{t_start:.3f}/TB,"
        f"scale={width}:{height},setsar=1[patch];"
        f"[0:v][patch]overlay=enable='between(t,{t_start:.3f},{t_end:.3f})':"
        f"eof_action=pass[v]"
    )
    args = [
        "ffmpeg", "-y",
        "-i", str(path),
        "-i", str(clip),
        "-filter_complex", _cap_frames(filt, frame_count),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(EDIT_CRF),
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        # Bounded by FRAME COUNT, not by a float duration: -t rounds, and a
        # file whose duration probes a hair short comes back missing the
        # frames at the end of it -- which is how a remediated commercial
        # silently lost 0.649s of running length across eleven edits. A
        # spot's running time is contractual. The bound lives in the graph
        # (_cap_frames) rather than in -frames:v, which took the copied
        # audio down with it.
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_encode_timeout(duration))
    return Path(out_path)
