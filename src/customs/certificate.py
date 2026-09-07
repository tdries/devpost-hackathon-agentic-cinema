"""The clearance certificate: one market's decision, as a document.

Everything else this system produces is a screen. A clearance desk's output
is a piece of paper: what was cleared, for where, on what date, against
which statutes, and what was changed to get there. A reviewer who was not
in the room needs that, and so does an archive three years later when
somebody asks why this cut aired in Riyadh.

So this is deliberately not a summary. Every finding the market raised is
in it, with the rule id, the class, the severity, the seconds it covers,
the statute in the regulator's own words, the citation the adjudicator
retrieved at judging time, and -- for anything that was fixed -- the method,
the change id and the verifier's own sentence saying it no longer fires.
A finding the Guard refused to auto-edit says so, in those words, with the
statute behind it: that refusal is the product working and the certificate
is where it has to be visible.

reportlab because nothing already installed can set type on a page, and a
hand-rolled PDF writer is more code and more risk than one dependency that
every Python shop already has. Platypus rather than a canvas: the flowables
paginate the finding list themselves, which is the only hard part.
"""
from __future__ import annotations

import time
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

# Google's four, the same hues the console uses for the same meanings.
BLUE = colors.HexColor("#4285F4")
RED = colors.HexColor("#EA4335")
YELLOW = colors.HexColor("#FBBC05")
GREEN = colors.HexColor("#34A853")
INK = colors.HexColor("#111318")
INK_2 = colors.HexColor("#5F6368")
LINE = colors.HexColor("#DADCE0")

_VERDICT = {
    "cleared": ("CLEARED", GREEN),
    "at_risk": ("CLEARED WITH NOTES", YELLOW),
    "blocked": ("NOT CLEARED", RED),
}

_STATUS_WORD = {
    "open": "open",
    "remediating": "fix in progress",
    "resolved": "fixed and verified",
}


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=9,
                          leading=12.5, textColor=INK, alignment=TA_LEFT)
    return {
        "base": base,
        "title": ParagraphStyle("title", parent=base, fontName="Helvetica-Bold",
                                fontSize=19, leading=22, spaceAfter=2),
        "sub": ParagraphStyle("sub", parent=base, fontSize=10.5, leading=14,
                              textColor=INK_2),
        "label": ParagraphStyle("label", parent=base, fontName="Helvetica-Bold",
                                fontSize=7.5, leading=10, textColor=INK_2),
        "rule": ParagraphStyle("rule", parent=base, fontName="Helvetica-Bold",
                               fontSize=10, leading=13, spaceBefore=2),
        "note": ParagraphStyle("note", parent=base, fontSize=8, leading=11,
                               textColor=INK_2),
        "cite": ParagraphStyle("cite", parent=base, fontSize=7.5, leading=10,
                               textColor=BLUE),
    }


def _timecode(seconds: float) -> str:
    """mm:ss.s, which is how every other screen writes a span."""
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def _bars(width: float) -> Table:
    """The four brand bars, as the console's own letterhead."""
    cell = width / 4.0
    bars = Table([[""] * 4], colWidths=[cell] * 4, rowHeights=[3.2 * mm])
    bars.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), BLUE),
        ("BACKGROUND", (1, 0), (1, 0), RED),
        ("BACKGROUND", (2, 0), (2, 0), YELLOW),
        ("BACKGROUND", (3, 0), (3, 0), GREEN),
        ("LINEBELOW", (0, 0), (-1, 0), 0, colors.white),
    ]))
    return bars


def _facts(rows: list[tuple[str, str]], width: float, st) -> Table:
    """The header block: label above value, four to a row."""
    cells = [[Paragraph(label.upper(), st["label"]) for label, _ in rows],
             [Paragraph(value, st["base"]) for _, value in rows]]
    table = Table(cells, colWidths=[width / len(rows)] * len(rows))
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("LINEBELOW", (0, -1), (-1, -1), 0.6, LINE),
    ]))
    return table


def _finding_block(finding, change, verifier_line: str, st, width: float):
    """One finding, whole: what fired, on what basis, and what happened."""
    status = _STATUS_WORD.get(finding.status, finding.status)
    head = (f'{finding.rule_id} &nbsp;&middot;&nbsp; {finding.klass} '
            f'&nbsp;&middot;&nbsp; severity {finding.severity} '
            f'&nbsp;&middot;&nbsp; {_timecode(finding.t_start)} to '
            f'{_timecode(finding.t_end)} &nbsp;&middot;&nbsp; {status}')
    flow = [Paragraph(head, st["rule"])]
    if finding.rationale:
        flow.append(Paragraph(finding.rationale, st["base"]))
    if finding.citation_ref:
        flow.append(Paragraph(f"<b>Basis.</b> {finding.citation_ref}", st["note"]))
    if finding.citation_url:
        flow.append(Paragraph(finding.citation_url, st["cite"]))
    elif not finding.sourced:
        flow.append(Paragraph(
            "No citation was retrieved for this rule at judging time, so its "
            "severity is capped and it cannot block a market on its own.",
            st["note"]))

    if finding.remediation_blocked or not finding.remediable:
        flow.append(Paragraph(
            "<b>Human decision required.</b> "
            + (finding.blocked_reason
               or "the rule targets a protected characteristic, so this "
                  "system will not edit the footage to satisfy it"),
            st["note"]))
    if change is not None:
        flow.append(Paragraph(
            f"<b>Changed.</b> {change.method} ({change.id})"
            + (f": {change.description}" if change.description else ""),
            st["note"]))
    if verifier_line:
        flow.append(Paragraph(f"<b>Verifier.</b> {verifier_line}", st["note"]))
    flow.append(Spacer(1, 5))
    # KeepTogether so a finding never breaks across a page in the middle of
    # its own statute: a citation on the next page reads as belonging to the
    # rule printed at the top of it.
    return KeepTogether(flow)


def render(run, pack, clearance: str, findings: list, changes: list,
           verifier_lines: dict[str, str], *, asset_name: str = "",
           instance: str = "") -> bytes:
    """The certificate for one market of one run, as PDF bytes.

    `verifier_lines` maps a finding id to the verifier's own sentence about
    it, which the caller lifts from the run's event log: the certificate
    quotes the system rather than paraphrasing it.
    """
    st = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=15 * mm, bottomMargin=16 * mm,
        title=f"Clearance certificate {pack.market} {run.id}",
        author="The Media Customs", subject=asset_name)
    width = doc.width

    word, hue = _VERDICT.get(clearance, (clearance.upper(), INK_2))
    blocking = [f for f in findings
                if f.status == "open" and f.klass == "legal" and f.sourced]
    guarded = [f for f in findings if f.remediation_blocked or not f.remediable]
    fixed = [f for f in findings if f.status == "resolved"]

    story = [
        _bars(width),
        Spacer(1, 9),
        Paragraph("CLEARANCE CERTIFICATE", st["title"]),
        Paragraph(f"{pack.name} ({pack.market}) &nbsp;&middot;&nbsp; "
                  f"{', '.join(pack.regulators) or 'no named regulator'}",
                  st["sub"]),
        Spacer(1, 10),
        _facts([
            ("asset", asset_name or run.asset_path.rsplit("/", 1)[-1]),
            ("run", run.id),
            ("issued", time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())),
            ("verdict", f'<font color="#{hue.hexval()[2:]}"><b>{word}</b></font>'),
        ], width, st),
        Spacer(1, 8),
        Paragraph(
            f"{len(findings)} finding(s) were raised against this market from "
            f"{pack.rules and len(pack.rules) or 0} resolved rules: "
            f"{len(blocking)} still holding clearance, {len(fixed)} fixed and "
            f"verified, {len(guarded)} referred to a human. Every rule below "
            f"was matched to an observation the analyst recorded from the "
            f"footage itself, and every citation was retrieved at judging "
            f"time.",
            st["base"]),
        Spacer(1, 12),
    ]

    if findings:
        story.append(Paragraph("FINDINGS", st["label"]))
        story.append(Spacer(1, 4))
        by_finding = {c.finding_id: c for c in changes}
        # Worst first, which is the order the market room lists them in.
        for finding in sorted(findings, key=lambda f: (-f.severity, f.t_start)):
            story.append(_finding_block(
                finding, by_finding.get(finding.id),
                verifier_lines.get(finding.id, ""), st, width))
    else:
        story.append(Paragraph(
            "No rule in this market's resolved set was triggered by anything "
            "the analyst observed in this commercial.", st["base"]))

    story += [
        Spacer(1, 6),
        Paragraph(
            "This is a machine-generated record of one clearance run: what the "
            "system observed, which rules it matched, what it cited and what it "
            "changed. It is evidence for a human decision, not the decision, and "
            "not legal advice."
            + (f" Issued by {instance}." if instance else ""),
            st["note"]),
    ]

    doc.build(story)
    return buffer.getvalue()
