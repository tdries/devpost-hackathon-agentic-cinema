from dataclasses import asdict, dataclass, field

FINDING_STATUSES = {"open", "remediating", "resolved"}

class _JsonMixin:
    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict):
        return cls(**data)

@dataclass
class Observation(_JsonMixin):
    id: str
    shot_id: str
    t_start: float
    t_end: float
    dimension: str
    statement: str
    evidence_frame: str
    confidence: float

@dataclass
class Finding(_JsonMixin):
    id: str
    run_id: str
    observation_id: str
    market: str
    rule_id: str
    klass: str
    severity: int
    t_start: float
    t_end: float
    rationale: str
    citation_ref: str
    citation_url: str
    sourced: bool
    remediable: bool
    remediation_blocked: bool
    blocked_reason: str
    status: str = "open"
    # How structural the violation is: frame | segment | scene | concept.
    # See customs/scope.py. Defaulted because findings written before scope
    # existed are still findings, and "" means "nobody judged it, measure it".
    scope: str = ""
    # Whether swapping the offending element for a permitted one would
    # satisfy the rule and leave the commercial standing. A child in a
    # slingshot gag is substitutable; an advertisement for alcohol is not.
    substitutable: bool = True
    # Three concrete ways to deal with THIS finding, written by the judge in
    # the same call that decided it triggered, so they describe the thing it
    # actually saw. Each is {"label": shown to the operator, "directive":
    # given to the image editor}. Empty on findings judged before this
    # existed, and on those costs.suggestions falls back to its table.
    remedies: list = field(default_factory=list)

    def __post_init__(self):
        if self.status not in FINDING_STATUSES:
            raise ValueError(f"invalid finding status: {self.status!r}")

@dataclass
class ChangeRecord(_JsonMixin):
    id: str
    run_id: str
    finding_id: str
    method: str
    description: str
    before_frame: str
    after_frame: str

@dataclass
class RunRecord(_JsonMixin):
    id: str
    asset_path: str
    t0: float | None
    status: str
    markets: list[str]
