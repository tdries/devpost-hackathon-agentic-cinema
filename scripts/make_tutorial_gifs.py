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
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
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


def agent_turn(base: str, cookie: str, message: str, run: str = "") -> dict:
    """One real turn with the deployed agent, for the GIF to show.

    Not a mock-up of a conversation: the sentence is posted to /agent/ask
    on the live service, and the words, the tool calls and the view in the
    frames below are whatever came back. It costs one text turn, which is
    the price of not staging a screenshot of a product answering itself.
    """
    body = urllib.parse.urlencode({"message": message, "session": "gif",
                                   "run": run}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/agent/ask", data=body,
                                 headers={"Cookie": cookie})
    turn = json.loads(urllib.request.urlopen(req, timeout=420).read())
    if turn.get("error"):
        raise SystemExit(f"the agent refused the turn: {turn['error']}")
    return turn


def stage(html: str, base: str, *, open_details=False, detail_view=False,
          open_scenes=False, eager=False, panel_png_for="", hide=(),
          asked="", turn=None, view_png: Path | None = None,
          calls_only=False) -> str:
    """Make one still show what a click would have shown."""
    html = html.replace("<head>", f'<head><base href="{base.rstrip("/")}/">', 1)
    if panel_png_for:
        # The grid is a live iframe into the Grafana viewer, and the viewer
        # renders it empty for a headless capture: the panel's own query
        # window is the run's four seconds on the mapped clock and it comes
        # back white. The console has a second path for exactly this case,
        # the server-side render at /runs/{id}/lanes.png, which is the same
        # Loki state timeline drawn by Grafana's renderer. That is what the
        # GIF shows: still Grafana's panel, arriving as an image.
        html = re.sub(
            r'<iframe class="mg-live"[^>]*></iframe>',
            f'<img class="mg-live" src="/runs/{panel_png_for}/lanes.png" '
            f'alt="Occurrences per scene, rendered by Grafana" '
            f'style="height:auto;width:100%;display:block">',
            html)
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
    if asked or turn:
        # The same bubbles customs.js appends for a turn, so the still is
        # the page as a browser would have drawn it rather than a mock-up
        # of it: .ag-msg.ag-you for the sentence, .ag-msg.ag-agent for the
        # reply, and the server's own reply_html so rule ids and market
        # codes wear their chips.
        parts = []
        if asked:
            parts.append('<div class="ag-msg ag-you">'
                         '<svg class="ic"><use href="#i-human"/></svg>'
                         f'<div class="ag-body"><p>{asked}</p></div></div>')
        if turn and calls_only:
            steps = "".join(f'<li class="on">{c}</li>'
                            for c in (turn.get("calls") or [])[:5])
            parts.append('<div class="ag-msg ag-agent">'
                         '<svg class="ic"><use href="#i-pending"/></svg>'
                         '<div class="ag-body"><div class="ag-phases">'
                         '<div class="ag-bar"><i></i></div>'
                         f'<ol class="ag-steps">{steps}</ol>'
                         '</div></div></div>')
        elif turn:
            said = turn.get("reply_html") or (
                "".join(f"<p>{line}</p>" for line in
                        (turn.get("reply") or "").split("\n") if line.strip()))
            parts.append('<div class="ag-msg ag-agent">'
                         '<svg class="ic"><use href="#i-adjudicator"/></svg>'
                         f'<div class="ag-body">{said}</div></div>')
        head, anchor, tail = html.partition('<div class="agent-suggest')
        head = head.rstrip()
        assert head.endswith("</div>"), "the agent log did not close where expected"
        html = (head[:-len("</div>")] + "".join(parts) + "</div>"
                + anchor + tail)
    if view_png is not None:
        # What the agent opened, on the right, as its own render rather
        # than an iframe: this app serves its pages with frame-ancestors
        # naming itself, and a file:// still cannot frame them.
        label = (turn or {}).get("view_label") or "what it opened"
        html = html.replace(
            '<div class="agent-canvas" id="agent-canvas">',
            '<div class="agent-canvas" id="agent-canvas">'
            f'<img src="file://{view_png}" alt="{label}" '
            'style="width:100%;display:block">'
            '<style>#agent-canvas .empty{display:none}</style>', 1)
        html = re.sub(r'(<span class="label" id="view-label">).*?(</span>)',
                      r'\1<svg class="ic"><use href="#n-board"/></svg>'
                      + label + r'\2', html, count=1, flags=re.S)
    for selector in hide:
        html = html.replace(selector, selector + ' style="display:none"')
    return html


def shot(html: str, dest: Path, tmp: Path, height: int = HEIGHT,
         budget: int = 6000, url: str = "") -> Path:
    """Render one page. `budget` is virtual milliseconds: a page that only
    has to lay itself out needs a couple of seconds, and one that boots a
    Grafana in an iframe needs twenty.

    `url` shoots the live page instead of a staged copy, for the pages a
    copy cannot show: the Grafana viewer's CSP names the app as its only
    allowed frame-ancestor, so every panel on a file:// copy is refused.
    """
    if not url:
        page = tmp / (dest.stem + ".html")
        page.write_text(html)
        url = f"file://{page}"
    subprocess.run([chrome(), "--headless", "--disable-gpu", "--hide-scrollbars",
                    f"--window-size={WIDTH},{height}",
                    f"--virtual-time-budget={budget}",
                    f"--screenshot={dest}", url],
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
        frame("t4a", page(f"/runs/{run}/timeline", panel_png_for=run),
              "1 · The grid: every category across the film's own clock",
              scroll=430, budget=12000),
        frame("t4b", page(f"/runs/{run}/timeline", panel_png_for=run),
              "2 · Click a square in Grafana's own panel", scroll=430,
              point=(1000, 470), budget=12000),
        frame("t4c", page(f"/runs/{run}/markets/{args.market}",
                          detail_view=True, open_scenes=True),
              "3 · ...and a priced generative fix starts on that scene", scroll=560),
    ]

    # The agent, which is the half of the product the explainer screens
    # never showed. Three frames: the sentence, the tools it chose, and
    # the answer with what it opened beside it. The turn is real -- posted
    # to the live /agent/ask -- so nothing here is a product answering
    # itself in a mock-up.
    def agent_frames():
        asked = "What is blocked in France, and what would it cost to fix?"
        turn = agent_turn(base, cookie, asked, run)
        view = turn.get("view") or f"/runs/{run}"
        png = shot("", tmp / "t5_view.png", tmp, 900,
                   url=base.rstrip("/") + view)
        return [
            frame("t5a", page("/agent"),
                  "1 · Ask the console in plain words",
                  point=(300, 812)),
            frame("t5b", page("/agent", asked=asked, turn=turn,
                              calls_only=True),
                  "2 · It picks its own tools, and says which"),
            frame("t5c", page("/agent", asked=asked, turn=turn,
                              view_png=png),
                  "3 · The answer, with the page it read beside it"),
        ]

    gifs["tut-5-agent"] = agent_frames

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
