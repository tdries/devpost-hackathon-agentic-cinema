"""Find frames by what the analyst said about them, across every run.

The captions are already in Loki: one `kind="observation"` line per
keyframe, carrying the analyst's sentence in its body. That makes "which
frames have a rabbit in them" a query rather than a feature, and it makes
it a query ACROSS runs, because Loki holds every run this instance ever
performed and labels each line with its asset.

Matching is SEMANTIC by default, and that is the whole point. The analyst
chose its own words months ago: it wrote "an animated rabbit holds a
cigar", nobody asked it to write "bunny". A keyword search makes the
person asking guess the vocabulary of a model that has already stopped
talking, and quietly answers "none" when they guess wrong. So the question
and the captions go to Gemini together, and it decides which frames the
question is about -- "bunnies" finds the rabbit, "a woman drinking" finds
"she raises a glass of red wine to her lips", and a carrot does not count
as a rabbit.

Literal mode is kept for the cases semantics is wrong for: an exact rule
id, a brand name, a regex somebody means literally. It runs as two stages,
because Loki's own line filter matches the whole JSON line and would
otherwise count a rule named RABBIT-01 as a rabbit on screen:

  1. Loki's filter narrows the stream server-side.
  2. The same pattern is re-applied here against the parsed `statement`
     field alone, which is the caption and only the caption.
"""
from __future__ import annotations

import json
import re
from collections import Counter

from customs.config import settings

# A caption is a sentence, so the search is a case-insensitive regex over
# it. Callers hand in plain words ("bunny", "short skirt") and get regex
# for free: "bunn|rabbit|hare" is a legitimate and useful thing to ask,
# which is the whole point of not making this a menu.
_MAX_PATTERN = 200


class SearchError(ValueError):
    """A pattern the caller can fix, phrased for whoever typed it."""


def _compile(text: str) -> re.Pattern | None:
    pattern = (text or "").strip()
    if not pattern:
        return None
    if len(pattern) > _MAX_PATTERN:
        raise SearchError(f"that pattern is {len(pattern)} characters; "
                          f"the limit is {_MAX_PATTERN}")
    # A leading word boundary, because "hare" found SHARE and "bunn" has to
    # keep finding bunnies: anchoring the start and not the end is what
    # makes a prefix search work and a substring accident stop. A pattern
    # that already anchors itself is left exactly as written.
    if not pattern.startswith(("\\b", "^", "(")):
        pattern = r"\b(?:" + pattern + ")"
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise SearchError(f"that is not a usable pattern: {exc}") from None


def logql(text: str, dimension: str = "", flagged: str = "") -> str:
    """The stream selector plus the line filter, as Loki will see it."""
    selector = ['app="customs"', 'kind="observation"']
    if dimension:
        selector.append(f'dimension="{dimension}"')
    if flagged in ("yes", "no"):
        selector.append(f'flagged="{flagged}"')
    query = "{" + ", ".join(selector) + "}"
    pattern = (text or "").strip()
    if pattern:
        # Loki's regex filter, case-insensitive. Quotes are escaped because
        # the pattern travels inside a quoted LogQL string.
        query += ' |~ "(?i)' + pattern.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return query


# How many captions go to the model in one call. They are one sentence
# each, so a few hundred is a small prompt; the batches run together
# because five serial calls is five times the wait for no benefit.
_BATCH = 300
_MAX_BATCHES = 8

_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["n"],
            },
        }
    },
    "required": ["matches"],
}

_PROMPT = """You are filtering frames of television commercials.

Below are numbered descriptions. Each was written by a vision model looking
at one frame, in its own words, without knowing this question would ever be
asked.

Return every number whose description is about: {question}

Judge by MEANING, not by wording:
* "bunnies" matches "an animated rabbit", "a hare crosses the road".
* "a woman drinking" matches "she raises a glass of red wine to her lips".
* "short skirt" matches "a hemline well above the knee".

Do not stretch it. Something merely adjacent to the subject is not the
subject: a carrot is not a rabbit, an ashtray is not a lit cigarette, and a
bar is not somebody drinking. If nothing matches, return an empty list.

`why` is at most eight words, quoting the part of the description that
decided it.

DESCRIPTIONS
{captions}"""


def _match_batch(question: str, batch: list[tuple[int, str]], model: str) -> dict[int, str]:
    """Which of these captions the question is about, as {index: why}."""
    from customs.genai_client import generate_json

    listing = "\n".join(f"{n}. {caption}" for n, caption in batch)
    answer = generate_json(
        model, [_PROMPT.format(question=question, captions=listing)],
        _MATCH_SCHEMA)
    known = {n for n, _ in batch}
    out = {}
    for hit in (answer or {}).get("matches") or []:
        try:
            n = int(hit.get("n"))
        except (TypeError, ValueError):
            continue
        # A number the model invented is a frame nobody has, and a search
        # that answers with one is worse than a search that answers with
        # nothing.
        if n in known:
            out[n] = (hit.get("why") or "").strip()
    return out


def semantic_hits(question: str, captions: list[str], model: str = "") -> dict[int, str]:
    """Index -> reason, for every caption the question is about.

    Identical captions are sent once. A run pushed twice, or a film where
    the same product sits in nine shots, otherwise pays for the same
    sentence nine times and the model reads it nine times.
    """
    from concurrent.futures import ThreadPoolExecutor

    from customs.config import settings as live

    model = model or live.model_text
    unique: dict[str, list[int]] = {}
    for i, caption in enumerate(captions):
        if caption:
            unique.setdefault(caption, []).append(i)

    rows = [(n, caption) for n, caption in enumerate(unique)]
    batches = [rows[i:i + _BATCH] for i in range(0, len(rows), _BATCH)][:_MAX_BATCHES]
    if not batches:
        return {}

    with ThreadPoolExecutor(max_workers=len(batches)) as pool:
        results = list(pool.map(lambda b: _match_batch(question, b, model), batches))

    by_caption = {}
    for found in results:
        for n, why in found.items():
            by_caption[rows[n][1]] = why
    return {i: why for caption, why in by_caption.items()
            for i in unique[caption]}


def _body(row: dict) -> dict:
    """The parsed line, whatever the reader called it.

    loki_lines names it `parsed`; the MCP path names it `finding`. Falling
    back to parsing `line` here means a third name never costs an hour
    again: the symptom of getting this wrong is zero results and no error,
    because every caption silently reads as empty.
    """
    for key in ("parsed", "finding", "body"):
        value = row.get(key)
        if isinstance(value, dict):
            return value
    try:
        parsed = json.loads(row.get("line") or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def frames(ops, text: str = "", dimension: str = "", market: str = "",
           flagged: str = "", days: int = 30, limit: int = 400,
           mode: str = "semantic", model: str = "") -> dict:
    """Every frame the question is about, across every run.

    `ops` is an open GrafanaOps. Returns the hits plus the counts a
    question like "how many" is actually asking for, so the caller never
    has to count rows itself.

    mode="semantic" (the default) reads the captions with Gemini and lets
    it decide what the question is about. Nothing is pushed down to Loki,
    because there is no keyword to push: the labels narrow the candidates
    and the model does the judging.

    mode="literal" treats `text` as a regex over the caption, filtered
    server-side first. Right for a rule id, a brand, or a pattern somebody
    means exactly.
    """
    wanted = (text or "").strip()
    literal = mode == "literal"
    matcher = _compile(text) if literal else None
    # Semantics has no keyword to give Loki, so the stream selector does
    # the narrowing and the model reads what comes back.
    query = logql(text if literal else "", dimension, flagged)
    rows = ops.loki_lines(query, days=days, limit=limit)

    hits = []
    seen = set()
    for row in rows:
        body = _body(row)
        statement = (body.get("statement") or "").strip()
        # literal stage 2: the caption, not the line
        if matcher and not matcher.search(statement):
            continue
        if not statement:
            continue
        markets = body.get("markets") or []
        if market and market not in markets:
            continue
        run_id = body.get("run_id") or ""
        obs_id = body.get("observation_id") or ""
        # One frame, one card. A re-pushed run writes the same observation
        # again and Loki keeps both, which is how "wine" came back 400
        # times from one film.
        if (run_id, obs_id) in seen:
            continue
        seen.add((run_id, obs_id))
        hits.append({
            "run_id": run_id,
            "observation_id": obs_id,
            "asset": (row.get("labels") or {}).get("asset", ""),
            "statement": statement,
            "dimension": body.get("dimension") or "none",
            "t_start": float(body.get("t_start") or 0.0),
            "t_end": float(body.get("t_end") or 0.0),
            "shot_id": body.get("shot_id") or "",
            "markets": markets,
            "rules": body.get("rules") or [],
            "severity": int(body.get("max_severity") or 0),
            "confidence": body.get("confidence"),
            "frame": (f"/runs/{run_id}/evidence/{obs_id}"
                      if run_id and obs_id and body.get("has_frame") else ""),
        })

    reason = {}
    if wanted and not literal:
        try:
            reason = semantic_hits(wanted, [h["statement"] for h in hits], model)
        except Exception as exc:  # noqa: BLE001 -- fall back rather than fail
            # The model being unreachable should cost the reasoning, not the
            # search: the same words as a literal pattern still find the
            # obvious matches, and the caller is told which one answered.
            fallback = _compile(re.escape(wanted))
            hits = [h for h in hits if fallback.search(h["statement"])]
            mode = f"literal (semantic failed: {type(exc).__name__})"
            reason = {}
        else:
            hits = [dict(h, why=reason.get(i, "")) for i, h in enumerate(hits)
                    if i in reason]

    hits.sort(key=lambda h: (h["asset"], h["t_start"]))
    return {
        "query": query,
        "mode": mode,
        "pattern": (text or "").strip(),
        "total": len(hits),
        "scanned": len(rows),
        # Loki gave us exactly as many lines as we asked for, so there are
        # probably more. Saying so beats a count that quietly means "400".
        "capped": len(rows) >= limit,
        "films": len({h["asset"] for h in hits}),
        "by_asset": dict(Counter(h["asset"] for h in hits).most_common()),
        "by_dimension": dict(Counter(h["dimension"] for h in hits).most_common()),
        "by_market": dict(Counter(m for h in hits for m in h["markets"]).most_common()),
        "flagged": sum(1 for h in hits if h["markets"]),
        "hits": hits,
    }


def summary(result: dict) -> str:
    """One sentence a person or an agent can read without the rows."""
    if not result["total"]:
        return (f"No frame's caption matches {result['pattern']!r} in the last "
                f"30 days. The analyst writes what it sees in its own words, "
                f"so try a synonym, or a pattern like 'bunn|rabbit|hare'.")
    films = result["films"]
    flagged = result["flagged"]
    more = " at least " if result.get("capped") else " "
    return (f"{more.strip().capitalize() + ' ' if result.get('capped') else ''}"
            f"{result['total']} frame{'' if result['total'] == 1 else 's'} across "
            f"{films} film{'' if films == 1 else 's'} match "
            f"{result['pattern'] or 'that filter'!r}; {flagged} of them had at "
            f"least one market object.")
