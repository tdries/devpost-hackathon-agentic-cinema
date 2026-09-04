"""Find frames by what the analyst said about them, across every run.

The captions are already in Loki: one `kind="observation"` line per
keyframe, carrying the analyst's sentence in its body. That makes "which
frames have a rabbit in them" a query rather than a feature, and it makes
it a query ACROSS runs, because Loki holds every run this instance ever
performed and labels each line with its asset.

Two-stage on purpose:

  1. Loki's own line filter narrows the stream server-side. It matches the
     raw JSON line, so a hit can come from a rule id or a market code that
     happens to contain the word.
  2. The same pattern is re-applied here against the parsed `statement`
     field alone. That is the caption, and only the caption, so
     "how many frames show a short skirt" cannot be inflated by a rule
     named SHORT-01.

Stage 1 without stage 2 would be fast and wrong. Stage 2 without stage 1
would pull a month of lines to filter locally.
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
           flagged: str = "", days: int = 30, limit: int = 400) -> dict:
    """Every frame whose caption matches, newest first, across all runs.

    `ops` is an open GrafanaOps. Returns the hits plus the counts a
    question like "how many" is actually asking for, so the caller never
    has to count rows itself.
    """
    matcher = _compile(text)
    query = logql(text, dimension, flagged)
    rows = ops.loki_lines(query, days=days, limit=limit)

    hits = []
    seen = set()
    for row in rows:
        body = _body(row)
        statement = (body.get("statement") or "").strip()
        # stage 2: the caption, not the line
        if matcher and not matcher.search(statement):
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

    hits.sort(key=lambda h: (h["asset"], h["t_start"]))
    return {
        "query": query,
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
