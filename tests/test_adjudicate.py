from pathlib import Path

import pytest

from customs import adjudicate, analyst
from customs.packs import MarketPack, MarketRule
from customs.packs import load as load_packs
from customs.schema import Finding, Observation

TEST_AD = Path(__file__).resolve().parents[1] / "docs" / "samples" / "test_ad.mp4"
MARKETS_DIR = Path("markets")

# --- helpers ---

def _obs(id="obs_1", dimension="alcohol_tobacco_drugs", statement="A wine glass is visible.",
         t_start=0.0, t_end=1.0, confidence=0.9) -> Observation:
    return Observation(id=id, shot_id="shot_0", t_start=t_start, t_end=t_end, dimension=dimension,
                        statement=statement, evidence_frame="f.jpg", confidence=confidence)

def _rule(id="ZZ-ALC-01", dimension="alcohol_tobacco_drugs", klass="legal", severity=90,
          trigger="t", basis="b", remediable=True) -> MarketRule:
    return MarketRule(id=id, dimension=dimension, klass=klass, severity=severity,
                       trigger=trigger, basis=basis, remediable=remediable)

def _pack(rules, market="ZZ", name="Zeta") -> MarketPack:
    return MarketPack(market=market, name=name, regulators=[], pre_clearance="none", rules=rules)

def _finding(**overrides) -> Finding:
    fields = dict(
        id="fnd_1", run_id="run_1", observation_id="obs_1", market="ZZ",
        rule_id="ZZ-01", klass="legal", severity=50, t_start=0.0, t_end=1.0,
        rationale="r", citation_ref="basis", citation_url="https://example.com",
        sourced=True, remediable=True, remediation_blocked=False,
        blocked_reason="", status="open",
    )
    fields.update(overrides)
    return Finding(**fields)

# --- Step 1/2: candidates, pure dimension-equality join ---

def test_candidates_joins_only_matching_dimensions():
    obs_alcohol = _obs(id="obs_1", dimension="alcohol_tobacco_drugs")
    obs_text = _obs(id="obs_2", dimension="text_legibility", statement="English text.")
    rule_alcohol = _rule(id="ZZ-ALC-01", dimension="alcohol_tobacco_drugs")
    rule_gambling = _rule(id="ZZ-GAM-01", dimension="gambling_and_finance")
    pack = _pack([rule_alcohol, rule_gambling])

    cands = adjudicate.candidates([obs_alcohol, obs_text], pack)

    assert cands == [(obs_alcohol, rule_alcohol)]

def test_candidates_pairs_one_observation_with_every_matching_rule():
    obs = _obs(dimension="alcohol_tobacco_drugs")
    rule_a = _rule(id="ZZ-ALC-01", dimension="alcohol_tobacco_drugs", klass="legal")
    rule_b = _rule(id="ZZ-ALC-02", dimension="alcohol_tobacco_drugs", klass="policy")
    pack = _pack([rule_a, rule_b])

    cands = adjudicate.candidates([obs], pack)

    assert cands == [(obs, rule_a), (obs, rule_b)]

def test_candidates_empty_when_nothing_matches():
    obs = _obs(dimension="text_legibility")
    rule = _rule(dimension="alcohol_tobacco_drugs")
    pack = _pack([rule])

    assert adjudicate.candidates([obs], pack) == []

# --- Step 1/2: clearance, pure derivation from findings ---

def test_clearance_blocked_for_legal_sourced_severity_95():
    findings = [_finding(klass="legal", severity=95, sourced=True)]
    assert adjudicate.clearance(findings) == "blocked"

def test_clearance_at_risk_for_policy_sourced_severity_75():
    findings = [_finding(klass="policy", severity=75, sourced=True)]
    assert adjudicate.clearance(findings) == "at_risk"

def test_clearance_cleared_for_offence_sourced_severity_90_never_blocks():
    findings = [_finding(klass="offence", severity=90, sourced=True)]
    assert adjudicate.clearance(findings) == "cleared"

def test_clearance_cleared_when_only_legal_finding_is_unsourced_and_capped():
    # realistic post-construction shape: sourced=false already capped severity to 40
    findings = [_finding(klass="legal", severity=40, sourced=False)]
    assert adjudicate.clearance(findings) == "cleared"

def test_clearance_unsourced_never_blocks_even_at_high_severity():
    # defense in depth: clearance() must check `sourced` itself rather than
    # trust that severity was already capped upstream.
    findings = [_finding(klass="legal", severity=95, sourced=False)]
    assert adjudicate.clearance(findings) == "cleared"

def test_clearance_blocked_takes_precedence_over_at_risk():
    findings = [
        _finding(id="f1", klass="policy", severity=80, sourced=True),
        _finding(id="f2", klass="legal", severity=95, sourced=True),
    ]
    assert adjudicate.clearance(findings) == "blocked"

def test_clearance_cleared_with_no_findings():
    assert adjudicate.clearance([]) == "cleared"

# --- Step 4: judge, mocked model boundary ---

def test_judge_returns_empty_and_skips_model_calls_when_no_candidates(monkeypatch):
    obs = _obs(dimension="text_legibility")
    rule = _rule(dimension="alcohol_tobacco_drugs")
    pack = _pack([rule])

    def fail(*a, **k):
        raise AssertionError("must not call the model when there are no candidates")
    monkeypatch.setattr(adjudicate, "generate_json", fail)
    monkeypatch.setattr(adjudicate, "generate_grounded", fail)

    assert adjudicate.judge("run_1", [obs], pack) == []

def test_judge_emits_start_event_with_candidate_count(monkeypatch):
    obs = _obs()
    rule = _rule()
    pack = _pack([rule])
    monkeypatch.setattr(adjudicate, "generate_json", lambda model, parts, schema: [])

    events = []
    adjudicate.judge("run_1", [obs], pack, on_event=lambda a, m: events.append((a, m)))

    assert any(a == "adjudicator" and pack.market in m and "1" in m for a, m in events), events

def test_judge_drops_whole_response_when_not_a_list(monkeypatch):
    obs = _obs()
    rule = _rule()
    pack = _pack([rule])
    monkeypatch.setattr(adjudicate, "generate_json", lambda model, parts, schema: {"not": "a list"})
    monkeypatch.setattr(
        adjudicate, "generate_grounded",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    events = []
    findings = adjudicate.judge("run_1", [obs], pack, on_event=lambda a, m: events.append((a, m)))

    assert findings == []
    warnings = [m for a, m in events if "warning" in m.lower()]
    assert len(warnings) == 1, f"expected exactly one warning for the whole response, got: {events}"

def test_judge_drops_items_that_are_not_dicts(monkeypatch):
    obs = _obs()
    rule = _rule()
    pack = _pack([rule])
    monkeypatch.setattr(adjudicate, "generate_json", lambda model, parts, schema: ["nope", 42])

    events = []
    findings = adjudicate.judge("run_1", [obs], pack, on_event=lambda a, m: events.append((a, m)))

    assert findings == []
    warnings = [m for a, m in events if "warning" in m.lower()]
    assert len(warnings) == 2, f"expected one warning per bad item, got: {events}"

def test_judge_drops_fabricated_pair_not_in_candidate_set(monkeypatch):
    obs = _obs()
    rule = _rule()
    pack = _pack([rule])

    monkeypatch.setattr(adjudicate, "generate_json", lambda model, parts, schema: [
        {"observation_id": "obs_does_not_exist", "rule_id": rule.id, "triggers": True,
         "severity_adjust": 0, "rationale": "fabricated"},
    ])
    monkeypatch.setattr(
        adjudicate, "generate_grounded",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not verify a fabricated pair's citation")),
    )

    events = []
    findings = adjudicate.judge("run_1", [obs], pack, on_event=lambda a, m: events.append((a, m)))

    assert findings == []
    warnings = [m for a, m in events if "warning" in m.lower()]
    assert warnings, f"expected a warning for the fabricated pair, got: {events}"

def test_judge_drops_pair_with_unhashable_field_without_crashing(monkeypatch):
    # a composite value (list/dict) instead of the declared string type must
    # be dropped like any other malformed pair, not raise TypeError when
    # checked against the candidate-pair set.
    obs = _obs()
    rule = _rule()
    pack = _pack([rule])
    monkeypatch.setattr(adjudicate, "generate_json", lambda model, parts, schema: [
        {"observation_id": ["nested", "list"], "rule_id": rule.id, "triggers": True,
         "severity_adjust": 0, "rationale": "fabricated"},
    ])
    monkeypatch.setattr(
        adjudicate, "generate_grounded",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not verify an unhashable pair's citation")),
    )

    events = []
    findings = adjudicate.judge("run_1", [obs], pack, on_event=lambda a, m: events.append((a, m)))

    assert findings == []
    warnings = [m for a, m in events if "warning" in m.lower()]
    assert warnings, f"expected a warning for the malformed pair, got: {events}"

def test_judge_skips_non_triggering_candidates_without_grounding_call(monkeypatch):
    obs = _obs()
    rule = _rule()
    pack = _pack([rule])
    monkeypatch.setattr(adjudicate, "generate_json", lambda model, parts, schema: [
        {"observation_id": obs.id, "rule_id": rule.id, "triggers": False,
         "severity_adjust": 0, "rationale": "does not apply"},
    ])
    monkeypatch.setattr(
        adjudicate, "generate_grounded",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not verify a non-triggering candidate")),
    )

    findings = adjudicate.judge("run_1", [obs], pack)

    assert findings == []

def test_judge_drops_triggered_item_with_null_rationale(monkeypatch):
    obs = _obs()
    rule = _rule()
    pack = _pack([rule])
    monkeypatch.setattr(adjudicate, "generate_json", lambda model, parts, schema: [
        {"observation_id": obs.id, "rule_id": rule.id, "triggers": True,
         "severity_adjust": 0, "rationale": None},
    ])
    monkeypatch.setattr(
        adjudicate, "generate_grounded",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not verify a dropped item's citation")),
    )

    events = []
    findings = adjudicate.judge("run_1", [obs], pack, on_event=lambda a, m: events.append((a, m)))

    assert findings == []
    warnings = [m for a, m in events if "warning" in m.lower()]
    assert any("rationale" in w.lower() for w in warnings), f"expected an empty-rationale warning, got: {events}"

def test_judge_clamps_severity_adjust_into_neg20_0(monkeypatch):
    obs_low = _obs(id="obs_low")
    obs_high = _obs(id="obs_high")
    rule = _rule(severity=90)
    pack = _pack([rule])

    monkeypatch.setattr(adjudicate, "generate_json", lambda model, parts, schema: [
        {"observation_id": "obs_low", "rule_id": rule.id, "triggers": True,
         "severity_adjust": -999, "rationale": "way over the floor"},
        {"observation_id": "obs_high", "rule_id": rule.id, "triggers": True,
         "severity_adjust": 5, "rationale": "over the ceiling"},
    ])
    monkeypatch.setattr(
        adjudicate, "generate_grounded",
        lambda model, prompt: ("ok", [{"uri": "https://example.com/x", "title": "t"}]),
    )

    findings = adjudicate.judge("run_1", [obs_low, obs_high], pack)

    by_obs = {f.observation_id: f for f in findings}
    assert by_obs["obs_low"].severity == 70   # 90 + clamp(-999) == 90 + (-20)
    assert by_obs["obs_high"].severity == 90  # 90 + clamp(5) == 90 + 0

def test_judge_caps_severity_at_40_when_unsourced(monkeypatch):
    obs = _obs()
    rule = _rule(severity=95)
    pack = _pack([rule])
    monkeypatch.setattr(adjudicate, "generate_json", lambda model, parts, schema: [
        {"observation_id": obs.id, "rule_id": rule.id, "triggers": True,
         "severity_adjust": 0, "rationale": "triggers the ban"},
    ])
    monkeypatch.setattr(adjudicate, "generate_grounded", lambda model, prompt: ("no sources found", []))

    findings = adjudicate.judge("run_1", [obs], pack)

    assert len(findings) == 1
    f = findings[0]
    assert f.sourced is False
    assert f.severity == 40
    assert f.citation_url == ""
    assert f.citation_ref == rule.basis

def test_judge_builds_finding_correctly_when_sourced(monkeypatch):
    obs = _obs(id="obs_shot_0_001", t_start=3.0, t_end=6.0)
    rule = _rule(id="ZZ-ALC-01", severity=90, klass="legal", basis="Statute X", trigger="Depiction of alcohol")
    pack = _pack([rule])
    monkeypatch.setattr(adjudicate, "generate_json", lambda model, parts, schema: [
        {"observation_id": obs.id, "rule_id": rule.id, "triggers": True, "severity_adjust": -10,
         "rationale": "A wine glass triggers the alcohol ban."},
    ])
    monkeypatch.setattr(
        adjudicate, "generate_grounded",
        lambda model, prompt: ("confirmed", [{"uri": "https://legifrance.example/x", "title": "Statute X"}]),
    )

    findings = adjudicate.judge("run_9", [obs], pack)

    assert len(findings) == 1
    f = findings[0]
    assert f.id == f"fnd_{pack.market}_{rule.id}_{obs.id}"
    assert f.run_id == "run_9"
    assert f.observation_id == obs.id
    assert f.market == pack.market
    assert f.rule_id == rule.id
    assert f.klass == "legal"
    assert f.severity == 80  # 90 + (-10)
    assert f.t_start == 3.0 and f.t_end == 6.0
    assert f.rationale == "A wine glass triggers the alcohol ban."
    assert f.citation_ref == "Statute X"
    assert f.citation_url == "https://legifrance.example/x"
    assert f.sourced is True
    assert f.remediable == rule.remediable
    assert f.remediation_blocked is False
    assert f.blocked_reason == ""
    assert f.status == "open"

def test_judge_works_without_on_event(monkeypatch):
    obs = _obs()
    rule = _rule()
    pack = _pack([rule])
    monkeypatch.setattr(adjudicate, "generate_json", lambda model, parts, schema: [
        {"observation_id": obs.id, "rule_id": rule.id, "triggers": True,
         "severity_adjust": 0, "rationale": "x"},
    ])
    monkeypatch.setattr(
        adjudicate, "generate_grounded",
        lambda model, prompt: ("ok", [{"uri": "https://example.com/x", "title": "t"}]),
    )

    findings = adjudicate.judge("run_1", [obs], pack)
    assert len(findings) == 1

# --- PROMPT contract ---

def test_prompt_interpolates_market_name():
    rendered = adjudicate.PROMPT.format(market_name="France")
    assert "France" in rendered
    assert "{market_name}" not in rendered
    assert "advertising clearance officer" in rendered
    assert "a wine glass triggers an alcohol-advertising ban" in rendered

def test_citation_prompt_interpolates_basis_trigger_market_name():
    rendered = adjudicate.CITATION_PROMPT.format(
        basis="Loi Evin L3323-2", trigger="Depiction of alcohol", market_name="France")
    assert "Loi Evin L3323-2" in rendered
    assert "Depiction of alcohol" in rendered
    assert "France" in rendered
    assert "{basis}" not in rendered and "{trigger}" not in rendered and "{market_name}" not in rendered

# --- Step 4: live spot-check ---

@pytest.mark.live
def test_judge_live_fr_alcohol_finding_is_sourced(tmp_path):
    fr = load_packs(MARKETS_DIR)["FR"]
    observations = analyst.observe_all(TEST_AD, tmp_path)
    findings = adjudicate.judge("run_live_fr", observations, fr)

    print(f"\n--- FR findings: {len(findings)} total ---")
    for f in findings:
        print(
            f"{f.rule_id:12} klass={f.klass:8} severity={f.severity:3} sourced={str(f.sourced):5} "
            f"obs={f.observation_id} t=[{f.t_start:.2f},{f.t_end:.2f}] "
            f"citation_url={f.citation_url or '(none)'}"
        )
        print(f"    citation_ref: {f.citation_ref}")
        print(f"    rationale:    {f.rationale}")
    print(f"--- clearance: {adjudicate.clearance(findings)} ---")

    alc = [f for f in findings if f.rule_id == "FR-ALC-01"]
    assert alc, f"expected a FR-ALC-01 finding, got rule_ids: {[f.rule_id for f in findings]}"
    assert alc[0].sourced is True, f"FR-ALC-01 finding was not sourced: {alc[0]}"
