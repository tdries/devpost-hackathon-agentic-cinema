from pathlib import Path

import pytest
import yaml

from customs.packs import MarketPack, MarketRule, PackError, load, taxonomy

MARKETS_DIR = Path("markets")

# Exact 18 dimensions from the design spec, section 5. taxonomy() must return
# precisely this set, not just 18 entries, so a typo that preserves the count
# still fails this test.
REQUIRED_DIMENSIONS = {
    "alcohol_tobacco_drugs", "religious_symbols_practices", "modesty_dress_body",
    "gesture_body_language", "food_and_animals", "gender_portrayal",
    "sexual_orientation_gender_id", "children_and_minors", "national_symbols_politics",
    "health_claims_pharma", "gambling_and_finance", "violence_and_weapons",
    "language_profanity_idiom", "humour_irony_satire", "superstition_number_colour",
    "photosensitivity_sensory", "text_legibility", "comparative_claims",
}


def test_taxonomy_has_18_dimensions():
    dims = taxonomy()
    assert isinstance(dims, set)
    assert len(dims) == 18
    assert dims == REQUIRED_DIMENSIONS


def test_fr_pack_loads_with_alc_01():
    packs = load(MARKETS_DIR)
    fr = packs["FR"]
    assert isinstance(fr, MarketPack)
    assert fr.market == "FR"
    assert 6 <= len(fr.rules) <= 12
    alc = next(r for r in fr.rules if r.id == "FR-ALC-01")
    assert isinstance(alc, MarketRule)
    assert alc.dimension == "alcohol_tobacco_drugs"
    assert alc.klass == "legal"
    assert alc.severity >= 90


def test_fr_pack_has_a_toubon_text_legibility_rule():
    packs = load(MARKETS_DIR)
    fr = packs["FR"]
    toubon = [r for r in fr.rules if r.dimension == "text_legibility"]
    assert toubon, "expected at least one text_legibility rule (Loi Toubon)"
    assert any("Toubon" in r.basis for r in toubon)


def test_all_real_packs_load_without_error():
    packs = load(MARKETS_DIR)
    assert set(packs.keys()) == {"FR", "SA", "US"}
    for market, pack in packs.items():
        assert pack.market == market
        assert 6 <= len(pack.rules) <= 12
        ids = [r.id for r in pack.rules]
        assert len(ids) == len(set(ids))
        for rule in pack.rules:
            assert rule.klass in {"legal", "policy", "offence"}
            assert rule.basis
            assert rule.trigger


def test_sa_pack_has_a_protected_basis_rule():
    packs = load(MARKETS_DIR)
    sa = packs["SA"]
    protected = [r for r in sa.rules if r.protected_basis]
    assert protected, "expected at least one protected_basis rule in the SA pack"


def test_sa_pack_has_modesty_and_alcohol_rules():
    packs = load(MARKETS_DIR)
    sa = packs["SA"]
    dims = {r.dimension for r in sa.rules}
    assert "modesty_dress_body" in dims
    assert "alcohol_tobacco_drugs" in dims


def test_us_pack_leans_policy_and_offence():
    packs = load(MARKETS_DIR)
    us = packs["US"]
    non_legal = [r for r in us.rules if r.klass in {"policy", "offence"}]
    assert len(non_legal) > len(us.rules) / 2
    legal_dims = {r.dimension for r in us.rules if r.klass == "legal"}
    assert "comparative_claims" in legal_dims
    assert "health_claims_pharma" in legal_dims


def _write_pack(path: Path, rules: list, market: str = "ZZ") -> None:
    data = {
        "market": market,
        "name": "Test market",
        "regulators": ["TEST"],
        "pre_clearance": "none",
        "rules": rules,
    }
    path.write_text(yaml.safe_dump(data))


def _rule(**overrides) -> dict:
    fields = dict(
        id="ZZ-TEST-01",
        dimension="alcohol_tobacco_drugs",
        klass="legal",
        severity=50,
        trigger="x",
        basis="y",
    )
    fields.update(overrides)
    # YAML key is "class", dataclass field is "klass"
    fields["class"] = fields.pop("klass")
    return fields


def test_bogus_dimension_raises_pack_error_naming_rule(tmp_path):
    _write_pack(tmp_path / "ZZ.yaml", [_rule(id="ZZ-BAD-01", dimension="not_a_real_dimension")])
    with pytest.raises(PackError, match="ZZ-BAD-01"):
        load(tmp_path)


def test_duplicate_rule_id_within_one_pack_raises(tmp_path):
    _write_pack(tmp_path / "ZZ.yaml", [_rule(id="DUP-01"), _rule(id="DUP-01")])
    with pytest.raises(PackError, match="DUP-01"):
        load(tmp_path)


def test_duplicate_rule_id_across_two_packs_raises(tmp_path):
    _write_pack(tmp_path / "AA.yaml", [_rule(id="DUP-01")], market="AA")
    _write_pack(tmp_path / "BB.yaml", [_rule(id="DUP-01")], market="BB")
    with pytest.raises(PackError, match="DUP-01"):
        load(tmp_path)


def test_severity_out_of_range_raises(tmp_path):
    _write_pack(tmp_path / "ZZ.yaml", [_rule(id="ZZ-SEV-01", severity=150)])
    with pytest.raises(PackError, match="ZZ-SEV-01"):
        load(tmp_path)


def test_invalid_klass_raises(tmp_path):
    _write_pack(tmp_path / "ZZ.yaml", [_rule(id="ZZ-KLASS-01", klass="not_a_klass")])
    with pytest.raises(PackError, match="ZZ-KLASS-01"):
        load(tmp_path)


def test_invalid_pre_clearance_raises(tmp_path):
    path = tmp_path / "ZZ.yaml"
    data = {
        "market": "ZZ",
        "name": "Test market",
        "regulators": ["TEST"],
        "pre_clearance": "sometimes",
        "rules": [_rule(id="ZZ-PC-01")],
    }
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(PackError, match="ZZ.yaml"):
        load(tmp_path)


def test_underscore_files_are_not_loaded_as_packs(tmp_path):
    _write_pack(tmp_path / "_taxonomy.yaml", [_rule(id="SHOULD-NOT-LOAD")])
    _write_pack(tmp_path / "AA.yaml", [_rule(id="AA-01")], market="AA")
    packs = load(tmp_path)
    assert set(packs.keys()) == {"AA"}
