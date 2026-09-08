"""Which manual pages overrun their fixed 297mm, and by how much.

    <playwright venv>/bin/python scripts/check_manual.py

A page is `overflow: hidden`, so content that does not fit is silently cut and
the absolute footer prints on top of whatever is still there. This measures the
gap instead of leaving it to the eye.
"""
import pathlib, sys
from playwright.sync_api import sync_playwright

url = "file://" + str(pathlib.Path("docs/manual-white/manual.html").resolve())
with sync_playwright() as pw:
    b = pw.chromium.launch()
    p = b.new_context(viewport={"width": 1200, "height": 1400}).new_page()
    p.goto(url, wait_until="networkidle")
    p.wait_for_timeout(1500)
    rows = p.evaluate("""() => {
      const mm = 96 / 25.4;                      // CSS px per mm at 1x
      const out = [];
      document.querySelectorAll('.page').forEach((pg, i) => {
        const top = pg.getBoundingClientRect().top;
        // the footer sits 11mm from the bottom; content must clear it
        const limit = top + (297 - 15) * mm;
        let low = top;
        pg.querySelectorAll(':scope > *').forEach(el => {
          if (el.classList.contains('folio')) return;
          const r = el.getBoundingClientRect();
          if (r.bottom > low) low = r.bottom;
        });
        out.push({page: i + 1, over: Math.round((low - limit) / mm)});
      });
      return out;
    }""")
    b.close()

bad = [r for r in rows if r["over"] > 0]
for r in bad:
    print(f"page {r['page']:>2}: {r['over']:>3} mm over")
tight = [r for r in rows if -12 < r["over"] <= 0]
for r in tight:
    print(f"page {r['page']:>2}: {abs(r['over']):>3} mm to spare (tight)")
print(f"\n{len(bad)} page(s) overflow, {len(rows)} total")
sys.exit(1 if bad else 0)
