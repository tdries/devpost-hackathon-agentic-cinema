"""Findings as markers, for the suite the fix actually happens in.

The console can fix a span itself, and for a lot of findings that is the
right answer. For the rest, the person who fixes it is an editor with the
master open in Resolve or Premiere, and what they need is not a web page:
it is the timecodes, on the timeline, in the right colour, with the statute
in the note so they can read why while they work.

Two formats, because the two suites disagree about which one they trust:

* CSV, which Resolve reads through its own marker import and Premiere
  through the Markers panel. Columns are the ones both look for.
* CMX3600 EDL, the oldest interchange there is, and the one Resolve
  imports most reliably. One event per finding, its rule id as the reel
  comment, so a marker lands on the timeline with the note attached.

Timecode is HH:MM:SS:FF at the asset's own rate, which is why this module
needs the frame rate and will not guess it silently: the rate it used is
written into both files.
"""
from __future__ import annotations

import csv
import io

# Colour by what the finding does, not by how it feels. The three the
# console uses, in the words each suite names them: a blocking finding is
# red, a policy or offence finding that only puts a market at risk is
# yellow, and anything already fixed and verified is green.
BLOCKING_SEVERITY = 70


def _colour(finding) -> str:
    if finding.status == "resolved":
        return "Green"
    if finding.klass == "legal" and finding.severity >= BLOCKING_SEVERITY:
        return "Red"
    return "Yellow"


def timecode(seconds: float, fps: float) -> str:
    """HH:MM:SS:FF, non-drop, at this asset's rate.

    Rounded to the nearest frame and clamped at zero: a finding that starts
    at 0.04s is on frame 1, not on frame -0.
    """
    fps = fps if fps and fps > 0 else 25.0
    total = max(0, round(max(0.0, float(seconds)) * fps))
    frames = int(total % round(fps))
    whole = int(total // round(fps))
    return (f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:"
            f"{whole % 60:02d}:{frames:02d}")


def _note(finding) -> str:
    """One line an editor can act on: what fired, and on what basis."""
    parts = [f"{finding.rule_id} ({finding.market})",
             f"severity {finding.severity}"]
    if finding.remediation_blocked or not finding.remediable:
        parts.append("HUMAN DECISION REQUIRED")
    if finding.rationale:
        parts.append(finding.rationale)
    if finding.citation_ref:
        parts.append(f"Basis: {finding.citation_ref}")
    return " | ".join(parts)


def as_csv(findings: list, fps: float, *, asset: str = "") -> str:
    """The marker list both suites read, newest column names.

    Sorted by timecode rather than by severity: this is a timeline, and an
    editor reads it in the order the film plays.
    """
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["Name", "Start", "End", "Duration", "Color", "Notes"])
    for finding in sorted(findings, key=lambda f: (f.t_start, f.rule_id)):
        start = timecode(finding.t_start, fps)
        end = timecode(finding.t_end, fps)
        span = timecode(max(0.0, finding.t_end - finding.t_start), fps)
        writer.writerow([finding.rule_id, start, end, span,
                         _colour(finding), _note(finding)])
    return out.getvalue()


def as_edl(findings: list, fps: float, *, title: str = "CUSTOMS") -> str:
    """CMX3600, one event per finding, the note as a comment.

    The oldest interchange format there is and the one Resolve imports most
    reliably. Record timecode equals source timecode because these markers
    describe the master itself, not a cut of it.
    """
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME"]
    for n, finding in enumerate(
            sorted(findings, key=lambda f: (f.t_start, f.rule_id)), start=1):
        start = timecode(finding.t_start, fps)
        end = timecode(finding.t_end, fps)
        lines.append(f"{n:03d}  AX       V     C        "
                     f"{start} {end} {start} {end}")
        lines.append(f"* FROM CLIP NAME: {finding.rule_id}")
        lines.append(f"* COMMENT: {_note(finding)}")
    return "\r\n".join(lines) + "\r\n"
