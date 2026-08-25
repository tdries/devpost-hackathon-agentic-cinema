"""How structural a violation is, which decides what can be done about it.

Two findings can share a rule, a severity and a citation and still be
completely different problems. A French tagline on a note is a label: change
the words and the film is unchanged. A Doritos ad built around a baby being
slung through the air is not a label. Quebec's ban on showing children in
unsafe situations does not attach to a detail inside that ad, it attaches to
the idea of it, and no edit reaches an idea.

So every finding carries a scope, and the scope decides which remediation
methods are even offered:

* **frame**    a detail visible in a moment. A patch fixes it.
* **segment**  part of the action inside one shot. A patch fixes it if the
               camera is still; otherwise the motion has to be followed.
* **scene**    the whole shot is the violation. Only regeneration reaches
               it, and only if the shot is short enough for Veo to bridge.
* **concept**  the premise of the commercial is the violation. Nothing in
               this system fixes it, and pretending otherwise would be the
               dishonest kind of automation: it goes to whoever can
               commission a different film.

Scope comes from two places that have to agree, and the stricter one wins.
The adjudicator names it, because "is this the premise" is a judgement about
meaning that only the judge holding the rule can make. The structure of the
findings corroborates it: a rule that fires across most of the running time,
or in most of the shots, is describing the film rather than a moment in it,
whatever the judge called it. Escalation is one-way. A model that calls the
premise a detail gets overruled by the arithmetic; a model that calls a
detail the premise is believed, because it read the film and the arithmetic
did not.
"""
from __future__ import annotations

SCOPES = ("frame", "segment", "scene", "concept")
_RANK = {name: i for i, name in enumerate(SCOPES)}

# A finding shorter than this is a moment, not an action.
_FRAME_MAX_S = 1.2
# One finding covering this much of the whole film is not a moment in it.
_CONCEPT_SPAN_SHARE = 0.6
# A rule covering this much of the running time is describing the film.
_CONCEPT_COVERAGE = 0.45
# ...as is a rule that keeps firing shot after shot AND holds this much of
# the film between those hits. Repetition alone is not a premise: a logo
# glimpsed in four shots of a thirty second ad is still a detail, four
# times over.
_CONCEPT_HITS = 4
_CONCEPT_HITS_COVERAGE = 0.2


def structural(span: float, duration: float, rule_span_total: float,
               rule_hits: int) -> str:
    """Scope from arithmetic alone: how much of the film this rule occupies.

    span: this finding's own length. duration: the asset's. rule_span_total
    and rule_hits: the same rule's total footprint in this market, which is
    what separates "a cigarette in one shot" from "a cigarette in every shot".
    """
    duration = max(duration, 0.001)
    coverage = rule_span_total / duration
    if (coverage >= _CONCEPT_COVERAGE
            or span / duration >= _CONCEPT_SPAN_SHARE
            or (rule_hits >= _CONCEPT_HITS and coverage >= _CONCEPT_HITS_COVERAGE)):
        return "concept"
    if span <= _FRAME_MAX_S:
        return "frame"
    return "segment"

# Note what this deliberately never returns: "scene". Arithmetic can see that
# a rule owns the film, because that is a measurement. It cannot see that a
# shot IS the violation rather than containing one: a wine glass on the table
# through a seven second take is a detail in a long shot, and a baby slung
# through the air for the same seven seconds is the shot itself. Only the
# judge holding the rule can tell those apart, so "scene" arrives from the
# adjudicator or not at all.


def merge(judged: str | None, measured: str) -> str:
    """The stricter of what the judge said and what the numbers show."""
    if judged not in _RANK:
        return measured
    return judged if _RANK[judged] >= _RANK[measured] else measured


def classify(finding, findings, duration: float) -> str:
    """The scope of one finding, in the context of its market's findings."""
    same_rule = [f for f in findings
                 if f.rule_id == finding.rule_id and f.market == finding.market]
    total = sum(max(0.0, f.t_end - f.t_start) for f in same_rule)
    measured = structural(max(0.0, finding.t_end - finding.t_start),
                          duration, total, len(same_rule))
    return merge(getattr(finding, "scope", None) or None, measured)


# Which methods can reach which scope. A patch cannot fix a shot, and
# nothing here fixes an idea.
_METHODS_BY_SCOPE = {
    "frame": ("overlay", "track", "bridge"),
    "segment": ("overlay", "track", "bridge"),
    "scene": ("bridge",),
    "concept": (),
}

_WHY_NOT = {
    ("scene", "overlay"): "The whole shot is the violation; a patch over one frame cannot reach it.",
    ("scene", "track"): "The whole shot is the violation; there is no clean part of it to propagate.",
    ("concept", "overlay"): "The premise of the commercial is the violation. No edit reaches it.",
    ("concept", "track"): "The premise of the commercial is the violation. No edit reaches it.",
    ("concept", "bridge"): "The premise of the commercial is the violation. Regenerating the shot would regenerate the problem.",
}


def allows(scope: str, method: str) -> tuple[bool, str]:
    """Whether this method can address a violation of this scope."""
    if method in _METHODS_BY_SCOPE.get(scope, ()):
        return True, ""
    return False, _WHY_NOT.get((scope, method), "Not applicable at this scope.")


DESCRIPTION = {
    "frame": "a detail in a moment",
    "segment": "part of the action in one shot",
    "scene": "the whole shot is the violation",
    "concept": "the premise of the commercial is the violation",
}

VERDICT = {
    "concept": ("This is not an edit. The commercial is built on what the rule "
                "forbids, so clearing this market means a different film, not a "
                "different frame."),
    "scene": ("The shot itself has to change. Regeneration can reach it if it is "
              "short enough; otherwise it is a reshoot."),
}
