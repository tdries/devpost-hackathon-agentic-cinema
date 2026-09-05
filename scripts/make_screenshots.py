"""Re-shoot the README screenshots and the social card from the live console.

The pictures date faster than anything else in the repo: the console went
from dark to light, grew screens, and the social card people see when they
paste the link was still a black launch board from August.

Shares its plumbing with make_tutorial_gifs.py, which already knows how to
fetch a page with a judge's cookie, point it at the live base href, and
force open the disclosures a screenshot cannot click.

    python scripts/make_screenshots.py --run run_xxx
    python scripts/make_screenshots.py --only og

The mission feed needs the same care it always did: its SSE socket never
closes, so a headless capture of the live URL hangs. Fetching the HTML and
rendering that copy is what makes it terminate.
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from make_tutorial_gifs import OUT, fetch, shot, stage  # noqa: E402

STATIC = ROOT / "src" / "customs" / "static"
# What the README uses everywhere else, so a new one drops in beside them.
SHOT_W, SHOT_H = 1400, 900
# Open Graph's own shape. The old card was 1200x833, which every card
# renderer cropped somewhere unhelpful.
OG_W, OG_H = 1200, 630


def resize(src: Path, size, dest: Path) -> Path:
    from PIL import Image

    im = Image.open(src).convert("RGB")
    if im.size != size:
        im = im.resize(size, Image.LANCZOS)
    im.save(dest, optimize=True)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://customs-app-akap4ao72a-ew.a.run.app")
    ap.add_argument("--run", default="run_804f7b687c72")
    ap.add_argument("--cookie", default="customs-role=judge")
    ap.add_argument("--only", help="og, mission, board, landing")
    args = ap.parse_args()

    tmp = ROOT / "scripts" / ".shotcache"
    tmp.mkdir(parents=True, exist_ok=True)
    run, base = args.run, args.base

    def page(path, **kw):
        return stage(fetch(base, path, args.cookie), base, **kw)

    jobs = {
        # the card a pasted link shows: the verdict, at Open Graph's shape
        "og": (f"/runs/{run}", STATIC / "og.png", (OG_W, OG_H), 700),
        "mission": (f"/runs/{run}/mission", OUT / "05-mission-feed.png",
                    (SHOT_W, SHOT_H), 950),
        "board": (f"/runs/{run}", OUT / "03-launch-board.png",
                  (SHOT_W, 880), 930),
        "landing": ("/", OUT / "01-landing.png", (SHOT_W, 950), 1000),
    }

    for name, (path, dest, size, height) in jobs.items():
        if args.only and args.only != name:
            continue
        raw = shot(page(path), tmp / f"{name}.png", tmp, height)
        resize(raw, size, dest)
        print(f"{name}: {dest.relative_to(ROOT)} {size[0]}x{size[1]} "
              f"({dest.stat().st_size / 1024:.0f} KB)", flush=True)

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
