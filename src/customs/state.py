"""One place that decides what a state looks like and when it applies.

The app paints tiles and pills; Grafana paints panels rendered from the
same numbers. If each kept its own copy of the palette and the thresholds
they would drift on the first change, and a market would be amber in one
place and red in the other. Both read this.
"""
from __future__ import annotations

# The brand's four, exact. Nothing else may name a hex.
SIGNAL = "#4285F4"     # blue    is the accent, and "needs a human"
BLOCKED = "#EA4335"    # red     is a market that will not take the film
AT_RISK = "#FBBC05"    # yellow  is cleared, with findings still open
CLEARED = "#34A853"    # green   is cleared and clean

# adjudicate.CLEARANCE_SEVERITY_THRESHOLD is the blocking line; these are
# the display bands, and they must not contradict it.
SEVERITY_BLOCKS = 70
SEVERITY_NOTES = 40


def colour_for_severity(severity: float) -> str:
    if severity >= SEVERITY_BLOCKS:
        return BLOCKED
    if severity >= SEVERITY_NOTES:
        return AT_RISK
    return CLEARED


STATE_COLOUR = {
    "cleared": CLEARED, "noted": AT_RISK, "at_risk": AT_RISK,
    "blocked": BLOCKED, "error": BLOCKED, "pending": SIGNAL,
    "human": SIGNAL, "offence": SIGNAL,
}


def grafana_thresholds() -> dict:
    """The same bands, as a Grafana fieldConfig thresholds block.

    Panel JSON built by the agent or by grafana_ops uses this, so a
    dashboard cannot disagree with the tile it sits next to.
    """
    return {
        "mode": "absolute",
        "steps": [
            {"color": CLEARED, "value": None},
            {"color": AT_RISK, "value": SEVERITY_NOTES},
            {"color": BLOCKED, "value": SEVERITY_BLOCKS},
        ],
    }


def grafana_state_mappings() -> list[dict]:
    """Verdict words -> exact hex, for state timeline and stat panels."""
    return [{"type": "value", "options": {
        word: {"color": colour, "index": i}
        for i, (word, colour) in enumerate(sorted(STATE_COLOUR.items()))
    }}]
