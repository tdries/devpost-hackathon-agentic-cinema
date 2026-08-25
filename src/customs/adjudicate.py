import json

from customs.config import settings
from customs.genai_client import generate_grounded, generate_json
from customs.packs import MarketPack, MarketRule
from customs.schema import Finding, Observation, Verdict

# Bounds the judge model may adjust a rule's base severity by. A candidate can
# only be judged less severe than the rule's default, never more; enforced in
# code (task-8-brief.md), not trusted from the model's raw output.
SEVERITY_ADJUST_MIN = -20
SEVERITY_ADJUST_MAX = 0

# A finding whose citation could not be grounded to a live source is capped at
# this severity and can never block or elevate clearance (design spec section 8).
UNSOURCED_SEVERITY_CAP = 40

# clearance() threshold: a sourced legal/policy finding at or above this
# severity blocks (legal) or flags at_risk (policy). Design spec section 8.
CLEARANCE_SEVERITY_THRESHOLD = 70

# Verbatim prompt text, single source of truth (task-8-brief.md Step 3).
# {market_name} is interpolated at call time via PROMPT.format(market_name=pack.name);
# the rest of the text is exact.
PROMPT = """You are the advertising clearance officer for {market_name}. For each
candidate pairing of an observed fact with a rule of this market, decide
whether the fact actually triggers the rule. Be strict about the rule's
wording: a wine glass triggers an alcohol-advertising ban; a water glass
does not. rationale: two sentences max, cite the rule basis text.
scope: how structural the violation is. "frame" if it is a detail visible in a
moment, "segment" if it is part of the action inside the shot, "scene" if the
whole shot is the violation, "concept" if the premise of the commercial is
what the rule forbids.
remedies: exactly three concrete, different ways to deal with THIS violation,
each describing the actual thing you were shown rather than its category: a
lit cigarette is not "the drink", a headline is not "the text". label is a
short imperative for a button, six words or fewer. directive is one sentence
telling an image editor what to change in the frame, naming the object and
what replaces it, and never asking for words to be written on screen unless
the violation is itself on-screen text.
substitutable: true if replacing the offending element with a permitted one
would satisfy the rule and leave the commercial's idea standing (a child in a
slingshot gag becomes an adult, and the gag survives), false if the forbidden
thing IS the idea (an advertisement for alcohol cannot be an advertisement for
alcohol with the alcohol removed)."""

# Verbatim citation-verification prompt (task-8-brief.md Step 3), formatted
# per triggered finding via .format(basis=rule.basis, trigger=rule.trigger,
# market_name=pack.name).
CITATION_PROMPT = (
    "Confirm the current status of {basis} as it applies to {trigger} in "
    "{market_name} television advertising. Answer in one sentence and only "
    "from sources you can cite."
)

_JUDGE_RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "observation_id": {"type": "string"},
            "rule_id": {"type": "string"},
            "triggers": {"type": "boolean"},
            "severity_adjust": {
                "type": "integer",
                "minimum": SEVERITY_ADJUST_MIN,
                "maximum": SEVERITY_ADJUST_MAX,
            },
            "rationale": {"type": "string"},
            "scope": {"type": "string",
                      "enum": ["frame", "segment", "scene", "concept"]},
            "substitutable": {"type": "boolean"},
            "remedies": {
                "type": "array", "minItems": 3, "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"},
                                   "directive": {"type": "string"}},
                    "required": ["label", "directive"],
                },
            },
        },
        "required": ["observation_id", "rule_id", "triggers", "severity_adjust",
                     "rationale", "scope", "substitutable", "remedies"],
    },
}

def _emit(on_event, message: str) -> None:
    if on_event is not None:
        on_event("adjudicator", message)

def candidates(observations: list[Observation], pack: MarketPack) -> list[tuple[Observation, MarketRule]]:
    """Pure dimension-equality join: every (observation, rule) pair sharing a dimension.

    One observation can pair with more than one rule of the same market (a
    pack can carry several rules over one dimension, e.g. separate alcohol
    and tobacco rules both keyed to alcohol_tobacco_drugs); every such pair is
    returned, not just the first match. Order follows observations, then the
    pack's own rule order, so results are deterministic.
    """
    return [
        (obs, rule)
        for obs in observations
        for rule in pack.rules
        if rule.dimension == obs.dimension
    ]

def _remedies(raw) -> list[dict]:
    """The judge's three remedies, or nothing rather than something malformed.

    Same posture as every other field here: a bad shape costs the remedies,
    not the finding. costs.suggestions() falls back to its dimension table
    when this is empty, so a finding without them is still actionable.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        directive = str(item.get("directive") or "").strip()
        if label and directive:
            out.append({"label": label, "directive": directive})
    return out[:3]

def _clamp_severity_adjust(value: int) -> int:
    """Clamp an already-numeric severity_adjust into [SEVERITY_ADJUST_MIN, SEVERITY_ADJUST_MAX].

    Raises TypeError or ValueError if value cannot be coerced to int at all
    (a list, dict, or non-numeric string). judge() catches that and drops
    the item with a warning instead of letting one malformed item crash the
    whole batch (the same invariant analyst.py and the rest of this module
    hold for every other field). Callers must coalesce a JSON null to 0
    themselves before calling this; 0 here means "no adjustment", never
    "invalid".
    """
    return max(SEVERITY_ADJUST_MIN, min(SEVERITY_ADJUST_MAX, int(value)))

def judge(run_id: str, observations: list[Observation], pack: MarketPack,
          on_event=None, on_verdict=None) -> list[Finding]:
    """Judge every dimension-matched candidate for one market, cite each trigger.

    One batched generate_json call decides, for every candidate pairing in
    this market, whether the observed fact actually triggers the rule
    (triggers, severity_adjust, rationale). A malformed response never
    crashes the pass, it only shrinks it, mirroring analyst.observe_shot: a
    non-list response drops the whole batch with one warning event; a
    non-dict item, a pair that is not one of this market's real candidates,
    an empty rationale (including an explicit JSON null), or a severity_adjust
    that cannot be coerced to a number at all (a list, dict, or non-numeric
    string) drops just that item with its own warning event, before any
    citation call is spent on it. A candidate the model marks as not
    triggering is simply skipped, no warning: that is a legitimate negative
    verdict, not malformed data, and never spends a citation call.

    Every surviving triggered item gets exactly one generate_grounded call to
    verify its citation. severity, sourced, the unsourced severity cap,
    citation_ref/citation_url, remediable, remediation_blocked, status and
    the finding id are all decided here in code, never by the model.
    """
    cands = candidates(observations, pack)
    _emit(on_event, f"judge -> {pack.market} ({len(cands)} candidates)")
    if not cands:
        return []

    obs_by_id = {obs.id: obs for obs, _ in cands}
    rule_by_id = {rule.id: rule for _, rule in cands}
    valid_pairs = {(obs.id, rule.id) for obs, rule in cands}

    # Seeded from the candidates, not from the response. A pairing the
    # model silently omits would otherwise disappear, and an omission
    # counted as clearance is exactly the error that would corrupt the
    # question this record exists to answer.
    verdicts: dict[tuple[str, str], Verdict] = {
        (obs.id, rule.id): Verdict(
            id=f"vrd_{pack.market}_{rule.id}_{obs.id}", run_id=run_id,
            observation_id=obs.id, market=pack.market, rule_id=rule.id,
            dimension=rule.dimension, klass=rule.klass, verdict="unreturned",
            t_start=obs.t_start, t_end=obs.t_end)
        for obs, rule in cands
    }

    payload = [
        {
            "observation_id": obs.id,
            "statement": obs.statement,
            "dimension": obs.dimension,
            "rule_id": rule.id,
            "rule_trigger": rule.trigger,
            "rule_basis": rule.basis,
        }
        for obs, rule in cands
    ]
    parts = [
        PROMPT.format(market_name=pack.name),
        f"Candidates (JSON array, each item one observation-rule pairing to judge):\n{json.dumps(payload)}",
    ]

    raw = generate_json(settings.model_text, parts, _JUDGE_RESPONSE_SCHEMA)

    if not isinstance(raw, list):
        _emit(
            on_event,
            f"warning: dropped whole judge response for {pack.market}, "
            f"expected a list, got {type(raw).__name__}",
        )
        return []

    findings = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            _emit(
                on_event,
                f"warning: dropped judge item {i} for {pack.market}, "
                f"expected an object, got {type(item).__name__}",
            )
            continue

        observation_id = item.get("observation_id")
        rule_id = item.get("rule_id")
        pair = (observation_id, rule_id)
        # isinstance guards first: a composite value (list/dict) instead of
        # the declared string type must never reach the `in valid_pairs` set
        # lookup, since a tuple containing an unhashable element crashes
        # there instead of just failing the membership test.
        is_known_pair = (
            isinstance(observation_id, str)
            and isinstance(rule_id, str)
            and pair in valid_pairs
        )
        if not is_known_pair:
            _emit(
                on_event,
                f"warning: dropped judge item {i} for {pack.market}, "
                f"pair {pair!r} is not one of this market's candidates",
            )
            continue

        # Recorded before the trigger test, and before the one call that
        # costs money: generate_grounded runs only for a finding, so an
        # acquittal is free to keep.
        # A severity_adjust that will not coerce costs the FINDING (the
        # existing guard below drops it with a warning) but must not cost
        # the verdict: "France said no to this" is still true and still
        # worth recording whatever nonsense came back in another field.
        try:
            _verdict_adjust = _clamp_severity_adjust(item.get("severity_adjust") or 0)
        except (TypeError, ValueError):
            _verdict_adjust = 0
        _verdict_rationale = item.get("rationale")
        verdicts[pair] = Verdict(
            id=f"vrd_{pack.market}_{pair[1]}_{pair[0]}", run_id=run_id,
            observation_id=pair[0], market=pack.market, rule_id=pair[1],
            dimension=rule_by_id[pair[1]].dimension,
            klass=rule_by_id[pair[1]].klass,
            verdict="triggered" if item.get("triggers") else "cleared",
            severity_adjust=_verdict_adjust,
            rationale=_verdict_rationale if isinstance(_verdict_rationale, str) else "",
            t_start=obs_by_id[pair[0]].t_start, t_end=obs_by_id[pair[0]].t_end)

        if not item.get("triggers"):
            continue

        # JSON null survives the response_schema's declared "string" type, so
        # an explicit null must be coalesced by hand (analyst.py precedent).
        scope_named = item.get("scope") if isinstance(item.get("scope"), str) else ""
        rationale = item.get("rationale")
        if not isinstance(rationale, str):
            rationale = ""
        if not rationale:
            _emit(
                on_event,
                f"warning: dropped judge item {i} for {pack.market}, "
                f"empty rationale for {pair[1]}",
            )
            continue

        severity_adjust_raw = item.get("severity_adjust")
        obs = obs_by_id[pair[0]]
        rule = rule_by_id[pair[1]]
        try:
            severity_adjust = _clamp_severity_adjust(
                0 if severity_adjust_raw is None else severity_adjust_raw
            )
        except (TypeError, ValueError):
            _emit(
                on_event,
                f"warning: dropped judge item {i} for {pack.market}, "
                f"non-numeric severity_adjust {severity_adjust_raw!r} for {rule.id}",
            )
            continue

        _emit(on_event, f"citation check -> {pack.market} {rule.id} for {obs.id}")
        _, chunks = generate_grounded(
            settings.model_text,
            CITATION_PROMPT.format(basis=rule.basis, trigger=rule.trigger, market_name=pack.name),
        )

        severity = max(0, rule.severity + severity_adjust)
        sourced = len(chunks) > 0
        if not sourced:
            severity = min(severity, UNSOURCED_SEVERITY_CAP)

        findings.append(Finding(
            id=f"fnd_{pack.market}_{rule.id}_{obs.id}",
            run_id=run_id,
            observation_id=obs.id,
            market=pack.market,
            rule_id=rule.id,
            klass=rule.klass,
            severity=severity,
            t_start=obs.t_start,
            t_end=obs.t_end,
            rationale=rationale,
            citation_ref=rule.basis,
            citation_url=chunks[0]["uri"] if sourced else "",
            sourced=sourced,
            remediable=rule.remediable,
            remediation_blocked=False,
            blocked_reason="",
            status="open",
            scope=scope_named,
            substitutable=bool(item.get("substitutable", True)),
            remedies=_remedies(item.get("remedies")),
        ))

    if on_verdict is not None:
        on_verdict(list(verdicts.values()))
    return findings

def clearance(findings: list[Finding]) -> str:
    """Derive market clearance status from findings. Code, not the model.

    Only findings with status "open" are considered (task-14 ruling). A
    finding the Remediator is working on ("remediating") or the Verifier has
    confirmed fixed ("resolved") is no longer holding the market: that is
    exactly what makes the alert clear after remediation, and it is what
    telemetry.push_status's customs_blocking filter has always done -- before
    this, clearance() was status-blind while the metric next to it was not,
    so a remediated market went on reporting "blocked" forever.

    Among open findings:

    blocked: any sourced legal finding at severity >= CLEARANCE_SEVERITY_THRESHOLD.
    at_risk: any sourced policy finding at severity >= CLEARANCE_SEVERITY_THRESHOLD
             (checked only once blocked is ruled out).
    cleared: otherwise.

    offence findings never block or elevate clearance, regardless of
    severity: only legal and policy are checked. Unsourced findings never
    block or elevate either; `sourced` is checked directly here rather than
    trusted from an already-capped severity, so this holds even if a finding
    somehow reaches clearance() without the construction-time cap applied.

    "cleared" therefore means "nothing open is holding this market", not
    "this market never had a finding": the findings themselves stay in the
    store and on the dashboards with their resolved status, and a market
    whose stage errored is a separate question its caller answers with
    pipeline.errored_markets, never with this function.
    """
    open_findings = [f for f in findings if f.status == "open"]
    if any(f.klass == "legal" and f.sourced and f.severity >= CLEARANCE_SEVERITY_THRESHOLD for f in open_findings):
        return "blocked"
    if any(f.klass == "policy" and f.sourced and f.severity >= CLEARANCE_SEVERITY_THRESHOLD for f in open_findings):
        return "at_risk"
    return "cleared"
