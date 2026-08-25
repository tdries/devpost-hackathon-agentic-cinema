"""Turn the agent's answer into the interface's own language.

The agent replies in prose. It was rendered with textContent, so a
sentence naming a market, a rule and a timecode arrived as an
undifferentiated grey line -- while three feet away the same three things
are a country chip, a legal chip and a mono timecode. The console was
speaking two dialects.

Two passes, in this order and never the other way round:

1. Markdown, a deliberately small subset: paragraphs, bullet and numbered
   lists, bold, italic, inline code. Enough to structure an answer,
   nothing that can carry a link or an image.
2. Entities: the things this product has a symbol for. A rule id becomes
   its class chip, a market code its country or channel chip, a dimension
   its taxonomy icon, a timecode a mono span, a severity its colour.

Everything is escaped BEFORE any markup is inserted, and the entity pass
runs over already-escaped text. Nothing the model writes can become an
element -- it can only be recognised as one of ours.
"""
from __future__ import annotations

import html
import re

from customs.state import colour_for_severity

# --- what we can recognise -------------------------------------------------

_RULE = re.compile(r"\b([A-Z]{2}(?:-[A-Z0-9]{1,8})*-[A-Z]{3,6}-\d{2})\b")
_TIME = re.compile(r"\b(\d{1,2}:\d{2}(?:\.\d)?|\d{1,3}\.\d\s?[-–]\s?\d{1,3}\.\d\s?s|\d{1,3}\.\d\s?s)\b")
_SEV = re.compile(r"\bseverity\s+(\d{1,3})\b", re.I)
_CODE = re.compile(r"`([^`\n]{1,200})`")
_BOLD = re.compile(r"\*\*([^*\n]{1,300})\*\*")
_ITAL = re.compile(r"(?<![*\w])\*([^*\n]{1,300})\*(?![*\w])")

_DIMENSIONS = (
    "alcohol_tobacco_drugs", "religious_symbols_practices", "modesty_dress_body",
    "gesture_body_language", "food_and_animals", "gender_portrayal",
    "sexual_orientation_gender_id", "children_and_minors",
    "national_symbols_politics", "health_claims_pharma", "gambling_and_finance",
    "violence_and_weapons", "language_profanity_idiom", "humour_irony_satire",
    "superstition_number_colour", "photosensitivity_sensory", "text_legibility",
    "comparative_claims",
)
_KLASS_ICON = {"legal": "i-legal", "policy": "i-policy", "offence": "i-offence"}


def _dimension_pattern() -> re.Pattern:
    words = sorted(_DIMENSIONS, key=len, reverse=True)
    both = [w for w in words] + [w.replace("_", " ") for w in words]
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in both) + r")\b", re.I)


_DIM = _dimension_pattern()


def _icon(symbol: str, extra: str = "") -> str:
    return f'<svg class="ic{(" " + extra) if extra else ""}"><use href="#{symbol}"/></svg>'


def _chip(symbol: str, text: str, cls: str = "") -> str:
    return (f'<span class="chip inl {cls}">{_icon(symbol)}'
            f'<span>{text}</span></span>')


# --- the markdown subset ---------------------------------------------------

def _inline(text: str, markets: set[str], rules: dict[str, str]) -> str:
    """Inline markup and entities, over ALREADY-ESCAPED text."""
    out = _CODE.sub(lambda m: f'<code>{m.group(1)}</code>', text)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITAL.sub(lambda m: f"<em>{m.group(1)}</em>", out)

    out = _RULE.sub(lambda m: _chip(_KLASS_ICON.get(rules.get(m.group(1), "legal"),
                                                    "i-legal"),
                                    m.group(1), "rule"), out)
    out = _SEV.sub(lambda m: (
        f'<span class="sev-inl" style="--sev:{colour_for_severity(int(m.group(1)))}">'
        f'severity <b>{m.group(1)}</b></span>'), out)
    out = _TIME.sub(lambda m: f'<span class="tc-inl mono">{m.group(1)}</span>', out)
    out = _DIM.sub(lambda m: _chip(f'd-{m.group(1).lower().replace(" ", "_")}',
                                   m.group(1).replace("_", " "), "dim"), out)

    # `AE-MOD-01` in backticks becomes a code span and THEN a rule chip,
    # which nests a chip inside code and reads as neither. The chip is the
    # better rendering, so it wins.
    out = re.sub(r"<code>(<span class=\"chip inl rule\">.*?</span></span>)</code>",
                 r"\1", out)

    if markets:
        pattern = re.compile(r"\b(" + "|".join(
            re.escape(m) for m in sorted(markets, key=len, reverse=True)) + r")\b")
        # a market code inside a chip we already made must not be re-wrapped
        parts, last = [], 0
        for m in re.finditer(r"<span class=\"chip[^>]*>.*?</span></span>", out):
            parts.append(pattern.sub(_market, out[last:m.start()]))
            parts.append(m.group(0))
            last = m.end()
        parts.append(pattern.sub(_market, out[last:]))
        out = "".join(parts)
    return out


def _market(m: re.Match) -> str:
    code = m.group(1)
    symbol = "n-cut" if "-" in code and len(code.split("-")[-1]) > 2 else f"c-{code}"
    return _chip(symbol, code, "mkt")


def render(text: str, markets=None, rules=None) -> str:
    """Agent prose -> the console's own markup. Safe by construction."""
    markets = set(markets or ())
    rules = dict(rules or {})
    blocks: list[str] = []
    items: list[str] = []
    ordered = False

    def flush():
        nonlocal items, ordered
        if items:
            tag = "ol" if ordered else "ul"
            blocks.append(f"<{tag}>" + "".join(f"<li>{i}</li>" for i in items) + f"</{tag}>")
            items = []

    for raw in (text or "").split("\n"):
        line = html.escape(raw.rstrip(), quote=False)
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        bullet = re.match(r"^[-*•]\s+(.*)$", stripped)
        number = re.match(r"^(\d{1,2})[.)]\s+(.*)$", stripped)
        if bullet:
            if not items:
                ordered = False
            items.append(_inline(bullet.group(1), markets, rules))
        elif number:
            if not items:
                ordered = True
            items.append(_inline(number.group(2), markets, rules))
        else:
            flush()
            blocks.append(f"<p>{_inline(stripped, markets, rules)}</p>")
    flush()
    return "".join(blocks)
