"""The overlays: a chip naming the screen, and pointers to what is being said.

    python3 scripts/make_overlays.py  ->  docs/video/ov/*.png

Two shapes, both in the console's own language: the four brand colours, a
hairline, mono capitals on ink. A chip sits bottom left and names the screen.
A callout draws a rounded rectangle around the thing under discussion, with
corner ticks in the brand colours and its label on a tab above it.
"""
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
FONT = "/System/Library/Fonts/Supplemental/Avenir Next.ttc"
MONO = "/System/Library/Fonts/Menlo.ttc"
INK = (23, 33, 37)
BRAND = [(66, 133, 244), (234, 67, 53), (251, 188, 5), (52, 168, 83)]
OUT = "docs/video/ov"

# screen chips: the name of the page, and the path it lives at
CHIPS = {
    2: ("HOME", "/"),
    3: ("HOW IT WORKS", "the flow"),
    4: ("ARCHIVE", "/runs"),
    5: ("NEW CLEARANCE", "/new"),
    6: ("MISSION FEED", "the crew, live"),
    7: ("LAUNCH BOARD", "the verdict per market"),
    8: ("TIMELINE", "findings across the film"),
    9: ("MARKET ROOM", "France"),
    10: ("THE FIX PANEL", "what changes, and how"),
    11: ("CUTTING ROOM", "original beside localized"),
    12: ("AGENT MODE", "/agent"),
    13: ("RULE LIBRARY  ·  FRAME SEARCH", "128 rules, every caption"),
    14: ("INTELLIGENCE", "across every run"),
    15: ("GRAFANA RESOURCES", "/grafana"),
}

# callouts: beat -> list of (name, x, y, w, h, label)
CALLS = {
    4: [("lanes", 138, 168, 1650, 210, "what it found, and when")],
    7: [("tiles", 128, 236, 1664, 300, "one tile per market"),
        ("panels", 128, 700, 1664, 330, "live Grafana, built by the agent")],
    8: [("grid", 500, 430, 1180, 420, "a live Grafana panel")],
    9: [("finding", 330, 590, 1300, 260, "the frame, the rule, the statute"),
        ("chips", 1090, 380, 640, 60, "certificate and markers")],
    10: [("change", 300, 470, 760, 300, "what should change"),
         ("how", 1090, 460, 780, 560, "how to do it, priced")],
    11: [("pair", 950, 300, 900, 500, "the localized master")],
    12: [("ask", 330, 1010, 700, 90, "ask in plain sentences")],
}


def rounded(d, box, r, outline, width):
    d.rounded_rectangle(box, radius=r, outline=outline, width=width)


def chip(title, path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    ft = ImageFont.truetype(MONO, 35)
    fp = ImageFont.truetype(FONT, 29, index=7)
    tw = max(d.textlength(title, font=ft), d.textlength(path, font=fp))
    bw, bh = int(tw) + 100, 140
    x, y = 96, H - bh - 78
    d.rounded_rectangle([x, y, x + bw, y + bh], radius=18, fill=INK + (232,))
    for i, c in enumerate(BRAND):          # the four colours, as a rule down the left
        top = y + 20 + i * ((bh - 40) // 4)
        d.rectangle([x + 18, top, x + 25, top + (bh - 40) // 4 - 6], fill=c + (255,))
    d.text((x + 52, y + 30), title, font=ft, fill=(255, 255, 255, 246))
    d.text((x + 52, y + 82), path, font=fp, fill=(176, 190, 197, 232))
    im.save(f"{OUT}/chip-{title.split()[0].lower()}-{abs(hash(path)) % 9999}.png")
    return im


def callout(x, y, w, h, label, glow=1.0):
    """One frame of a pointer. `glow` 0..1 breathes the halo and the stroke."""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    box = [x, y, x + w, y + h]
    # three rings of halo, widest and faintest outside, all riding on `glow`
    for i, (spread, base) in enumerate(((13, 46), (9, 74), (5, 104))):
        a = int(base * (0.25 + 0.75 * glow))
        rounded(d, [v + o for v, o in zip(box, (-spread, -spread, spread, spread))],
                16 + spread, (120, 176, 255, a), 9 - i * 2)
    rounded(d, [v + o for v, o in zip(box, (-3, -3, 3, 3))], 16,
            (255, 255, 255, int(70 + 90 * glow)), 7)
    rounded(d, box, 13, (26, 115, 232, int(200 + 55 * glow)), 4)
    tick = 34                               # corner ticks, one per brand colour
    for (cx, cy, dx, dy), c in zip([(x, y, 1, 1), (x + w, y, -1, 1),
                                    (x, y + h, 1, -1), (x + w, y + h, -1, -1)], BRAND):
        d.line([cx, cy, cx + dx * tick, cy], fill=c + (255,), width=7)
        d.line([cx, cy, cx, cy + dy * tick], fill=c + (255,), width=7)
    f = ImageFont.truetype(MONO, 25)
    tw = d.textlength(label.upper(), font=f)
    ty = y - 54 if y > 70 else y + h + 12
    d.rounded_rectangle([x - 4, ty, x + tw + 44, ty + 46], radius=10, fill=INK + (240,))
    d.text((x + 16, ty + 9), label.upper(), font=f, fill=(255, 255, 255, 246))
    return im


# beat 1: the risk each opening shot carries, tagged as it plays
TAGS = [("modesty dress body", "MODESTY & DRESS", "how much skin a market allows", 2),
        ("alcohol tobacco drugs", "ALCOHOL", "FR: Loi Evin bans it on television", 1),
        ("gesture body language", "GESTURE", "an insult across the Gulf and West Africa", 0),
        ("comparative claims", "COMPARATIVE CLAIM", "FR: it has to be substantiated", 3),
        ("alcohol tobacco drugs", "ALCOHOL, AGAIN", "and a different rule in every market", 1)]


def tag(kicker, line, colour, dim=None):
    """A risk tag for the opening: dark card, a brand rule, the taxonomy mark.

    The icon is the console's own mark for that dimension, so the category a
    viewer meets in the first fifteen seconds is the one they meet again on
    every finding, the frame board and the timeline.
    """
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    fk = ImageFont.truetype(MONO, 34)
    fl = ImageFont.truetype(FONT, 27, index=0)
    icon = None
    if dim and os.path.exists(f"docs/video/dims/{dim}.png"):
        icon = Image.open(f"docs/video/dims/{dim}.png").convert("RGBA")
        icon = icon.resize((74, 74), Image.LANCZOS)
    lead = 46 + (icon.width + 26 if icon else 0)
    tw = max(d.textlength(kicker, font=fk), d.textlength(line, font=fl))
    bw, bh = int(tw) + lead + 50, 132
    x, y = 110, H - bh - 110
    d.rounded_rectangle([x, y, x + bw, y + bh], radius=16, fill=INK + (232,))
    d.rectangle([x + 16, y + 20, x + 23, y + bh - 20], fill=BRAND[colour] + (255,))
    if icon:
        im.alpha_composite(icon, (x + 46, y + (bh - icon.height) // 2))
    d.text((x + lead, y + 26), kicker, font=fk, fill=(255, 255, 255, 248))
    d.text((x + lead, y + 78), line, font=fl, fill=(182, 195, 202, 235))
    return im


if __name__ == "__main__":
  os.makedirs(OUT, exist_ok=True)
  for i, (_dim, kicker, line, colour) in enumerate(TAGS):
      tag(kicker, line, colour, _dim.replace(" ", "_")).save(f"{OUT}/tag-01-{i}.png")
  made = []
  for n, (title, path) in CHIPS.items():
      im = chip(title, path)
      im.save(f"{OUT}/chip-{n:02d}.png")
      made.append(f"chip-{n:02d}")
  for n, items in CALLS.items():
      for name, x, y, w, h, label in items:
          callout(x, y, w, h, label).save(f"{OUT}/call-{n:02d}-{name}.png")
          made.append(f"call-{n:02d}-{name}")
  print(f"{len(made)} overlays written to {OUT}")
