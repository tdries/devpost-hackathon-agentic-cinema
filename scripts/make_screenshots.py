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

from make_tutorial_gifs import OUT, WIDTH, fetch, shot, stage, window  # noqa: E402

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
    # The showcase run: seven markets judged, three dimensions, eight fixes
    # landed and verified, and a film this project generated rather than
    # borrowed. app.SHOWCASE_RUN is the same id.
    ap.add_argument("--run", default="run_3ea5230a429f")
    ap.add_argument("--market", default="AE",
                    help="the market room and cutting room to shoot")
    ap.add_argument("--cookie", default="customs-role=judge")
    ap.add_argument("--only", help="one job name, or 'all' (the default)")
    args = ap.parse_args()

    tmp = ROOT / "scripts" / ".shotcache"
    tmp.mkdir(parents=True, exist_ok=True)
    run, base, market = args.run, args.base, args.market

    def page(path, **kw):
        return stage(fetch(base, path, args.cookie), base, **kw)

    # path, dest, final size, render height, virtual-time budget, stage kwargs
    jobs = {
        # the card a pasted link shows: the verdict, at Open Graph's shape.
        # 1280x672 is Open Graph's own 1.905 aspect at the render width, so
        # the resize down to 1200x630 is a scale and not a squash -- shooting
        # 700 tall and resizing to 630 compressed every line of type by a
        # tenth, which is visible in the headline.
        "og": (f"/runs/{run}", STATIC / "og.png", (OG_W, OG_H),
               round(OG_H * WIDTH / OG_W), 6000, {}),
        "mission": (f"/runs/{run}/mission", OUT / "05-mission-feed.png",
                    (SHOT_W, SHOT_H), 950, 6000, {}),
        "board": (f"/runs/{run}", OUT / "03-launch-board.png",
                  (SHOT_W, 880), 930, 6000, {}),
        "landing": ("/", OUT / "01-landing.png", (SHOT_W, 950), 1000, 6000, {}),
        # The rest of the README's tour, in the order it is read. Every one
        # of these used to be shot from whatever run was interesting at the
        # time, which is how the market room came to show studio cartoons
        # and the cutting room a Popeye short: they are all pinned to the
        # showcase run now, and it is ours.
        # Live, and with the page's own hero hidden: the archive's panel is
        # an iframe the viewer refuses to a file:// copy, and the thing this
        # screenshot is OF is the run cards, which sit below a hero and a
        # full-width panel and were entirely out of frame.
        "archive": ("/runs", OUT / "02-archive.png", (SHOT_W, 900), 1500,
                    120000, {"live": True, "crop_to": 560}),
        "grid": (f"/runs/{run}/timeline", OUT / "04-timeline-grid.png",
                 (SHOT_W, 900), 950, 120000, {"live": True}),
        "frames": (f"/runs/{run}/frames", OUT / "06-frame-board.png",
                   (SHOT_W, 950), 1000, 6000, {}),
        "market": (f"/runs/{run}/markets/{market}", OUT / "07-market-room.png",
                   (SHOT_W, 900), 950, 6000, {}),
        "market_open": (f"/runs/{run}/markets/{market}",
                        OUT / "07b-market-open.png", (SHOT_W, 900), 950, 6000,
                        {"open_details": True, "open_scenes": True}),
        "cutting": (f"/runs/{run}/cutting", OUT / "08-cutting-room.png",
                    (SHOT_W, 900), 950, 6000, {}),
        "agent": ("/agent", OUT / "09-agent-mode.png", (SHOT_W, 900), 950,
                  6000, {}),
        "library": ("/library", OUT / "10-library.png", (SHOT_W, 900), 950,
                    6000, {}),
        # The intelligence board is the one page a staged copy cannot show
        # (its panels are iframes the viewer refuses to a file:// origin)
        # and the one that needs real time: booting a Grafana and answering
        # a 30-day query takes far longer than the six seconds that suit a
        # page which only has to lay itself out. Both defaults gave an
        # empty hero, which is the panel the README points at.
        "insight": ("/insight", OUT / "09-intelligence.png", (SHOT_W, 900),
                    950, 120000, {"live": True}),
    }

    for name, (path, dest, size, height, budget, kw) in jobs.items():
        if args.only and args.only not in ("all", name):
            continue
        live = kw.pop("live", False)
        top = kw.pop("crop_to", 0)
        raw = shot("" if live else page(path, **kw), tmp / f"{name}.png",
                   tmp, height, budget,
                   url=base.rstrip("/") + path if live else "")
        if top:
            # A scroll, taken as a crop. Shooting the page short instead
            # would just cut the bottom off the thing being photographed.
            raw = window(raw, top, tmp / f"{name}-cropped.png",
                         height=height - top)
        resize(raw, size, dest)
        print(f"{name}: {dest.relative_to(ROOT)} {size[0]}x{size[1]} "
              f"({dest.stat().st_size / 1024:.0f} KB)", flush=True)

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
