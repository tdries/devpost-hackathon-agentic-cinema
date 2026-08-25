from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DEFAULT_MARKETS_DIR = Path("markets")
_TAXONOMY_FILENAME = "_taxonomy.yaml"
_CHANNELS_FILENAME = "_channels.yaml"
_VALID_KLASSES = {"legal", "policy", "offence"}
_VALID_PRE_CLEARANCE = {"none", "advisory", "mandatory"}
# The jurisdiction ladder. A pack names its level and its parent, and load()
# resolves each pack's rule list to its own rules plus every ancestor's, so
# everything downstream (adjudicate, guard, telemetry) keeps seeing one flat
# rule list per market and needs no idea that a hierarchy exists.
_LEVELS = ("global", "continental", "regional", "national", "subnational", "channel")

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
    rules: list[MarketRule]          # own + inherited, resolved by load()
    level: str = "national"
    parent: str = ""
    own_rules: list[MarketRule] = field(default_factory=list)

    @property
    def inherited(self) -> int:
        return len(self.rules) - len(self.own_rules)

def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise PackError(f"{path.name}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise PackError(f"{path.name}: expected a YAML mapping at the top level")
    return data

def taxonomy() -> set[str]:
    """The fixed observation-dimension taxonomy (design spec section 5).

    Always reads the canonical markets/_taxonomy.yaml. There is exactly one
    dimension vocabulary for the whole system, so this intentionally takes no
    markets_dir override, unlike load() below (which loads pack files from a
    given directory but still validates dimensions against this fixed file).
    """
    path = _DEFAULT_MARKETS_DIR / _TAXONOMY_FILENAME
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

    level = data.get("level", "national")
    if level not in _LEVELS:
        raise PackError(f"{path.name}: level {level!r} must be one of {list(_LEVELS)}")

    rules = [_build_rule(path, raw, dims, seen_rule_ids) for raw in (data.get("rules") or [])]

    return MarketPack(
        market=market,
        name=data.get("name", market),
        regulators=data.get("regulators") or [],
        pre_clearance=pre_clearance,
        rules=list(rules),
        level=level,
        parent=data.get("parent", "") or "",
        own_rules=list(rules),
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
    seen_markets: dict[str, str] = {}
    packs: dict[str, MarketPack] = {}
    for path in sorted(markets_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        pack = _load_pack(path, dims, seen_rule_ids)
        if pack.market in seen_markets:
            raise PackError(
                f"{path.name}: duplicate market {pack.market!r} "
                f"(already defined in {seen_markets[pack.market]})"
            )
        seen_markets[pack.market] = path.name
        packs[pack.market] = pack
    _add_registry_channels(markets_dir, packs)
    return _resolve(packs)


def _add_registry_channels(markets_dir: Path, packs: dict[str, MarketPack]) -> None:
    """Turn markets/_channels.yaml into selectable nodes with no rules of
    their own. A channel that has its own pack file keeps it; this only fills
    in the ones nobody has written rules for yet, so every listed broadcaster
    is analysable against its country's rules from the moment it is named."""
    path = markets_dir / _CHANNELS_FILENAME
    if not path.is_file():
        return
    for country, entries in (_read_yaml(path).get("channels") or {}).items():
        if country not in packs:
            raise PackError(f"{path.name}: channels listed for unknown market {country!r}")
        for entry in entries or []:
            cid = entry.get("id")
            if not cid:
                raise PackError(f"{path.name}: a channel under {country} has no id")
            if cid in packs:
                continue
            packs[cid] = MarketPack(
                market=cid, name=entry.get("name", cid),
                regulators=list(packs[country].regulators),
                pre_clearance=packs[country].pre_clearance,
                rules=[], level="channel", parent=country, own_rules=[])


def _resolve(packs: dict[str, MarketPack]) -> dict[str, MarketPack]:
    """Give every pack its ancestors' rules, outermost first.

    A channel is judged against its own rules AND its country's AND its
    continent's AND the global baseline, which is what "analyse at channel
    level" has to mean. Resolved here rather than at judging time so that a
    market is still one flat rule list everywhere else in the system.
    """
    for pack in packs.values():
        chain, seen, node = [], {pack.market}, pack
        while node.parent:
            if node.parent not in packs:
                raise PackError(
                    f"{node.market}: parent {node.parent!r} is not a known pack")
            if node.parent in seen:
                raise PackError(f"{node.market}: parent cycle via {node.parent!r}")
            seen.add(node.parent)
            node = packs[node.parent]
            chain.append(node)
        inherited = [r for ancestor in reversed(chain) for r in ancestor.own_rules]
        pack.rules = inherited + pack.own_rules
    return packs
