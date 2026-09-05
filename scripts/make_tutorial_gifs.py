"""Four tutorial GIFs, shot from the running console.

Not mockups. Each frame is a real page of the deployed app, fetched with a
judge's cookie, rendered by headless Chrome at a fixed viewport, and
captioned. What a reader sees in the README is what they get when they
open the same URL.

    python scripts/make_tutorial_gifs.py --base https://... --run run_xxx

Three things the pages need help with, all of them because a screenshot
cannot click:

  * <details> is closed until somebody opens it, so the fix picker and the
    scene rows are forced open in the fetched HTML
  * the market room ships in list view; the class that turns it into the
    detail view is set here
  * a cursor has to be drawn, because a screenshot has no pointer

ffmpeg does the assembly with palettegen, which is what keeps a
screenshot's greys from banding into mud.
"""
import argparse
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "media"
WIDTH, HEIGHT = 1280, 780
CHROME = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
          "/usr/bin/google-chrome", "/usr/bin/chromium")

CAPTION_H = 54
FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def chrome() -> str:
    for path in CHROME:
        if Path(path).exists():
            return path
    raise SystemExit("no Chrome found; set one in CHROME")


def fetch(base: str, path: str, cookie: str) -> str:
    req = urllib.request.Request(base.rstrip("/") + path,
                                 headers={"Cookie": cookie})
    return urllib.request.urlopen(req, timeout=90).read().decode()


def stage(html: str, base: str, *, open_details=False, detail_view=False,
          open_scenes=False, eager=False, hide=()) -> str:
    """Make one still show what a click would have shown."""
    html = html.replace("<head>", f'<head><base href="{base.rstrip("/")}/">', 1)
    if eager:
        # A lazy iframe below the fold never loads in a headless capture,
        # which is how the Grafana panel came out blank in the one GIF that
        # is about the Grafana panel.
        html = html.replace(' loading="lazy"', "")
    if open_details:
        html = re.sub(r"<details(?![^>]*\bopen\b)", "<details open", html)
    if open_scenes:
        html = html.replace('<tbody class="mkscene">', '<tbody class="mkscene" data-open>')
    if detail_view:
        html = html.replace('class="panel as-list"', 'class="panel"')
    for selector in hide:
        html = html.replace(selector, selector + ' style="display:none"')
    return html


def shot(html: str, dest: Path, tmp: Path, height: int = HEIGHT,
         budget: int = 6000) -> Path:
    """Render one page. `budget` is virtual milliseconds: a page that only
    has to lay itself out needs a couple of seconds, and one that boots a
    Grafana in an iframe needs twenty."""
    page = tmp / (dest.stem + ".html")
    page.write_text(html)
    subprocess.run([chrome(), "--headless", "--disable-gpu", "--hide-scrollbars",
                    f"--window-size={WIDTH},{height}",
                    f"--virtual-time-budget={budget}",
                    f"--screenshot={dest}", f"file://{page}"],
                   check=True, capture_output=True)
    return dest


def window(src: Path, top: int, dest: Path, height: int = HEIGHT) -> Path:
    """A HEIGHT-tall slice of a tall render: what a scroll would show.

    Every frame of one GIF has to be the same size or ffmpeg's concat
    stitches nonsense, so scrolling is a crop rather than a taller shot.
    """
    from PIL import Image

    im = Image.open(src).convert("RGB")
    top = max(0, min(top, max(0, im.height - height)))
    im.crop((0, top, im.width, top + height)).save(dest)
    return dest


def caption(src: Path, text: str, dest: Path) -> Path:
    """A title bar above the frame, in the console's own voice."""
    from PIL import Image, ImageDraw, ImageFont

    shot_im = Image.open(src).convert("RGB")
    out = Image.new("RGB", (shot_im.width, shot_im.height + CAPTION_H), "#ffffff")
    out.paste(shot_im, (0, CAPTION_H))
    draw = ImageDraw.Draw(out)
    draw.rectangle([0, 0, out.width, CAPTION_H - 1], fill="#111318")
    font = None
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            font = ImageFont.truetype(candidate, 21)
            break
    draw.text((26, CAPTION_H // 2), text, fill="#ffffff", font=font, anchor="lm")
    # the four marks, so a still from any of these is recognisably ours
    for i, colour in enumerate(("#4285F4", "#EA4335", "#FBBC05", "#34A853")):
        x = out.width - 30 - i * 14
        draw.rectangle([x, CAPTION_H // 2 - 5, x + 9, CAPTION_H // 2 + 4], fill=colour)
    out.save(dest)
    return dest


def cursor(src: Path, xy, dest: Path, *, ring: bool = True) -> Path:
    """Draw a pointer, because a screenshot has none."""
    from PIL import Image, ImageDraw

    im = Image.open(src).convert("RGB")
    draw = ImageDraw.Draw(im, "RGBA")
    x, y = xy
    if ring:
        for r, alpha in ((34, 60), (24, 90), (15, 130)):
            draw.ellipse([x - r, y - r, x + r, y + r], outline=(66, 133, 244, alpha), width=3)
    arrow = [(x, y), (x, y + 21), (x + 5, y + 16), (x + 9, y + 25),
             (x + 13, y + 23), (x + 9, y + 14), (x + 15, y + 14)]
    draw.polygon(arrow, fill=(17, 19, 24, 255), outline=(255, 255, 255, 255))
    im.save(dest)
    return dest


def assemble(frames: list[Path], dest: Path, delay: float = 1.9) -> Path:
    """Frames to GIF, via a palette so the greys do not band."""
    listing = dest.with_suffix(".txt")
    lines = []
    for path in frames:
        lines.append(f"file '{path}'")
        lines.append(f"duration {delay}")
    lines.append(f"file '{frames[-1]}'")          # ffmpeg needs the last twice
    lines.append("duration 2.6")
    listing.write_text("\n".join(lines))
    palette = dest.with_name(dest.stem + "_palette.png")
    common = ["-f", "concat", "-safe", "0", "-i", str(listing)]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *common,
                    "-vf", "scale=900:-1:flags=lanczos,palettegen=stats_mode=diff",
                    str(palette)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *common, "-i", str(palette),
                    "-lavfi", "scale=900:-1:flags=lanczos[s];[s][1:v]paletteuse=dither=bayer",
                    "-loop", "0", str(dest)], check=True)
    palette.unlink(missing_ok=True)
    listing.unlink(missing_ok=True)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://customs-app-akap4ao72a-ew.a.run.app")
    ap.add_argument("--run", default="run_804f7b687c72")
    ap.add_argument("--market", default="FR")
    ap.add_argument("--fixed", default="run_c61fa291681f",
                    help="a run that already has a localized master")
    ap.add_argument("--cookie", default="customs-role=judge")
    ap.add_argument("--only", help="one gif by name")
    args = ap.parse_args()

    tmp = ROOT / "scripts" / ".gifcache"
    tmp.mkdir(parents=True, exist_ok=True)
    run, base, cookie = args.run, args.base, args.cookie

    def page(path, **kw):
        return stage(fetch(base, path, cookie), base, **kw)

    def frame(name, html, text, scroll=0, point=None, budget=6000):
        """One captioned still, optionally scrolled down the page.

        The page is rendered tall enough to contain the scroll, then a
        window of the standard height is cut out of it, so every frame of
        a GIF is the same size whatever part of the page it shows.
        """
        raw = shot(html, tmp / f"{name}_raw.png", tmp, HEIGHT + scroll + 40,
                   budget=budget)
        if scroll:
            raw = window(raw, scroll, tmp / f"{name}_win.png")
        if point:
            raw = cursor(raw, point, tmp / f"{name}_cur.png")
        return caption(raw, text, tmp / f"{name}.png")

    gifs = {}

    gifs["tut-1-upload"] = lambda: [
        frame("t1a", page("/new"), "1 · Hand it a master, or a YouTube link"),
        frame("t1b", page("/new", open_details=True),
              "2 · Pick the markets: 98 jurisdictions, 21 packs", scroll=900),
        frame("t1c", page(f"/runs/{run}/mission"),
              "3 · The crew narrates itself, stage by stage", scroll=380),
        frame("t1d", page(f"/runs/{run}"),
              "4 · A verdict per market, with the evidence behind it"),
    ]

    gifs["tut-2-frames"] = lambda: [
        frame("t2a", page(f"/runs/{run}/frames"),
              "1 · Every shot, as the analyst saw it"),
        frame("t2b", page(f"/runs/{run}/frames"),
              "2 · One neutral sentence per frame, no verdicts allowed", scroll=620),
        frame("t2c", page(f"/runs/{run}/markets/{args.market}",
                          detail_view=True, open_scenes=True),
              "3 · A finding is observation x rule x citation", scroll=560),
    ]

    # The picker keeps the list view on purpose: in the detail view it
    # lands in a table cell the evidence column is squeezing, and five
    # priced methods render as five ribbons of one word per line.
    gifs["tut-3-fix"] = lambda: [
        frame("t3a", page(f"/runs/{run}/markets/{args.market}",
                          detail_view=True, open_scenes=True),
              "1 · What each market objected to, and why", scroll=420),
        frame("t3b", page(f"/runs/{run}/markets/{args.market}",
                          open_scenes=True, open_details=True),
              "2 · Five ways to fix it, priced before you press", scroll=560),
        # a run that HAS a localized master: the cutting room of one that
        # never ran a fix is a page saying nothing has been edited
        frame("t3c", page(f"/runs/{args.fixed}/cutting"),
              "3 · The localized master, beside the original", scroll=300),
    ]

    # eager + a long budget: this GIF is about the live Grafana panel, and
    # a lazy iframe that never loads is the one thing it cannot show.
    gifs["tut-4-grafana"] = lambda: [
        frame("t4a", page(f"/runs/{run}/timeline", eager=True),
              "1 · The grid: every category across the film's own clock",
              scroll=430, budget=25000),
        frame("t4b", page(f"/runs/{run}/timeline", eager=True),
              "2 · Click a square in Grafana's own panel", scroll=430,
              point=(430, 420), budget=25000),
        frame("t4c", page(f"/runs/{run}/markets/{args.market}",
                          detail_view=True, open_scenes=True),
              "3 · ...and a priced generative fix starts on that scene", scroll=560),
    ]

    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    for name, build in gifs.items():
        if args.only and args.only != name:
            continue
        print(f"{name}: shooting", flush=True)
        frames = build()
        dest = assemble(frames, OUT / f"{name}.gif")
        print(f"{name}: {dest.stat().st_size / 1e6:.1f} MB -> {dest}", flush=True)
        made.append(dest)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(made)} gif(s) in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
