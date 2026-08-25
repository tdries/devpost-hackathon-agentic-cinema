"""What a fix costs, how hard it is, and what the day's budget has left.

Three ways to put a compliant version of a shot on screen, in order of what
they touch:

* **overlay** edits one frame and holds it over the span. Cheap, instant, and
  right only when the picture is not moving: on a locked-off shot it is
  invisible, on a moving shot it reads as a freeze frame.
* **track** edits one frame into a clean plate and warps it across every
  frame of the span with optical flow, so the patch stays glued to a flat
  surface (a pack, a sign, a label) while the camera moves. Not built yet;
  it is here because an operator choosing between the other two deserves to
  know it is the right answer for most moving shots.
* **bridge** edits the first and last frame of the span and has Veo generate
  the motion between them, anchored on both. It is the only one that can
  follow genuine 3D motion, and it is the only one that regenerates pixels
  the brand did not shoot, so it costs real money and is capped hard.

Prices are estimates in euro, deliberately rounded up, and they exist to be
shown to the operator BEFORE the work runs rather than discovered on an
invoice. Veo bills per second of output and will not emit less than four,
so a two second bridge and a four second bridge cost the same.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Veo 3.1, per second of generated video, rounded up from list price. Veo
# refuses to emit less than MIN_BRIDGE_S or more than MAX_BRIDGE_S, so a
# span outside that range cannot be bridged at all.
_EURO_PER_VIDEO_SECOND = 0.45
MIN_BRIDGE_S = 4.0
MAX_BRIDGE_S = 8.0

# One Gemini image edit. Overlay spends one; a bridge spends two (both ends).
_EURO_PER_IMAGE_EDIT = 0.04

# The whole system's Veo allowance for one day. Spent, it stays spent until
# midnight UTC: a twenty second cigarette scene is exactly the thing this
# stops someone from regenerating on a whim. Raised to 10 and then to
# 20 EUR on 2026-08-25, both at the operator's request.
DAILY_BUDGET_EUR = 20.0


@dataclass(frozen=True)
class Method:
    key: str
    name: str
    how: str
    complexity: str
    best_for: str


METHODS = (
    Method("overlay", "Patch one frame",
           "Edits a single frame and holds it over the span.",
           "low", "a locked-off shot, where nothing moves"),
    Method("track", "Track and propagate",
           "Edits one clean frame and warps it across the span with optical flow.",
           "medium", "a moving camera over a flat target: a pack, a sign, a label"),
    Method("bridge", "Regenerate with Veo",
           "Edits both ends of the span and generates the motion between them.",
           "high", "genuine 3D motion, where a patch cannot hold"),
)

_BY_KEY = {m.key: m for m in METHODS}


def bridge_seconds(span: float) -> float:
    """What Veo would actually bill for a span this long."""
    return min(MAX_BRIDGE_S, max(MIN_BRIDGE_S, math.ceil(span)))


def estimate(method: str, span: float) -> float:
    """Euro estimate for one fix, rounded up to the cent."""
    if method == "bridge":
        raw = 2 * _EURO_PER_IMAGE_EDIT + bridge_seconds(span) * _EURO_PER_VIDEO_SECOND
    elif method in ("overlay", "track"):
        raw = _EURO_PER_IMAGE_EDIT
    else:
        raise ValueError(f"unknown method: {method!r}")
    return math.ceil(raw * 100) / 100


def available(method: str, span: float, spent_today: float) -> tuple[bool, str]:
    """Whether this method may run on this span right now, and why not.

    The reason is written to be shown to an operator, so it says what to do
    instead rather than naming a limit.
    """
    if method == "track":
        return False, "Not built yet: this is the right answer for a moving shot over a flat target."
    if method == "overlay":
        return True, ""
    if method != "bridge":
        return False, f"Unknown method {method}."
    if span > MAX_BRIDGE_S:
        # .1f, not .0f: an 8.1s span rounded to "8s is longer than Veo will
        # generate in one piece (8s)", which reads as a bug rather than a limit.
        return False, (f"{span:.1f}s is longer than Veo will generate in one piece "
                       f"({MAX_BRIDGE_S:.0f}s). Cut the finding's span, or send it to an editor.")
    price = estimate("bridge", span)
    if spent_today + price > DAILY_BUDGET_EUR:
        left = max(0.0, DAILY_BUDGET_EUR - spent_today)
        return False, (f"Today's generation budget has {left:.2f} EUR left and this "
                       f"costs {price:.2f} EUR. It resets at midnight UTC.")
    return True, ""


def options(span: float, spent_today: float, scope: str = "segment",
            substitutable: bool = True) -> list[dict]:
    """Every method with its price and whether it can run, for the console.

    Scope no longer closes doors. It rides along as `caveat`, the sentence
    explaining why this method is a poor fit for a violation of this shape,
    and the operator decides. `available` reflects only what genuinely
    cannot run: an unimplemented method, a span longer than Veo will
    generate, or a price the day's budget will not cover.
    """
    from customs import scope as scope_mod

    out = []
    for method in METHODS:
        fits, caveat = scope_mod.allows(scope, method.key, substitutable)
        ok, why = available(method.key, span, spent_today)
        out.append({
            "key": method.key, "name": method.name, "how": method.how,
            "complexity": method.complexity, "best_for": method.best_for,
            "eur": estimate(method.key, span),
            "length": (f"{bridge_seconds(span):.0f}s generated"
                       if method.key == "bridge" else f"{span:.1f}s patched"),
            "available": ok, "why_not": why,
            "caveat": "" if fits else caveat,
        })
    return out


# --- what to change it into ---------------------------------------------
#
# Three concrete choices per dimension, because "remediate this" is not a
# decision anyone can make: swapping the wine for tea, emptying the glass and
# removing the glass are three different films, and the operator owns that
# call, not the model.

# One dimension covers alcohol, tobacco AND drugs, so this fallback cannot
# name a drink: it was offering "swap the drink for a non-alcoholic one" over
# a lit cigarette. Wording here stays neutral about the substance; the
# specific naming comes from the judge's own remedies, which saw the frame.
_SUGGESTIONS = {
    "alcohol_tobacco_drugs": [
        ("swap", "Swap it for a permitted product"),
        ("empty", "Keep the container, change the contents"),
        ("remove", "Remove it from the shot entirely"),
    ],
    "modesty_dress_body": [
        ("cover", "Extend the clothing to cover more"),
        ("reframe", "Reframe the shot to exclude it"),
        ("replace", "Replace the garment with a modest one"),
    ],
    "religious_symbols_practices": [
        ("remove", "Remove the symbol"),
        ("neutral", "Replace it with a neutral object"),
        ("reframe", "Reframe to keep it out of shot"),
    ],
    "text_legibility": [
        ("translate", "Re-letter the text in the market's language"),
        ("neutral", "Replace it with wordless artwork"),
        ("remove", "Remove the text from the surface"),
    ],
    "comparative_claims": [
        ("qualify", "Qualify the claim so it is substantiated"),
        ("soften", "Restate it without the comparison"),
        ("drop", "Cut the claim from the line"),
    ],
    "children_and_minors": [
        ("adult", "Recast with an adult in the child's place"),
        ("object", "Put an object where the child is"),
        ("remove", "Remove the sequence"),
    ],
    "gambling_and_finance": [
        ("remove", "Remove the betting imagery"),
        ("neutral", "Replace it with a neutral activity"),
        ("disclaim", "Add the market's required wording"),
    ],
    "violence_and_weapons": [
        ("remove", "Remove the weapon"),
        ("neutral", "Replace it with a harmless object"),
        ("reframe", "Reframe to exclude the action"),
    ],
    "food_and_animals": [
        ("swap", "Swap the food for an acceptable one"),
        ("remove", "Remove it from the shot"),
        ("reframe", "Reframe so it is not visible"),
    ],
    "photosensitivity_sensory": [
        ("slow", "Slow the flashing below the threshold"),
        ("dim", "Reduce the contrast of the flashes"),
        ("cut", "Cut the sequence"),
    ],
}

_DEFAULT_SUGGESTIONS = [
    ("neutral", "Replace it with something market-appropriate"),
    ("remove", "Remove it from the shot"),
    ("reframe", "Reframe the shot to exclude it"),
]


def suggestions(dimension: str, finding=None) -> list[dict]:
    """Three concrete ways to deal with a finding.

    The judge writes three while it is deciding the finding, so they name the
    thing it actually saw. Those win. The dimension table is the fallback for
    findings judged before that existed, and it can only ever speak in
    categories -- which is how a cigarette came to be offered a non-alcoholic
    drink, both being alcohol_tobacco_drugs.

    A judge-written remedy is addressed by index, never by its text: the
    directive reaches the image editor from the stored finding, so nothing a
    model wrote has to survive a round trip through a form field.
    """
    own = list(getattr(finding, "remedies", None) or [])
    if own:
        return [{"key": f"remedy:{i}", "label": r["label"]}
                for i, r in enumerate(own) if r.get("label")]
    raw = _SUGGESTIONS.get(dimension, _DEFAULT_SUGGESTIONS)
    return [{"key": key, "label": label} for key, label in raw]
