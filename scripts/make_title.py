"""The title card: the mark, the name, and the line, over the opening footage.

    python3 scripts/make_title.py  ->  docs/video/title.png  (1920x1080 RGBA)
"""
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
FONT = "/System/Library/Fonts/Supplemental/Avenir Next.ttc"
card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d = ImageDraw.Draw(card)

# a scrim, heaviest in the middle, so the footage stays readable underneath
for y in range(H):
    edge = min(y, H - y) / (H / 2)
    d.line([(0, y), (W, y)], fill=(8, 12, 16, int(150 + 85 * min(1.0, edge * 1.6))))

logo = Image.open("src/customs/static/logo.png").convert("RGBA")
lw = 560
logo = logo.resize((lw, int(logo.height * lw / logo.width)), Image.LANCZOS)
card.alpha_composite(logo, ((W - lw) // 2, H // 2 - 300))

name = ImageFont.truetype(FONT, 104, index=2)      # Avenir Next Demi Bold
sub = ImageFont.truetype(FONT, 30, index=0)
title = "T H E   M E D I A   C U S T O M S"
tw = d.textlength(title, font=name)
d.text(((W - tw) / 2, H // 2 + 20), title, font=name, fill=(255, 255, 255, 245))

line = "one asset  ·  every market  ·  before it ships"
sw = d.textlength(line, font=sub)
d.text(((W - sw) / 2, H // 2 + 170), line, font=sub, fill=(226, 232, 236, 205))

# the four brand colours, as a rule under the line
bar_w, x0, y0 = 520, (W - 520) // 2, H // 2 + 240
for i, c in enumerate([(66, 133, 244), (234, 67, 53), (251, 188, 5), (52, 168, 83)]):
    d.rectangle([x0 + i * bar_w // 4, y0, x0 + (i + 1) * bar_w // 4 - 6, y0 + 5],
                fill=c + (235,))

card.save("docs/video/title.png")

# the closing card: the same mark, over a heavier scrim, with the line that ends it
end = Image.new("RGBA", (W, H), (8, 12, 16, 236))
d2 = ImageDraw.Draw(end)
end.alpha_composite(logo, ((W - lw) // 2, H // 2 - 300))
tw2 = d2.textlength(title, font=name)
d2.text(((W - tw2) / 2, H // 2 + 20), title, font=name, fill=(255, 255, 255, 250))
closing = "Gemini  ·  Google Cloud Agent Builder  ·  Grafana"
cw = d2.textlength(closing, font=sub)
d2.text(((W - cw) / 2, H // 2 + 170), closing, font=sub, fill=(226, 232, 236, 215))
for i, c in enumerate([(66, 133, 244), (234, 67, 53), (251, 188, 5), (52, 168, 83)]):
    d2.rectangle([x0 + i * bar_w // 4, y0, x0 + (i + 1) * bar_w // 4 - 6, y0 + 5],
                 fill=c + (240,))
end.save("docs/video/endcard.png")
print("title and end cards written")
