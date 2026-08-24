"""Build docs/samples/test_ad.mp4, the landmine test commercial.

Eight Veo shots for a fictional drink brand "Solstice", each carrying one
deliberate clearance trigger. docs/samples/landmines.yaml documents what is
planted where and which market rules are expected to fire.

Two of the triggers are NOT left to the model, because a probabilistic
generator cannot guarantee them:

  shot 2  the note is generated blank, then the exact English line
          "Happiness is one sip away" is burned in, so OCR has a fixed target
  shot 5  a normal transition is generated, then a deterministic full-frame
          white strobe is composited over it at 6 flashes/second for 1.5 s

Shot 6's voice-over is rendered with the TTS model and mixed over the shot's
own audio, so the comparative claim is in the audio track verbatim.

Live model calls happen only with --generate. Every generated asset is cached
under scripts/.cache/, and a cached asset is never regenerated, so re-running
without --generate reassembles the ad for free.

Usage:
    python scripts/make_test_ad.py --generate         # generate what is missing, assemble
    python scripts/make_test_ad.py                    # reassemble from cache only
    python scripts/make_test_ad.py --generate --only 4 --force   # redo shot 4
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from google.genai import types

from customs.config import settings
from customs.genai_client import client
from customs.media import probe_duration

CACHE = ROOT / "scripts" / ".cache"
OUT = ROOT / "docs" / "samples" / "test_ad.mp4"

WIDTH, HEIGHT, FPS = 1280, 720, 24
SHOT_SECONDS = 7.0          # 8 shots x 7 s = 56 s, inside the 45-60 s target
VEO_SECONDS = 8             # what Veo generates; the tail is trimmed off

# Live-checked 2026-08-23 against project veoapi-469317: generate_videos and the
# TTS model both work on the "global" endpoint that customs.genai_client.client()
# pins, so this script needs no second client. What does NOT work is the model id
# .env used to carry: veo-3.0-generate-001 and veo-3.0-fast-generate-001 are in
# models.list() but 404 on predictLongRunning ("your project does not have access
# to it") in both global and us-central1, as does veo-2.0-generate-001. Only the
# veo-3.1-* family generates here, so VEO_MODEL is repinned to veo-3.1-generate-001.
POLL_SECONDS = 10
POLL_TIMEOUT = 900

NOTE_TEXT = "Happiness is one sip away"
VO_LINE = "Twice the energy of any other drink"

# Shot 5's strobe, in seconds from the start of the shot. floor(t*FPS) makes it
# frame-exact: 2 white frames on, 2 off, at 24 fps, so 6 flashes per second,
# twice the 3 flashes/second threshold the photosensitivity rules use.
STROBE_START, STROBE_END = 2.6, 4.1
STROBE_ENABLE = f"between(t,{STROBE_START},{STROBE_END})*lt(mod(floor(t*{FPS}),4),2)"

# Shot 6: where the voice-over starts, and how far the shot's own audio ducks.
VO_DELAY_MS = 700
VO_GAIN = 2.0
SHOT6_DUCK = 0.30

NEGATIVE_TEXT = "on-screen text, subtitles, captions, watermark, logo overlay, letterboxing"

SHOTS = {
    1: dict(
        landmine="wine glasses raised in a toast",
        prompt=(
            "Live-action television commercial, late golden hour on a European "
            "cafe terrace. Two friends in their thirties sit at a small round "
            "marble table and raise two large wine glasses filled with deep red "
            "wine into the centre of the frame, clinking them together in a "
            "clear toast. Both glasses are large and clearly visible, held high "
            "at chest height, the red wine catching the low sun. A tall dark "
            "green glass bottle with a plain cream label stands on the table "
            "beside them. Warm rim light, shallow depth of field, slow dolly in. "
            "Ambient terrace chatter and the clink of glass."
        ),
        negative=NEGATIVE_TEXT,
    ),
    2: dict(
        landmine="blank note (text burned in afterwards)",
        prompt=(
            "Extreme close-up macro shot, camera looking almost straight down at "
            "a small square of cream coloured notepaper lying flat on a dark "
            "walnut cafe table. The paper is completely blank: no writing, no "
            "letters, no print, no marks of any kind, just clean empty paper "
            "filling the middle of the frame. A fountain pen and a dark green "
            "glass bottle sit out of focus at the edge of frame. Soft window "
            "light from the left, shallow depth of field, very slow push in. "
            "Quiet cafe room tone."
        ),
        negative="text, writing, handwriting, letters, print, ink marks, "
                 "scribbles, watermark, captions, subtitles",
    ),
    3: dict(
        landmine="thumbs-up straight to camera",
        prompt=(
            "Live-action commercial, medium close-up. A smiling young man in a "
            "linen shirt stands on a sunlit city street, looks straight into the "
            "camera lens, and raises his right hand into the frame giving a big "
            "unmistakable thumbs-up: fist closed, thumb pointing straight up, "
            "hand held beside his face and completely in focus. He holds the "
            "thumbs-up and grins at the lens for the whole shot. Bright daylight, "
            "static camera, shallow depth of field. Street ambience."
        ),
        negative=NEGATIVE_TEXT,
    ),
    4: dict(
        landmine="beach scene in swimwear",
        prompt=(
            "Live-action commercial, wide tracking shot at midday on a tropical "
            "beach. Three young adults in colourful beach swimwear, two women in "
            "swimsuits and a man in swim shorts, walk together along the wet sand "
            "at the edge of the surf, laughing, one of them carrying a dark green "
            "glass bottle. Turquoise water, bright sun, palm trees behind them. "
            "Handheld camera tracking from the side, full bodies in frame. Surf "
            "and gull ambience."
        ),
        negative=NEGATIVE_TEXT,
    ),
    5: dict(
        landmine="transition (strobe composited afterwards)",
        prompt=(
            "Fast energetic transition shot for a drinks commercial. The camera "
            "whip-pans hard to the left across a sunlit beach, heavy horizontal "
            "motion blur streaking across the whole frame, and lands on the "
            "interior of a bright modern bar with warm neon accents and a dark "
            "green bottle on the counter. High energy, fast, no people in close "
            "up. Whoosh transition sound."
        ),
        negative=NEGATIVE_TEXT,
    ),
    6: dict(
        landmine="voice-over line (mixed in afterwards)",
        prompt=(
            "Live-action commercial, sunrise on a rooftop running track. A young "
            "athlete finishes a sprint, hands on knees, breathing hard, then "
            "straightens up and lifts a cold dark green glass bottle and drinks "
            "from it. Backlit by the low sun, haze in the air, the camera arcs "
            "slowly around him. Cinematic, shallow depth of field. Only ambient "
            "city and breathing sound, no music, no speech."
        ),
        negative=NEGATIVE_TEXT + ", dialogue, singing",
    ),
    7: dict(
        landmine="two men holding hands",
        prompt=(
            "Live-action commercial, warm evening at an outdoor cafe table under "
            "string lights. Two men in their thirties sit facing each other at a "
            "small round table, smiling warmly at each other. Their hands are "
            "clasped together on the tabletop in the exact centre of the frame, "
            "clearly holding hands, fingers interlocked, in sharp focus. Two dark "
            "green glass bottles stand on the table. Slow push in, shallow depth "
            "of field. Quiet evening terrace ambience."
        ),
        negative=NEGATIVE_TEXT,
    ),
    8: dict(
        landmine="product, clover emblem, price tag",
        # Regenerated twice. Take 1 drew a three leaf shamrock and cropped the neck
        # tag. Take 2 fixed the framing and the price but still drew a shamrock:
        # the words "four leaf clover" pull Veo hard toward its shamrock prior, so
        # take 3 describes the shape geometrically as a quatrefoil and never says
        # "clover". Dropping "text" from the negative list is what let the tag
        # print a readable price.
        prompt=(
            "Wide studio product commercial shot on a clean bright white "
            "background, camera pulled back so the whole bottle is in frame from "
            "cap to base with empty white space all around it. A single tall "
            "green glass bottle stands perfectly still in the exact centre, "
            "beaded with condensation, no rotation, only the light shifting. Its "
            "round cream label carries one large dark green quatrefoil emblem: "
            "four identical rounded heart shaped leaves arranged like a plus "
            "sign, one leaf at twelve o'clock, one at three o'clock, one at six "
            "o'clock and one at nine o'clock, all four meeting at a single centre "
            "point, with a thin stem below the bottom leaf. Four leaves in total. "
            "A small white rectangular cardboard price tag hangs from the bottle "
            "neck on a short string, facing the camera, printed with the price "
            "24.99. Soft studio key light, crisp macro detail. Quiet studio room tone."
        ),
        negative="shamrock, trefoil, three leaves, three leaf clover, three petals, "
                 "rotating bottle, subtitles, captions, watermark, people, hands",
    ),
}


class Blocked(RuntimeError):
    """A quota or allowlist refusal. Generation stops immediately, no retries."""


class NeedsGenerate(RuntimeError):
    """A live-API asset (Veo shot or TTS line) is missing from cache and
    --generate was not passed. Raised before any such call is attempted, so a
    flag-less run can never reach the network."""


def log(msg: str) -> None:
    print(f"[make_test_ad] {msg}", flush=True)


def run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {proc.returncode}:\n{(proc.stderr or '')[-2500:]}")
    return proc


# --------------------------------------------------------------------------- veo


def check_blocked(err: Exception) -> None:
    text = str(err)
    if re.search(r"RESOURCE_EXHAUSTED|429|quota|allowlist|allow-list|not allowed to use",
                 text, re.I):
        raise Blocked(text) from err


def generate_shot(n: int, allow_generate: bool) -> Path:
    """Generate shot n with Veo and cache it. No-op if the cache file exists.

    Never calls Veo unless allow_generate is True; raises NeedsGenerate
    instead, so a flag-less run cannot reach the network.
    """
    dest = CACHE / f"shot_{n}.mp4"
    if dest.exists():
        log(f"shot {n}: cached, skipping generation")
        return dest
    if not allow_generate:
        raise NeedsGenerate(
            f"shot {n}: {dest.name} is not in scripts/.cache/ and --generate "
            "was not passed; run with --generate to call Veo, or restore the "
            "cache file")
    # Spend lock (2026-08-24): Veo is the expensive model in this project by an
    # order of magnitude, and the test ad is already generated and cached. Even
    # --generate refuses unless VEO_UNLOCK=1 is set in the environment, so a
    # rebuilt cache or a stray --generate cannot silently start billing again.
    if os.environ.get("VEO_UNLOCK") != "1":
        raise NeedsGenerate(
            f"shot {n}: Veo generation is locked to prevent accidental spend. "
            "Set VEO_UNLOCK=1 in the environment (alongside --generate) to "
            "deliberately re-generate this shot.")
    spec = SHOTS[n]
    cfg = types.GenerateVideosConfig(
        duration_seconds=VEO_SECONDS,
        aspect_ratio="16:9",
        resolution="720p",
        number_of_videos=1,
        generate_audio=True,
        person_generation="allow_adult",
        negative_prompt=spec["negative"],
    )
    log(f"shot {n}: generating ({spec['landmine']})")
    try:
        # source=, not prompt=: the prompt kwarg is deprecated in google-genai 2.19
        # and warns on every call.
        op = client().models.generate_videos(
            model=settings.model_video,
            source=types.GenerateVideosSource(prompt=spec["prompt"]),
            config=cfg)
        waited = 0
        while not op.done:
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            if waited > POLL_TIMEOUT:
                raise RuntimeError(f"shot {n}: Veo operation still running after {waited}s")
            op = client().operations.get(op)
    except Blocked:
        raise
    except Exception as e:
        check_blocked(e)
        raise
    if getattr(op, "error", None):
        err = RuntimeError(f"shot {n}: Veo operation failed: {op.error}")
        check_blocked(err)
        raise err
    result = getattr(op, "response", None) or getattr(op, "result", None)
    videos = getattr(result, "generated_videos", None) or []
    if not videos:
        raise RuntimeError(f"shot {n}: Veo returned no video. Full response: {result}")
    video = videos[0].video
    data = getattr(video, "video_bytes", None)
    if not data:
        raise RuntimeError(
            f"shot {n}: Veo returned a reference instead of bytes (uri={getattr(video, 'uri', None)}); "
            "fetch it from GCS or set output_gcs_uri")
    dest.write_bytes(data)
    log(f"shot {n}: wrote {dest.name} ({dest.stat().st_size / 1e6:.1f} MB, "
        f"{probe_duration(dest):.2f}s)")
    return dest


# --------------------------------------------------------------------------- tts


def tts_wav(allow_generate: bool) -> Path:
    """Render the shot 6 voice-over line. Cached.

    Never calls the TTS model unless allow_generate is True; raises
    NeedsGenerate instead, so a flag-less run cannot reach the network.
    """
    dest = CACHE / "vo_shot6.wav"
    if dest.exists():
        log("vo: cached, skipping TTS")
        return dest
    if not allow_generate:
        raise NeedsGenerate(
            f"vo: {dest.name} is not in scripts/.cache/ and --generate was "
            "not passed; run with --generate to call the TTS model, or "
            "restore the cache file")
    log(f"vo: rendering {VO_LINE!r} with {settings.model_tts}")
    cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon"))),
    )
    prompt = f"Read this advertising line in a confident, upbeat announcer voice: {VO_LINE}"
    try:
        resp = client().models.generate_content(
            model=settings.model_tts, contents=prompt, config=cfg)
    except Exception as e:
        check_blocked(e)
        raise
    blob = None
    for cand in (resp.candidates or []):
        for part in (getattr(cand.content, "parts", None) or []):
            if getattr(part, "inline_data", None) and part.inline_data.data:
                blob = part.inline_data
                break
        if blob:
            break
    if blob is None:
        raise RuntimeError(f"tts: no audio in response: {resp}")
    rate_match = re.search(r"rate=(\d+)", blob.mime_type or "")
    rate = rate_match.group(1) if rate_match else "24000"
    raw = CACHE / "vo_shot6.pcm"
    raw.write_bytes(blob.data)
    run(["ffmpeg", "-y", "-f", "s16le", "-ar", rate, "-ac", "1", "-i", str(raw),
         "-ar", "48000", "-ac", "2", str(dest)])
    raw.unlink()
    log(f"vo: wrote {dest.name} ({probe_duration(dest):.2f}s)")
    return dest


# --------------------------------------------------------------------- burned text


def note_png() -> Path:
    """The shot 2 note text as a transparent PNG, ready to composite.

    drawtext is the obvious way to burn text, but it needs an ffmpeg built with
    libfreetype and Homebrew's default ffmpeg 8.0.1 bottle is not, so the filter
    simply does not exist there. Both branches below burn the identical string,
    so the OCR target is the same either way: drawtext when the local ffmpeg has
    it, otherwise rsvg-convert renders the string once and overlay composites it.
    """
    dest = CACHE / "note_text.png"
    if dest.exists():
        return dest
    box_w, box_h = 900, 220
    if _ffmpeg_has_drawtext():
        run(["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"color=c=black@0.0:s={box_w}x{box_h}:d=1,format=rgba",
             "-vf", ("drawtext=fontfile=/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf:"
                     f"text='{NOTE_TEXT}':fontcolor=0x1a1a2e:fontsize=64:"
                     "x=(w-text_w)/2:y=(h-text_h)/2"),
             "-frames:v", "1", str(dest)])
    else:
        svg = CACHE / "note_text.svg"
        svg.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{box_w}" height="{box_h}">'
            f'<text x="{box_w // 2}" y="{box_h // 2}" text-anchor="middle" '
            'dominant-baseline="middle" font-family="Bradley Hand, Snell Roundhand, '
            'Apple Chancery, cursive" font-size="66" font-weight="bold" fill="#1a1a2e" '
            f'>{NOTE_TEXT}</text></svg>'
        )
        run(["rsvg-convert", "-w", str(box_w), "-h", str(box_h),
             "-o", str(dest), str(svg)])
    log(f"note: rendered {NOTE_TEXT!r} to {dest.name}")
    return dest


def _ffmpeg_has_drawtext() -> bool:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                          capture_output=True, text=True)
    return bool(re.search(r"^\s*\S+\s+drawtext\s", proc.stdout, re.M))


# ----------------------------------------------------------------------- assembly


def has_audio(path: Path) -> bool:
    proc = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                "stream=index", "-of", "csv=p=0", str(path)], timeout=60)
    return bool(proc.stdout.strip())


BASE_VF = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
           f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}")


def build_shot(n: int, allow_generate: bool) -> Path:
    """Normalize shot n to 1280x720/24fps/yuv420p + stereo AAC, apply that
    shot's post-processing, and trim to SHOT_SECONDS."""
    src = CACHE / f"shot_{n}.mp4"
    dest = CACHE / f"norm_{n}.mp4"
    if not src.exists():
        raise FileNotFoundError(f"missing {src}; run with --generate")

    inputs = ["-i", str(src)]
    # Track the ffmpeg input index by hand. It is not len(inputs)/2, because a
    # lavfi input contributes four argv elements ("-f lavfi -i spec") not two.
    n_inputs = 1
    filters = []

    if n == 2:
        inputs += ["-loop", "1", "-i", str(note_png())]
        filters.append(f"[0:v]{BASE_VF}[base]")
        # Sits low and centre, where the blank paper is.
        filters.append(f"[base][{n_inputs}:v]overlay=(W-w)/2:H*0.52-h/2,format=yuv420p[v]")
        n_inputs += 1
    elif n == 5:
        inputs += ["-f", "lavfi", "-i", f"color=c=white:s={WIDTH}x{HEIGHT}:r={FPS}"]
        filters.append(f"[0:v]{BASE_VF}[base]")
        filters.append(f"[base][{n_inputs}:v]overlay=0:0:"
                       f"enable='{STROBE_ENABLE}',format=yuv420p[v]")
        n_inputs += 1
    else:
        filters.append(f"[0:v]{BASE_VF},format=yuv420p[v]")

    src_audio = has_audio(src)
    if src_audio:
        aidx = 0
    else:
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        aidx = n_inputs
        n_inputs += 1
    afmt = "aformat=sample_rates=48000:channel_layouts=stereo"

    if n == 6:
        inputs += ["-i", str(tts_wav(allow_generate))]
        vo_idx = n_inputs
        n_inputs += 1
        filters.append(f"[{aidx}:a]{afmt},volume={SHOT6_DUCK}[a0]")
        filters.append(f"[{vo_idx}:a]{afmt},adelay=delays={VO_DELAY_MS}:all=1,"
                       f"volume={VO_GAIN}[a1]")
        filters.append("[a0][a1]amix=inputs=2:duration=first:dropout_transition=0:"
                       "normalize=0[a]")
    else:
        filters.append(f"[{aidx}:a]{afmt}[a]")

    args = (["ffmpeg", "-y"] + inputs +
            ["-filter_complex", ";".join(filters),
             "-map", "[v]", "-map", "[a]",
             "-t", f"{SHOT_SECONDS}",
             "-c:v", "libx264", "-preset", "medium", "-crf", "21",
             "-pix_fmt", "yuv420p", "-r", str(FPS),
             "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
             "-movflags", "+faststart", str(dest)])
    run(args)
    log(f"shot {n}: built {dest.name} ({probe_duration(dest):.2f}s)")
    return dest


def assemble() -> Path:
    parts = [CACHE / f"norm_{n}.mp4" for n in sorted(SHOTS)]
    missing = [p.name for p in parts if not p.exists()]
    if missing:
        raise FileNotFoundError(f"cannot assemble, missing: {', '.join(missing)}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    inputs = []
    for p in parts:
        inputs += ["-i", str(p)]
    streams = "".join(f"[{i}:v][{i}:a]" for i in range(len(parts)))
    args = (["ffmpeg", "-y"] + inputs +
            ["-filter_complex", f"{streams}concat=n={len(parts)}:v=1:a=1[v][a]",
             "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "medium", "-crf", "21",
             "-pix_fmt", "yuv420p", "-r", str(FPS),
             "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
             "-movflags", "+faststart", str(OUT)])
    run(args)
    log(f"assembled {OUT} ({probe_duration(OUT):.2f}s, {OUT.stat().st_size / 1e6:.1f} MB)")
    return OUT


def keyframes(path: Path, times: list[float], out_dir: Path, tag: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for i, t in enumerate(times):
        out = out_dir / f"{tag}_{i}.png"
        run(["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(path),
             "-frames:v", "1", "-vf", f"scale={WIDTH}:-1", str(out)], timeout=120)
        made.append(out)
    return made


def parse_list(raw: str | None) -> list[int]:
    if not raw:
        return sorted(SHOTS)
    try:
        wanted = [int(x) for x in raw.replace(" ", "").split(",") if x]
    except ValueError as e:
        raise SystemExit(f"--only takes comma-separated shot numbers, got {raw!r}") from e
    unknown = [n for n in wanted if n not in SHOTS]
    if unknown:
        raise SystemExit(f"--only: no such shot {unknown}, valid shots are {sorted(SHOTS)}")
    return wanted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--generate", action="store_true",
                    help="allow live Veo/TTS calls for anything not in scripts/.cache/")
    ap.add_argument("--only", help="comma-separated shot numbers to work on; skips assembly")
    ap.add_argument("--force", action="store_true",
                    help="with --only, drop those cached shots first and regenerate")
    ap.add_argument("--keyframes", action="store_true",
                    help="also extract per-shot keyframes into scripts/.cache/frames/")
    args = ap.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    wanted = parse_list(args.only)

    if args.force:
        if not args.only:
            ap.error("--force needs --only, refusing to drop every cached shot")
        for n in wanted:
            for p in (CACHE / f"shot_{n}.mp4", CACHE / f"norm_{n}.mp4"):
                if p.exists():
                    p.unlink()
                    log(f"dropped {p.name}")

    try:
        for n in wanted:
            generate_shot(n, args.generate)
            build_shot(n, args.generate)
            if args.keyframes:
                for f in keyframes(CACHE / f"norm_{n}.mp4",
                                   [SHOT_SECONDS * 0.3, SHOT_SECONDS * 0.75],
                                   CACHE / "frames", f"shot{n}"):
                    log(f"keyframe {f}")
    except Blocked as e:
        log("BLOCKED by quota or allowlist, stopping without retries:")
        print(str(e), file=sys.stderr)
        return 2
    except NeedsGenerate as e:
        log("cache miss without --generate, stopping (no API call made):")
        print(str(e), file=sys.stderr)
        return 3

    if args.only:
        log("--only given, skipping assembly")
        return 0

    assemble()
    duration = probe_duration(OUT)
    if not 45.0 <= duration <= 60.0:
        log(f"WARNING: {duration:.2f}s is outside the 45-60 s target")
    if args.keyframes:
        marks = [SHOT_SECONDS * (i + 0.5) for i in range(len(SHOTS))]
        for f in keyframes(OUT, marks, CACHE / "frames", "final"):
            log(f"keyframe {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
