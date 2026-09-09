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


# JS that runs on the live page before the shutter, per job. A headless
# Chrome CLI cannot run any: that is how the grid shipped with a white
# body (the Grafana iframe never booted) and the cutting room with two
# black players (a <video> at t=0 of a film that opens on black).
GRID_READY = """async () => {
  const f = document.querySelector('iframe.mg-live');
  if (f.dataset.src !== undefined) { f.src = f.dataset.src; delete f.dataset.src; }
  await Promise.race([new Promise(r => f.addEventListener('load', r, {once: true})),
                      new Promise(r => setTimeout(r, 15000))]);
}"""
# Every lazy Grafana frame at once; browse() then waits for each to draw.
FRAMES_READY = """() => {
  for (const f of document.querySelectorAll('iframe[data-src]')) { f.src = f.dataset.src; delete f.dataset.src; }
}"""
# One real turn with the agent, typed into the page: the sentence, the
# reply, and the dashboard it built rendered on the right. %s is the ask.
AGENT_ASK = """async () => {
  document.getElementById('agent-input').value = %s;
  document.getElementById('agent-ask').requestSubmit();
  for (let i = 0; i < 420; i++) {
    const img = document.querySelector('#agent-canvas img.agent-shot');
    if (img && img.complete && img.naturalWidth > 0) return i;
    await new Promise(r => setTimeout(r, 1000));
  }
  throw new Error('no dashboard came back');
}"""


def browse(url: str, cookie: str, dest: Path, height: int, ready: str) -> Path:
    """The live page in a real browser, with `ready` awaited before the shot.

    Playwright rather than the Chrome CLI, because two of these pages need
    something a screenshot cannot do: wait for an iframe to boot, and
    seek a <video>.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": WIDTH, "height": height})
        name, _, value = cookie.partition("=")
        ctx.add_cookies([{"name": name, "value": value, "url": url}])
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        page.evaluate(ready)
        # The viewer is another origin, so page JS cannot see whether a
        # framed panel has drawn; Playwright can. "Loaded" is not "drawn":
        # the intelligence page shipped showing Grafana's loading mark.
        for frame in page.frames:
            if "/d-solo/" in frame.url or "/d/" in frame.url:
                # The panel's content box exists before its marks do, so
                # wait for a mark: a plot canvas, or a bar gauge's value.
                try:
                    frame.wait_for_selector(
                        'canvas, [data-testid="data-testid Bar gauge value"]',
                        timeout=90_000)
                except Exception:  # noqa: BLE001 -- a panel below the fold
                    print(f"  (a panel never drew: {frame.url[:80]})")
        page.wait_for_timeout(4000)
        page.screenshot(path=str(dest))
        browser.close()
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://customs-app-akap4ao72a-ew.a.run.app")
    # The showcase run: seven markets judged, three dimensions, eight fixes
    # landed and verified, and a film this project generated rather than
    # borrowed. app.SHOWCASE_RUN is the same id.
    ap.add_argument("--run", default="run_959b162a0f25")
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
                 (SHOT_W, 900), 1500, 0, {"ready": GRID_READY, "crop_to": 455}),
        # The board's Grafana half: the lanes panel, live, below the fold.
        "board_grafana": (f"/runs/{run}", OUT / "03b-board-grafana.png",
                          (SHOT_W, 760), 2100, 150000,
                          {"live": True, "crop_to": 1180}),
        "frames": (f"/runs/{run}/frames", OUT / "06-frame-board.png",
                   (SHOT_W, 950), 1000, 6000, {}),
        "market": (f"/runs/{run}/markets/{market}", OUT / "07-market-room.png",
                   (SHOT_W, 900), 950, 6000, {}),
        # The same room with the fix picker open, which is the point of the
        # second shot and sits below the fold: five methods, priced in euro,
        # before anything is spent. Shot tall and cropped to it, because a
        # short shot of an opened picker is a shot of the header above it.
        "market_open": (f"/runs/{run}/markets/{market}",
                        OUT / "07b-market-open.png", (SHOT_W, 900), 1900, 8000,
                        {"open_details": True, "open_scenes": True,
                         "crop_to": 820}),
        # No "cutting" job: 08-cutting-room.png is a hand-taken shot of the
        # AE pair, champagne flutes becoming mugs, chosen by eye. A reshoot
        # here would overwrite it with whatever the page opens on.
        "agent": ("/agent", OUT / "09-agent-mode.png", (SHOT_W, 900), 950,
                  6000, {}),
        # The agent asked for Grafana, twice: the canned findings-by-label
        # dashboard, and a chart it composes itself. Both cost a real turn.
        # No pie in the second: every pie but the by-dimension one renders
        # as a single series, which is a flaw in the spec, not the shot.
        "agent_dash": (f"/agent?run={run}", OUT / "09b-agent-dashboard.png",
                       (SHOT_W, 900), 950, 0,
                       {"ready": AGENT_ASK % '"Build a Grafana dashboard of the findings by dimension."',
                        "crop_to": 0}),
        "agent_chart": (f"/agent?run={run}", OUT / "09c-agent-chart.png",
                        (SHOT_W, 900), 950, 0,
                        {"ready": AGENT_ASK % '"Across every run, chart in Grafana: a bar chart with one bar per market, a bar chart with one bar per dimension, and a state timeline of blocking findings by market over the last 30 days. No pie charts."',
                         "crop_to": 0}),
        "library": ("/library", OUT / "10-library.png", (SHOT_W, 900), 950,
                    6000, {}),
        # My edits: the cross-run cutting room. Shot live, because each
        # pair is a <video> whose poster comes off this service and whose
        # span is cut on demand -- a file:// copy shows two grey boxes.
        "edits": ("/edits", OUT / "11-my-edits.png", (SHOT_W, 900), 1250,
                  90000, {"live": True, "crop_to": 240}),
        # No "generated" job either: 11b-generated.png is a hand-taken shot,
        # chosen by eye like the cutting room's.
        # The tour's front, which is what a stranger meets if they take the
        # third door. Slide one, because the deck is server-rendered and a
        # still of slide one is the deck's own cover.
        "tour": ("/tour", OUT / "12-tour.png", (SHOT_W, 900), 950, 6000, {}),
        # The intelligence board is the one page a staged copy cannot show
        # (its panels are iframes the viewer refuses to a file:// origin)
        # and the one that needs real time: booting a Grafana and answering
        # a 30-day query takes far longer than the six seconds that suit a
        # page which only has to lay itself out. Both defaults gave an
        # empty hero, which is the panel the README points at.
        "insight": ("/insight", OUT / "09-intelligence.png", (SHOT_W, 900),
                    950, 0, {"ready": FRAMES_READY, "crop_to": 0}),
    }

    for name, (path, dest, size, height, budget, kw) in jobs.items():
        if args.only and args.only not in ("all", name):
            continue
        live = kw.pop("live", False)
        top = kw.pop("crop_to", 0)
        ready = kw.pop("ready", "")
        if ready:
            raw = browse(base.rstrip("/") + path, args.cookie,
                         tmp / f"{name}.png", height, ready)
            # Cut to the README's own aspect instead of squashing to it.
            raw = window(raw, top, tmp / f"{name}-cropped.png",
                         height=round(WIDTH * size[1] / size[0]))
            top = 0
        else:
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
