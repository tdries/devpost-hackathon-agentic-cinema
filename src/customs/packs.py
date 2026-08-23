from dataclasses import dataclass
from pathlib import Path

import yaml

_DEFAULT_MARKETS_DIR = Path("markets")
_TAXONOMY_FILENAME = "_taxonomy.yaml"
_VALID_KLASSES = {"legal", "policy", "offence"}
_VALID_PRE_CLEARANCE = {"none", "advisory", "mandatory"}

class PackError(Exception):
    """Raised when a market pack file fails validation. Message names the file and rule."""

@dataclass
class MarketRule:
    id: str
    dimension: str
    klass: str
    severity: int
    trigger: str
    basis: str
    source_hint: str = ""
    remediable: bool = True
    protected_basis: bool = False

@dataclass
class MarketPack:
    market: str
    name: str
    regulators: list[str]
    pre_clearance: str
    rules: list[MarketRule]

def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise PackError(f"{path.name}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise PackError(f"{path.name}: expected a YAML mapping at the top level")
    return data

def taxonomy(markets_dir: Path = _DEFAULT_MARKETS_DIR) -> set[str]:
    """The fixed observation-dimension taxonomy (design spec section 5)."""
    path = Path(markets_dir) / _TAXONOMY_FILENAME
    data = _read_yaml(path)
    dims = data.get("dimensions") or []
    return set(dims)

def _build_rule(path: Path, raw: dict, dims: set[str], seen_rule_ids: dict[str, str]) -> MarketRule:
    if not isinstance(raw, dict) or not raw.get("id"):
        raise PackError(f"{path.name}: a rule is missing the required 'id' field")
    rule_id = raw["id"]

    dimension = raw.get("dimension")
    if dimension not in dims:
        raise PackError(f"{path.name}: rule {rule_id!r} has unknown dimension {dimension!r}")

    klass = raw.get("class")
    if klass not in _VALID_KLASSES:
        raise PackError(
            f"{path.name}: rule {rule_id!r} has invalid class {klass!r} "
            f"(must be one of {sorted(_VALID_KLASSES)})"
        )

    severity = raw.get("severity")
    if isinstance(severity, bool) or not isinstance(severity, int) or not (0 <= severity <= 100):
        raise PackError(f"{path.name}: rule {rule_id!r} has severity {severity!r} outside 0..100")

    if rule_id in seen_rule_ids:
        origin = seen_rule_ids[rule_id]
        where = "the same pack" if origin == path.name else origin
        raise PackError(f"{path.name}: duplicate rule id {rule_id!r} (already defined in {where})")
    seen_rule_ids[rule_id] = path.name

    return MarketRule(
        id=rule_id,
        dimension=dimension,
        klass=klass,
        severity=severity,
        trigger=raw.get("trigger", ""),
        basis=raw.get("basis", ""),
        source_hint=raw.get("source_hint", ""),
        remediable=raw.get("remediable", True),
        protected_basis=raw.get("protected_basis", False),
    )

def _load_pack(path: Path, dims: set[str], seen_rule_ids: dict[str, str]) -> MarketPack:
    data = _read_yaml(path)

    market = data.get("market")
    if not market:
        raise PackError(f"{path.name}: missing required 'market' field")

    pre_clearance = data.get("pre_clearance", "none")
    if pre_clearance not in _VALID_PRE_CLEARANCE:
        raise PackError(
            f"{path.name}: pre_clearance {pre_clearance!r} "
            f"must be one of {sorted(_VALID_PRE_CLEARANCE)}"
        )

    rules = [_build_rule(path, raw, dims, seen_rule_ids) for raw in (data.get("rules") or [])]

    return MarketPack(
        market=market,
        name=data.get("name", market),
        regulators=data.get("regulators") or [],
        pre_clearance=pre_clearance,
        rules=rules,
    )

def load(markets_dir: Path = _DEFAULT_MARKETS_DIR) -> dict[str, MarketPack]:
    """Load every *.yaml pack in markets_dir except files starting with '_'.

    Rule dimensions are always validated against the canonical taxonomy() (the
    fixed system-wide dimension vocabulary), not against markets_dir, so a
    single throwaway pack file is enough to test validation without also
    needing a throwaway taxonomy file alongside it.
    """
    markets_dir = Path(markets_dir)
    dims = taxonomy()
    seen_rule_ids: dict[str, str] = {}
    packs: dict[str, MarketPack] = {}
    for path in sorted(markets_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        pack = _load_pack(path, dims, seen_rule_ids)
        packs[pack.market] = pack
    return packs
