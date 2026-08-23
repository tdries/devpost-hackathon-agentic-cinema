"""Guard: the rule layer that decides whether a finding may be remediated.

Design spec section 7 / task-10-brief.md. This is the one place in Customs
that turns a market pack's own rule metadata into a decision about whether a
finding is safe to auto-remediate, so it is deliberately the most
restricted module in the codebase: it reads ONLY two things -- the pack rule
matched by a finding's rule_id, and the finding's own klass -- and nothing
else about the finding (not rationale, not severity, not any other
model-authored field) and nothing about the video itself. No function here
calls a model or imports genai_client; that is not a style preference, it
is the whole point -- a market pack's protected_basis flag is set once, by
a human, when the pack is authored, so a finding can never talk its way
past this layer no matter what its rationale says. tests/test_guard.py
enforces this both behaviorally (a finding whose rationale asks to be
unblocked still blocks) and structurally (a test parses this module's own
AST and asserts no code path ever accesses .rationale or calls/imports a
model client).
"""

from dataclasses import replace

from customs.packs import MarketPack
from customs.schema import Finding

# Exact contract string (task-10-brief.md). A finding blocked under Rule 1
# always carries this reason, verbatim, so a human reviewing it sees why at
# a glance rather than having to go look up a rule id.
BLOCKED_REASON = "rule basis targets a protected characteristic; human decision required"

def apply(findings: list[Finding], pack: MarketPack) -> list[Finding]:
    """Apply the guard rule layer to one market's findings.

    Rule 1: a finding whose matched rule (looked up by rule_id in
    pack.rules) has protected_basis=True gets remediation_blocked=True and
    blocked_reason=BLOCKED_REASON. status is left exactly as it was (judge()
    always sets "open"; this function never touches status) -- blocking
    remediation is not the same as closing or downgrading the finding
    itself, it only takes auto-remediation off the table pending a human.

    Rule 2: a finding with klass == "offence" gets remediable=False,
    always, regardless of what the matched rule's own `remediable` field
    says (an offence is never something this tool auto-fixes, whatever the
    pack claims about remediability).

    Rule 3: everything else -- including a finding whose rule_id names a
    rule not present in pack.rules at all -- passes through untouched. A
    finding/pack mismatch should not happen for anything judge() actually
    produces (adjudicate.candidates only ever pairs an observation with a
    rule that is really in the pack), but this function degrades safely
    rather than raising if it ever sees one: a stale finding replayed
    against a newer pack revision is a data-integrity question for whoever
    is doing that replay, not something this pure function should crash
    over.

    Rules 1 and 2 are independent and both apply if both conditions hold
    (an offence-class finding matched to a protected_basis rule gets both
    remediable=False and remediation_blocked=True).

    Returns brand new Finding objects (dataclasses.replace); this never
    mutates a finding already in the input list in place, and never
    mutates the input list itself. A caller that keeps its own reference to
    the original findings list keeps seeing the pre-guard values on every
    object in it -- this is the "rebuild" side of the contract's mutation
    choice, taken over in-place mutation so a finding's guard state can
    never be observed half-applied by anything still holding the old
    reference.
    """
    rules_by_id = {rule.id: rule for rule in pack.rules}

    guarded = []
    for finding in findings:
        rule = rules_by_id.get(finding.rule_id)
        updates = {}

        if rule is not None and rule.protected_basis:
            updates["remediation_blocked"] = True
            updates["blocked_reason"] = BLOCKED_REASON

        if finding.klass == "offence":
            updates["remediable"] = False

        guarded.append(replace(finding, **updates))

    return guarded
