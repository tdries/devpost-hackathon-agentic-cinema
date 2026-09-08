"""Drive the live console through the demo script and record one video per beat.

    <playwright venv>/bin/python scripts/record_demo.py [outdir]

Reads docs/voiceover/NN.wav for each beat's length and paces the actions to fit.
Records the read-only screens against LIVE; the launcher (which sits behind the
spending door) is recorded against a local instance on LOCAL.

ponytail: nothing here clicks a control that spends -- no clearance is started,
no square is clicked, no agent question is sent. Those beats are staged (form
filled, square hovered) and the finished showcase run supplies the results.
"""
import os, subprocess, sys, time
from playwright.sync_api import sync_playwright

_T0 = 0.0
FLOW_HEAD = 4.0   # what cut_demo drops off the front of the flow beat
ONLY = {int(x) for x in os.environ.get("ONLY_BEATS", "").split(",") if x.strip()}
LIVE = os.environ.get("CUSTOMS_URL", "https://customs-app-akap4ao72a-ew.a.run.app")
LOCAL = os.environ.get("CUSTOMS_LOCAL", "http://127.0.0.1:8000")
RUN = os.environ.get("SHOWCASE_RUN", "run_959b162a0f25")
OUT = sys.argv[1] if len(sys.argv) > 1 else "docs/video/beats"
W, H = 1920, 1080


def secs(n):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", f"docs/voiceover/{n:02d}.wav"],
                       capture_output=True, text=True)
    return float(p.stdout.strip())


def glide(page, x, y, steps=25):
    page.mouse.move(x, y, steps=steps)


def creep(page, px, over):
    """Scroll px pixels over `over` seconds, in small steps, like a hand does."""
    steps = max(1, int(over / 0.05))
    per = px / steps
    for _ in range(steps):
        page.mouse.wheel(0, per)
        page.wait_for_timeout(50)


def beat(pw, n, url, actions, base=None, head=0.0):
    if ONLY and n not in ONLY:
        return
    d = secs(n)
    ctx = pw.chromium.launch(args=["--force-device-scale-factor=1"]).new_context(
        viewport={"width": W, "height": H}, device_scale_factor=1,
        record_video_dir=f"{OUT}/{n:02d}", record_video_size={"width": W, "height": H})
    # ponytail: the role cookie is what the door sets; only ever on LOCAL, so the
    # live gate is never walked around.
    if base == LOCAL:
        ctx.add_cookies([{"name": "customs-role", "value": "judge",
                          "url": LOCAL}])
    page = ctx.new_page()
    global _T0
    t0 = _T0 = time.time()
    page.goto((base or LIVE) + url, wait_until="load", timeout=60000)
    page.wait_for_timeout(900)
    actions(page)
    left = d + 0.8 + head - (time.time() - t0)
    if left > 0:
        page.wait_for_timeout(int(left * 1000))
    ctx.close()
    print(f"{n:02d}  target {d:5.1f}s  took {time.time() - t0:5.1f}s  {url}")


def main(only=None):
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as pw:

        # 2 homepage, and through the judge door
        def b1(p):
            p.wait_for_timeout(2500)
            door = p.locator("a.door-judge")
            door.scroll_into_view_if_needed(); p.wait_for_timeout(700)
            box = door.bounding_box()
            glide(p, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            p.wait_for_timeout(1600)
            creep(p, 900, 3.0)
        beat(pw, 2, "/", b1)

        # 3 the flow diagram, driven by the narration's own part offsets
        def b3flow(p):
            import json
            offs = json.load(open("docs/voiceover/marks.json"))["3"]
            fig = p.locator("#flow")
            fig.first.evaluate("el => el.scrollIntoView({block: 'center'})")
            # It plays itself once on approach. Left alone that race puts the
            # diagram on step 8 while the narration is still on step 2, so the
            # autoplay is stopped (its button toggles) before anything is driven.
            p.wait_for_timeout(1500)
            p.evaluate("""() => {
              const b = document.getElementById('flow-play');
              if (b && b.textContent.trim() === 'stop') b.click();
            }""")
            # line the first stage up with the first word
            wait0 = FLOW_HEAD - (time.time() - _T0)
            if wait0 > 0:
                p.wait_for_timeout(int(wait0 * 1000))
            t0 = time.time()
            for i, off in enumerate(offs, 1):
                wait = off - (time.time() - t0)
                if wait > 0:
                    p.wait_for_timeout(int(wait * 1000))
                p.evaluate("""(n) => {
                  const svg = document.querySelector('#flow .flowsvg');
                  const fig = document.getElementById('flow');
                  const now = document.getElementById('flow-now');
                  if (svg) svg.setAttribute('data-step', n);
                  if (fig) fig.setAttribute('data-step', n);
                  document.querySelectorAll('#flow .flow-steps li')
                    .forEach((li, i) => li.classList.toggle('on', i === n - 1));
                  if (now) now.textContent = 'step ' + n + ' of 8';
                }""", i)
        beat(pw, 3, "/", b3flow, head=FLOW_HEAD)

        # 4 the archive: hover the cards so their timelapses play
        def b3(p):
            # Only the films this project generated go on camera. The live archive
            # also holds borrowed development footage (config.WITHHELD_ASSETS is
            # empty on the deployed revision), and none of that belongs in a
            # submission video.
            p.evaluate("""() => {
              const own = ['test_ad', 'trigger_reel_a', 'trigger_reel_b',
                           'ember_lounge', 'voltage_runway', 'solstice_rooftop'];
              document.querySelectorAll('.runcard').forEach(c => {
                if (!own.some(n => c.textContent.includes(n))) c.remove();
              });
              // the weekly panel's legend prints every asset name, borrowed ones
              // included, so it stays out of frame; the per-card lanes stay live
              document.querySelectorAll('iframe.archlive').forEach(f => {
                const s = f.closest('section'); (s || f).remove();
              });
            }""")
            p.wait_for_timeout(1600)
            cards = p.locator(".runcard")
            for i in range(min(cards.count(), 3)):
                c = cards.nth(i)
                c.scroll_into_view_if_needed()
                bb = c.bounding_box()
                if not bb:
                    continue
                glide(p, bb["x"] + bb["width"] * 0.26, bb["y"] + bb["height"] * 0.32)
                p.wait_for_timeout(2500)      # the thumbnail loads on approach
                glide(p, bb["x"] + bb["width"] * 0.72, bb["y"] + bb["height"] * 0.68, steps=18)
                p.wait_for_timeout(1600)      # rest on the lanes chart
            creep(p, 600, 2.0)
        beat(pw, 4, "/runs", b3)

        # 4 the launcher, filled in but never submitted (local instance)
        def b2(p):
            p.set_input_files("#asset", "docs/samples/test_ad.mp4")
            p.wait_for_timeout(1400)
            # the country picks live inside collapsed family folds
            p.evaluate("document.querySelectorAll('details.level, details.famfold').forEach(d=>d.open=true)")
            p.wait_for_timeout(600)
            creep(p, 500, 1.2)
            for m in ["FR", "SA", "US", "JP", "CN", "DE"]:
                # the checkbox is visually hidden; the label is what a person clicks
                lab = p.locator(f'label.market-pick:has(input[value="{m}"])')
                if lab.count():
                    lab.first.scroll_into_view_if_needed()
                    bb = lab.first.bounding_box()
                    if bb: glide(p, bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2, steps=8)
                    lab.first.click(); p.wait_for_timeout(420)
            # back up so both columns stay in frame, then rest on the button
            creep(p, -260, 1.0)
            go = p.locator("button.go")
            bb = go.first.bounding_box() if go.count() else None
            if bb and 0 < bb["y"] < H - 60:
                glide(p, bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            p.wait_for_timeout(1800)
        beat(pw, 5, "/new", b2, base=LOCAL)

        # 4 mission feed
        beat(pw, 6, f"/runs/{RUN}/mission", lambda p: (p.wait_for_timeout(2000), creep(p, 2600, 9.5)))

        # 5 launch board: tiles, then the Grafana panels under them
        def b4(p):
            p.wait_for_timeout(2200)
            t = p.locator("#tile-FR")
            if t.count():
                t.scroll_into_view_if_needed(); p.wait_for_timeout(400)
                bb = t.bounding_box()
                if bb: glide(p, bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            p.wait_for_timeout(2200)
            creep(p, 3200, 11.0)
        beat(pw, 7, f"/runs/{RUN}", b4)

        # 7 timeline grid: hover the squares, never click (a click spends)
        def b6(p):
            p.wait_for_timeout(2000)
            creep(p, 500, 1.2)
            for dx in (0, 180, 360):
                glide(p, 760 + dx, 620); p.wait_for_timeout(1100)
            creep(p, 500, 1.5)
        beat(pw, 8, f"/runs/{RUN}/timeline", b6)

        # 8 market room FR: open a scene, then the takeaway chips
        def b7(p):
            p.wait_for_timeout(1500)
            rows = p.locator("tr.scene-row")
            if rows.count():
                rows.first.scroll_into_view_if_needed()
                rows.first.click(); p.wait_for_timeout(2600)
            creep(p, 1300, 4.5)
            chip = p.locator('a.chip[href*="certificate.pdf"]')
            if chip.count():
                chip.first.scroll_into_view_if_needed(); p.wait_for_timeout(300)
                bb = chip.first.bounding_box()
                if bb: glide(p, bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            p.wait_for_timeout(2200)
        beat(pw, 9, f"/runs/{RUN}/markets/FR", b7)

        # 10 the fix panel: both columns and the price, the button untouched
        def bfix(p):
            p.wait_for_timeout(1100)
            rows = p.locator("tr.scene-row")
            if rows.count():
                rows.first.click(); p.wait_for_timeout(1400)
            fx = p.locator("details.fixer")
            if not fx.count():
                return
            fx.first.scroll_into_view_if_needed()
            fx.first.locator("summary").click()
            p.wait_for_timeout(1600)
            # put the panel's own heading at the top of the frame, not at the fold
            fx.first.evaluate("el => el.scrollIntoView({block: 'start'})")
            p.wait_for_timeout(2000)
            # read down what should change, then down the methods and their prices
            for x, y in ((520, 430), (520, 520), (1240, 430), (1240, 560),
                         (1240, 690), (1240, 820)):
                glide(p, x, y, steps=14)
                p.wait_for_timeout(1500)
            creep(p, 300, 1.4)
        beat(pw, 10, f"/runs/{RUN}/markets/FR", bfix)

        # 11 cutting room: original and localized, playing together
        def b9(p):
            p.wait_for_timeout(1200)
            fr = p.locator('[data-pair="FR"]')
            if fr.count():
                fr.first.scroll_into_view_if_needed()
                p.wait_for_timeout(1200)
                fr.first.evaluate("el => el.querySelectorAll('video')"
                                  ".forEach(v => { v.muted = true; v.play(); })")
            p.wait_for_timeout(12000)
            creep(p, 260, 1.6)
        beat(pw, 11, f"/runs/{RUN}/cutting", b9)

        # 11 agent mode: three questions typed in turn, none of them sent
        def b10(p):
            p.wait_for_timeout(1000)
            box = p.locator("#agent-input")
            if not box.count():
                return
            for q in ["which markets blocked this ad, and why?",
                      "show me the frames FR-ALC-01 fired on",
                      "what should I fix first, and what will it cost?"]:
                box.first.click()
                box.first.fill("")
                box.first.type(q, delay=38)
                p.wait_for_timeout(1500)
        beat(pw, 12, "/agent", b10, base=LOCAL)

        # 12 rule library, then frame search
        def b11(p):
            p.wait_for_timeout(1200)
            creep(p, 1600, 5.0)
            p.goto(LIVE + "/search?q=wine&mode=literal", wait_until="load")
            p.wait_for_timeout(2500)
            creep(p, 900, 2.5)
        beat(pw, 13, "/library", b11)

        # 13 intelligence board
        # Grafana needs ~30s to paint 17 panels; cut_demo drops that head
        beat(pw, 14, "/insight", lambda p: (p.wait_for_timeout(32000), creep(p, 2400, 5.0)))

        # 14 the Grafana inventory, then home
        def b13(p):
            p.wait_for_timeout(1500)
            creep(p, 3000, 8.5)
            p.goto(LIVE + "/", wait_until="load"); p.wait_for_timeout(2600)
        beat(pw, 15, "/grafana", b13)


if __name__ == "__main__":
    main()
