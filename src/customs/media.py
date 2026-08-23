import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_PTS_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
_TIMEOUT = 60

class MediaError(Exception):
    """Raised when an ffmpeg/ffprobe subprocess fails or cannot be run."""

@dataclass
class Shot:
    shot_id: str
    t_start: float
    t_end: float

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

def extract_keyframes(path, shot: Shot, out_dir, per_shot: int = 2) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    span = shot.t_end - shot.t_start
    frames = []
    for i in range(per_shot):
        frac = (i + 0.5) / per_shot
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
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy",
        # -shortest alone only bounds the encode via a *mapped* stream reaching a
        # real EOF (e.g. audio, when present); with no audio track the only output
        # stream is [v], fed by an infinite -loop input, so it never ends on its
        # own. -t is an unconditional cap regardless of which streams are mapped.
        "-t", f"{duration:.3f}", "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_TIMEOUT)
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
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy",
        # see replace_segment_video: -t is the unconditional bound, -shortest is
        # belt-and-suspenders for whenever a real audio stream is also mapped.
        "-t", f"{duration:.3f}", "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    _run(args, timeout=_TIMEOUT)
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
