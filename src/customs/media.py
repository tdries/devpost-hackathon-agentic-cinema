import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_PTS_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
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
        "-filter_complex", filt,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-t", f"{duration:.3f}", "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_encode_timeout(duration))
    return Path(out_path)

def detect_shots(path) -> list[Shot]:
    duration = probe_duration(path)
    args = [
        "ffmpeg", "-i", str(path), "-an", "-vf",
        "select='gt(scene,0.3)',showinfo", "-f", "null", "-",
    ]
    proc = _run(args, timeout=_TIMEOUT)
    cuts = set()
    for line in proc.stderr.splitlines():
        if "showinfo" not in line:
            continue
        m = _PTS_TIME_RE.search(line)
        if not m:
            continue
        t = round(float(m.group(1)), 3)
        if 0.05 < t < duration - 0.05:
            cuts.add(t)
    boundaries = [0.0] + sorted(cuts) + [duration]
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
        "-filter_complex", filt,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "copy",
        # -shortest alone only bounds the encode via a *mapped* stream reaching a
        # real EOF (e.g. audio, when present); with no audio track the only output
        # stream is [v], fed by an infinite -loop input, so it never ends on its
        # own. -t is an unconditional cap regardless of which streams are mapped.
        "-t", f"{duration:.3f}", "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_encode_timeout(duration))
    return Path(out_path)

def overlay_image(path, png, t_start: float, t_end: float, out_path) -> Path:
    duration = probe_duration(path)
    filt = f"[0:v][1:v]overlay=enable='between(t,{t_start:.3f},{t_end:.3f})'[v]"
    args = [
        "ffmpeg", "-y",
        "-i", str(path),
        "-loop", "1", "-i", str(png),
        "-filter_complex", filt,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "copy",
        # see replace_segment_video: -t is the unconditional bound, -shortest is
        # belt-and-suspenders for whenever a real audio stream is also mapped.
        "-t", f"{duration:.3f}", "-shortest", "-movflags", "+faststart",
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
    """Replace [t_start, t_end) of `path` with the first (t_end - t_start)
    seconds of `clip`, keeping the original audio across the whole file.

    Used by the Veo bridge: the generated motion carries the picture for the
    span and nothing else, so the performance either side is the brand's own
    footage, untouched, and the soundtrack never breaks. Veo will not emit a
    clip shorter than four seconds, so the clip is trimmed here rather than
    asked for at span length.
    """
    duration = probe_duration(path)
    span = max(t_end - t_start, 0.04)
    width, height = probe_resolution(path)
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
        f"[1:v]trim=0:{span:.3f},setpts=PTS-STARTPTS+{t_start:.3f}/TB,"
        f"scale={width}:{height},setsar=1[patch];"
        f"[0:v][patch]overlay=enable='between(t,{t_start:.3f},{t_end:.3f})':"
        f"eof_action=pass[v]"
    )
    args = [
        "ffmpeg", "-y",
        "-i", str(path),
        "-i", str(clip),
        "-filter_complex", filt,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-t", f"{duration:.3f}", "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_encode_timeout(duration))
    return Path(out_path)
