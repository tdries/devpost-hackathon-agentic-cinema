"""The title and closing cards: the mark, the name, and what it is built on.

    python3 scripts/make_title.py  ->  docs/video/title.png, docs/video/endcard.png
"""
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
FONT = "/System/Library/Fonts/Supplemental/Avenir Next.ttc"
NAME = ImageFont.truetype(FONT, 104, index=2)
SUB = ImageFont.truetype(FONT, 30, index=0)
CAP = ImageFont.truetype(FONT, 21, index=2)
TITLE = "T H E   M E D I A   C U S T O M S"
BRAND = [(66, 133, 244), (234, 67, 53), (251, 188, 5), (52, 168, 83)]
MARKS = [("google.png", "Google Cloud"), ("adk.png", "Agent Builder"),
         ("omni.png", "Gemini Omni"), ("veo.png", "Veo 3.1"),
         ("grafana.png", "Grafana")]


def google_wordmark(height=88):
    """The wordmark, drawn letter by letter in the four brand colours."""
    f = ImageFont.truetype(FONT, int(height * 1.05), index=5)   # Medium, closest to the wordmark
    letters = list(zip("Google", [BRAND[0], BRAND[1], BRAND[2], BRAND[0],
                                  BRAND[3], BRAND[1]]))
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    w = int(sum(probe.textlength(c, font=f) for c, _ in letters)) + 12
    im = Image.new("RGBA", (w, int(height * 1.7)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x = 4
    for c, colour in letters:
        d.text((x, 0), c, font=f, fill=colour + (255,))
        x += probe.textlength(c, font=f)
    return im.crop(im.getbbox())


def marks_row(card, d, y, height=84, gap=96):
    """The four marks this is built on, evenly spaced, each captioned."""
    loaded = []
    for f, label in MARKS:
        im = Image.open(f"docs/video/marks/{f}").convert("RGBA")
        im = im.resize((int(im.width * height / im.height), height), Image.LANCZOS)
        loaded.append((im, label))
    widths = [max(im.width, d.textlength(lab, font=CAP)) for im, lab in loaded]
    total = sum(widths) + gap * (len(loaded) - 1)
    x = (W - total) / 2
    for (im, label), w in zip(loaded, widths):
        card.alpha_composite(im, (int(x + (w - im.width) / 2), y))
        tw = d.textlength(label, font=CAP)
        d.text((x + (w - tw) / 2, y + height + 18), label, font=CAP,
               fill=(214, 222, 228, 225))
        x += w + gap


def bars(d, y, width=520):
    x0 = (W - width) // 2
    for i, c in enumerate(BRAND):
        d.rectangle([x0 + i * width // 4, y, x0 + (i + 1) * width // 4 - 6, y + 5],
                    fill=c + (238,))


def base(scrim):
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    if callable(scrim):
        for y in range(H):
            d.line([(0, y), (W, y)], fill=(8, 12, 16, scrim(y)))
    else:
        d.rectangle([0, 0, W, H], fill=(8, 12, 16, scrim))
    logo = Image.open("src/customs/static/logo.png").convert("RGBA")
    lw = 480
    logo = logo.resize((lw, int(logo.height * lw / logo.width)), Image.LANCZOS)
    card.alpha_composite(logo, ((W - lw) // 2, H // 2 - 350))
    tw = d.textlength(TITLE, font=NAME)
    d.text(((W - tw) / 2, H // 2 - 70), TITLE, font=NAME, fill=(255, 255, 255, 246))
    return card, d


google_wordmark().save("docs/video/marks/google.png")

# the opening: a scrim that leaves the footage readable at the edges
card, d = base(lambda y: int(150 + 85 * min(1.0, (min(y, H - y) / (H / 2)) * 1.6)))
line = "one asset  ·  every market  ·  before it ships"
lw2 = d.textlength(line, font=SUB)
d.text(((W - lw2) / 2, H // 2 + 76), line, font=SUB, fill=(226, 232, 236, 210))
bars(d, H // 2 + 146)
marks_row(card, d, H // 2 + 220)
card.save("docs/video/title.png")

# the closing: the same, held over a heavier scrim
end, d2 = base(238)
closing = "built on Google Cloud and Grafana"
cw = d2.textlength(closing, font=SUB)
d2.text(((W - cw) / 2, H // 2 + 76), closing, font=SUB, fill=(226, 232, 236, 220))
bars(d2, H // 2 + 146)
marks_row(end, d2, H // 2 + 220)
end.save("docs/video/endcard.png")
print("title and end cards written")
