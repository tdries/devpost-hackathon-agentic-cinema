"""Two more test commercials, built by injecting the missing triggers with Omni.

The reel this project already owns trips 8 of the taxonomy's 18 dimensions.
Three more need no footage at all -- their rules simply live in markets the
showcase run never selected (US for humour and profanity, CN or JP for
superstition), so a re-judge reaches them for the price of a Gemini call.
That leaves SEVEN dimensions that need something on screen that is not
there.

Veo would generate them from nothing at EUR 0.45 a second, four seconds
minimum: seven shots is EUR 12.60 before a single retry. Omni edits footage
that already exists at EUR 0.10 a second, so the same seven shots are EUR
2.80 -- and the footage stays this project's own, which is the whole reason
the archive was swept in the first place.

So: cut a four second span out of the existing reel, hand it to Omni with
one instruction, and put the result back. Each injection is written to meet
a REAL rule's trigger text (see INJECTIONS below and the rule ids beside
each one), because a trigger that does not satisfy any rule is a shot
nobody objects to: violence was observed thirty-one times in this reel and
never once fired, since DE, FR and UK all want it "gratuitous or realistic"
and a knife on a counter is neither.

Nothing here depicts a child, and nothing depicts an injury. The
children_and_minors trigger is a cartoon mascot and a "collect all six"
line, which is exactly what GL-CHILD-01 means by exploiting the credulity
of children, and needs no minor on screen to do it.

    OMNI_UNLOCK=1 python scripts/make_trigger_ads.py --generate
    OMNI_UNLOCK=1 python scripts/make_trigger_ads.py --generate --only gambling
    python scripts/make_trigger_ads.py --assemble

Same spend lock as make_showcase_ads.py, for the same reason, plus a hard
ceiling: BUDGET_EUR is what was authorised, the script refuses to start a
call that would cross it, and it prices only footage that actually came
back. A refusal costs nothing and says so.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from customs import genai_client                      # noqa: E402
from customs.config import settings                   # noqa: E402

SOURCE = ROOT / "docs" / "samples" / "test_ad.mp4"
OUT_DIR = ROOT / "docs" / "samples"
WORK = ROOT / "scripts" / ".triggercache"

EUR_PER_SECOND = 0.10          # costs._EURO_PER_OMNI_SECOND
SPAN_S = 4.0                   # one shot's worth, and Omni's cheapest useful unit
BUDGET_EUR = 3.60              # what was authorised. Nine spans' worth; seven are planned.

# span:      (start, seconds) in test_ad.mp4, chosen to sit inside one shot
# inject:    the instruction, written to satisfy the trigger text of `expects`
# expects:   rule ids that SHOULD fire, from packs that exist today
# reel:      which of the two commercials it lands in
INJECTIONS = {
    # Person-free spans first. Omni accepted the footage and then refused
    # to return an edit that put an online casino next to photorealistic
    # people ("reputational harms to photorealistic individuals"), so the
    # triggers that implicate a person go where there is no person: the
    # notepad shot (7-14s) and the product-on-white shot (49-56s).
    "children": {
        "span": (7.2, SPAN_S), "reel": "a",
        "expects": ["GL-CHILD-01", "UK-CHILD-01", "BE-VRT-KIDS-01"],
        "inject": (
            "Add a brightly coloured cartoon mascot, a smiling cartoon fox "
            "in a cape, standing on the desk beside the notepad and waving "
            "at camera, and add the words COLLECT ALL SIX in a childish "
            "crayon font across the bottom of the frame. Keep the desk, the "
            "notepad, the pen and the lighting exactly as they are."),
    },
    "pork": {
        "span": (10.0, SPAN_S), "reel": "a",
        "expects": ["AE-FOOD-01", "SA-FOOD-01"],
        "inject": (
            "Add a white plate of sliced cured ham and crisp bacon on the "
            "desk beside the notepad, as an ordinary snack. Keep the desk, "
            "the notepad and the lighting exactly as they are."),
    },
    "gambling": {
        "span": (49.4, SPAN_S), "reel": "b",
        "expects": ["AE-GAM-01", "SA-GAM-01", "CN-GAM-01", "DE-GAM-01"],
        "inject": (
            "Add a phone standing upright beside the bottle, its screen "
            "clearly showing an online casino: a spinning roulette wheel, "
            "betting odds, and a large DOUBLE YOUR MONEY button. Keep the "
            "bottle, the white background and the lighting unchanged."),
    },
    "flag": {
        "span": (52.0, SPAN_S), "reel": "b",
        "expects": ["CN-NAT-01", "FR-POL-01"],
        "inject": (
            "Replace the plain white background with the national flag of "
            "the People's Republic of China, red with five yellow stars, "
            "hung flat behind the bottle so the flag reads as the "
            "product's backdrop. Keep the bottle and its label exactly as "
            "they are."),
    },
    # These two have blurred people in the background, which may or may
    # not be enough for the output filter. A refusal costs nothing.
    "religion": {
        "span": (31.2, SPAN_S), "reel": "b",
        "expects": ["AE-REL-01", "SA-REL-01"],
        "inject": (
            "Add a crucifix pendant on a chain, hung over the neck of the "
            "bottle on the bar counter as a decorative sales tag. Keep the "
            "bar, the shelves and the lighting exactly as they are."),
    },
    "violence": {
        "span": (54.5, 3.0), "reel": "a",
        "expects": ["US-VIOL-01", "DE-WR-01", "UK-VIOL-01", "FR-VIOL-01"],
        "inject": (
            "Add a gloved hand entering frame holding a revolver, pressing "
            "its muzzle against the bottle and pulling the trigger, so the "
            "bottle shatters violently and glass flies. Nobody is present "
            "and nobody is hurt. Keep the white background."),
    },
    # The one that needs a person to be portrayed at all, and therefore the
    # one most likely to be refused: the rule is about reducing a person to
    # set dressing, which is a reputational harm by construction.
    "gender": {
        "span": (49.0, 3.0), "reel": "a",
        "expects": ["BE-GENDER-01", "FR-GEND-01", "US-GEND-01"],
        "inject": (
            "Add large on-screen advertising copy across the frame beside "
            "the bottle, in a bold serif font, reading exactly: FOR THE MAN "
            "WHO EARNED IT. ASK HER TO POUR. Keep the bottle and the white "
            "background exactly as they are."),
    },
    # One clean shot per reel, unedited and therefore free: without a
    # control, a reel that fires on everything cannot be told apart from a
    # judge that fires on anything.
    "control_a": {"span": (15.0, SPAN_S), "reel": "a", "inject": "",
                  "expects": []},
    "control_b": {"span": (44.0, SPAN_S), "reel": "b", "inject": "",
                  "expects": []},
}

REELS = {"a": "trigger_reel_a.mp4", "b": "trigger_reel_b.mp4"}


def _run(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True)


def cut(name: str, start: float, seconds: float) -> Path:
    """One span of the source reel, re-encoded so it starts on a keyframe."""
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / f"{name}_src.mp4"
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}",
          "-t", f"{seconds:.3f}", "-i", str(SOURCE),
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
          "-c:a", "aac", str(out)])
    return out


def vertex_client():
    """A genai client on the gcloud user token.

    Application-default credentials on this machine are expired and the
    Omni path needs a live token; `gcloud auth print-access-token` is the
    one that works. Installed on the module's own singleton so
    generate_omni_edit picks it up unchanged.
    """
    from google import genai
    from google.oauth2.credentials import Credentials

    token = subprocess.run(["gcloud", "auth", "print-access-token"],
                           capture_output=True, text=True,
                           check=True).stdout.strip()
    client = genai.Client(vertexai=True, project=settings.gcp_project,
                          location="global",
                          credentials=Credentials(token=token))
    genai_client._client = client                      # noqa: SLF001
    return client


def assemble(reel: str, pieces: list[Path]) -> Path:
    """The shots of one reel, in order, as one file."""
    listing = WORK / f"reel_{reel}.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in pieces))
    out = OUT_DIR / REELS[reel]
    _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
          "-i", str(listing), "-c:v", "libx264", "-preset", "veryfast",
          "-crf", "18", "-c:a", "aac", str(out)])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--generate", action="store_true",
                    help="actually call Omni (needs OMNI_UNLOCK=1)")
    ap.add_argument("--assemble", action="store_true",
                    help="stitch whatever spans are already in the cache")
    ap.add_argument("--only", default="", help="one injection by name")
    args = ap.parse_args()

    if args.generate and os.environ.get("OMNI_UNLOCK") != "1":
        print("refusing to spend: set OMNI_UNLOCK=1 as well as --generate")
        return 2

    planned = {k: v for k, v in INJECTIONS.items()
               if not args.only or k == args.only}
    spent = 0.0
    edited: dict[str, Path] = {}

    if args.generate:
        vertex_client()
        for name, plan in planned.items():
            start, seconds = plan["span"]
            src = cut(name, start, seconds)
            if not plan["inject"]:
                edited[name] = src
                print(f"  {name:10} control shot, unedited, EUR 0.00")
                continue
            price = seconds * EUR_PER_SECOND
            if spent + price > BUDGET_EUR:
                print(f"  {name:10} SKIPPED: EUR {spent:.2f} spent, "
                      f"EUR {price:.2f} more would cross the EUR "
                      f"{BUDGET_EUR:.2f} ceiling")
                continue
            out = WORK / f"{name}_omni.mp4"
            try:
                genai_client.generate_omni_edit(plan["inject"], src, out)
            except Exception as exc:  # noqa: BLE001 -- report and keep going
                print(f"  {name:10} REFUSED, nothing charged: "
                      f"{type(exc).__name__}: {str(exc)[:120]}")
                continue
            spent += price
            edited[name] = out
            print(f"  {name:10} rewrote {seconds:.0f}s, EUR {price:.2f} "
                  f"(running total EUR {spent:.2f}) -> {out.name}")

    if args.assemble or args.generate:
        for reel in REELS:
            pieces = [WORK / f"{n}_omni.mp4" if (WORK / f"{n}_omni.mp4").is_file()
                      else WORK / f"{n}_src.mp4"
                      for n, p in INJECTIONS.items() if p["reel"] == reel]
            pieces = [p for p in pieces if p.is_file()]
            if not pieces:
                continue
            out = assemble(reel, pieces)
            size = out.stat().st_size / 1024 / 1024
            print(f"  reel {reel}: {len(pieces)} shots -> "
                  f"{out.relative_to(ROOT)} ({size:.1f} MB)")

    print(f"\nspent: EUR {spent:.2f} of EUR {BUDGET_EUR:.2f} authorised")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
