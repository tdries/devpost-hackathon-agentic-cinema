"""Generate the three showcase commercials, and nothing else.

The archive's most vivid films were somebody else's footage, which is a bad
look on a rights-clearance tool. These three are ours: generated with Veo,
deliberately loaded so that a single four second spot trips several markets
at once, and vivid enough to be worth looking at on a front page.

Four seconds each, because Veo will not emit less and every second is
EUR 0.45. Three clips is EUR 5.40, which is the budget this was approved
against. 720p is what veo-3.1 produces here; there is no 8K to ask for.

Same spend lock as make_test_ad.py: --generate AND VEO_UNLOCK=1, both
deliberate, because this is the expensive model in the project by an order
of magnitude and a stray re-run is real money.

    VEO_UNLOCK=1 python scripts/make_showcase_ads.py --generate
    python scripts/make_showcase_ads.py --upload   # send them to the console
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from google.genai import types

from customs.config import settings
from customs.genai_client import client as adc_client


_CLIENT = None


def client():
    """The genai client, made once, ADC first and a gcloud token second.

    Application-default credentials on this laptop expire faster than the
    user login does, and a five euro generation should not wait on a
    browser round trip when `gcloud auth print-access-token` is sitting
    right there. Made ONCE and kept: a fresh client per call gets its
    httpx session closed underneath it by the garbage collector, which
    arrives as "Cannot send a request, as the client has been closed"
    in the middle of a poll loop. The container never takes this path; it
    runs as a service account and ADC always works there.
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    try:
        candidate = adc_client()
        candidate.models.list(config={"page_size": 1})  # the constructor is lazy
        _CLIENT = candidate
        return _CLIENT
    except Exception:  # noqa: BLE001 -- fall through to the token
        pass
    import subprocess

    from google import genai
    from google.oauth2.credentials import Credentials

    token = subprocess.run(["gcloud", "auth", "print-access-token"],
                           capture_output=True, text=True, check=True).stdout.strip()
    _CLIENT = genai.Client(vertexai=True, project=settings.gcp_project,
                           location="global", credentials=Credentials(token=token))
    return _CLIENT


OUT = ROOT / "docs" / "samples"
SECONDS = 4
POLL_SECONDS = 10
POLL_TIMEOUT = 600

# One negative for all three: the analyst reads what is in the frame, and a
# watermark or a burned-in caption would be a finding about the generator
# rather than about the ad.
NEGATIVE = ("on-screen text, subtitles, captions, watermark, logo overlay, "
            "letterboxing, blurry, low contrast")

# Each spot carries several triggers at once, on purpose: one four second
# clip that lights up alcohol, modesty and gesture in three different
# jurisdictions says more about this system than three clean ones do.
SPOTS = {
    "solstice_rooftop": {
        "landmines": "alcohol (FR Loi Evin), modesty (AE/SA), gesture",
        "prompt": (
            "Cinematic 4-second luxury drinks commercial, shot on 35mm, "
            "golden hour on a Barcelona rooftop bar. Two women in their "
            "twenties in short summer dresses clink tall glasses of "
            "sparkling rose wine, laughing, one throwing her head back. "
            "Neon-pink and turquoise bar lights, lens flare, shallow depth "
            "of field, saturated colour grade, slow dolly-in. No text."),
    },
    "ember_lounge": {
        "landmines": "tobacco (EU AVMSD art. 9), alcohol, night-life",
        "prompt": (
            "Cinematic 4-second premium commercial in a dim jazz lounge, "
            "deep teal and amber lighting. A man in a velvet suit lights a "
            "cigarette, exhales a slow curl of smoke over a crystal glass "
            "of amber whisky on a marble bar. Volumetric light through the "
            "smoke, extreme colour saturation, macro rack focus onto the "
            "glowing ember. No text."),
    },
    "voltage_runway": {
        "landmines": "modesty (AE/SA), photosensitivity, gender portrayal",
        "prompt": (
            "Cinematic 4-second high-fashion commercial, a model in a very "
            "short metallic mini dress strides down a mirrored runway "
            "toward camera while photographers' flashguns fire rapidly, "
            "strobing white light six times a second. Hyper-saturated "
            "magenta and electric blue, high contrast, wide angle, "
            "slow motion. No text."),
    },
}


class NeedsGenerate(RuntimeError):
    """Raised rather than silently spending."""


def log(message: str) -> None:
    print(message, flush=True)


def generate(name: str, spec: dict, allow: bool) -> Path:
    dest = OUT / f"{name}.mp4"
    if dest.exists():
        log(f"{name}: already generated, skipping")
        return dest
    if not allow:
        raise NeedsGenerate(f"{name}: pass --generate to call Veo")
    if os.environ.get("VEO_UNLOCK") != "1":
        raise NeedsGenerate(
            f"{name}: Veo is locked. Set VEO_UNLOCK=1 alongside --generate.")

    cfg = types.GenerateVideosConfig(
        duration_seconds=SECONDS, aspect_ratio="16:9", resolution="720p",
        number_of_videos=1, generate_audio=True,
        person_generation="allow_adult", negative_prompt=NEGATIVE)
    log(f"{name}: generating {SECONDS}s ({spec['landmines']})")
    op = client().models.generate_videos(
        model=settings.model_video,
        source=types.GenerateVideosSource(prompt=spec["prompt"]), config=cfg)
    waited = 0
    while not op.done:
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        if waited > POLL_TIMEOUT:
            raise RuntimeError(f"{name}: still running after {waited}s")
        op = client().operations.get(op)
    if getattr(op, "error", None):
        raise RuntimeError(f"{name}: Veo failed: {op.error}")
    result = getattr(op, "response", None) or getattr(op, "result", None)
    videos = getattr(result, "generated_videos", None) or []
    if not videos:
        raise RuntimeError(f"{name}: Veo returned no video: {result}")
    data = getattr(videos[0].video, "video_bytes", None)
    if not data:
        raise RuntimeError(f"{name}: Veo returned a reference, not bytes")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    log(f"{name}: {len(data) / 1e6:.1f} MB -> {dest}")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true",
                    help="call Veo for anything not already on disk")
    ap.add_argument("--only", help="one spot by name")
    args = ap.parse_args()

    wanted = {args.only: SPOTS[args.only]} if args.only else SPOTS
    made = []
    for name, spec in wanted.items():
        try:
            made.append(generate(name, spec, args.generate))
        except NeedsGenerate as exc:
            log(f"SKIP {exc}")
    log(f"\n{len(made)} spot(s) ready in {OUT}")
    for path in made:
        log(f"  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
