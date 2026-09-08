import secrets
import os
import re
from dataclasses import dataclass
from pathlib import Path

def _read_env_file(path: Path) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out

# The two Grafana pages Launch Control embeds, as public-dashboard share
# URLs. They are pinned here rather than discovered at request time on
# purpose: building a GrafanaOps to ask the stack for its access tokens
# spawns the mcp-grafana subprocess and makes a network call, and the console
# builds these URLs inside a request handler that must never do either
# (grafana_ops.embed_url has the same rule: "pure string building, never a
# network call"). The tokens are stable for the life of the share -- they
# change only if someone revokes public sharing and enables it again -- and
# scripts/provision_grafana.py prints the current pair on every run, so a
# reprovisioned stack is a one line edit here or a GRAFANA_PUBLIC_* env var.
#
# These pages have no login by design (see grafana_ops.enable_public): they
# are the judge facing surface and carry demo findings about a synthetic test
# asset. A share token is not a credential and grants read of those two pages
# only, which is why it can sit in the repo when nothing else here can.
_PUBLIC_DASHBOARDS = {
    "customs-overview": "https://dreamystairs2355.grafana.net/public-dashboards/572d542e26ea4384b206deab8589e63e",
    "customs-timeline": "https://dreamystairs2355.grafana.net/public-dashboards/35f3ef6746614fd0948172de3e64c11d",
}

@dataclass(frozen=True)
class Settings:
    grafana_url: str
    grafana_sa_token: str
    grafana_cloud_token: str
    grafana_stack_id: str
    otlp_url: str
    loki_push_url: str
    loki_user: str
    gcp_project: str
    gcp_location: str
    model_vision: str
    model_text: str
    model_image: str
    model_video: str
    model_omni: str
    model_tts: str
    db_path: str
    grafana_public_overview: str
    grafana_public_timeline: str
    grafana_viewer_url: str
    webhook_token: str
    judge_password: str
    session_secret: str
    visitor_password: str
    # The word that deletes an edit. Its own setting rather than the judge
    # word, so the thing that removes evidence can be rotated without
    # locking judges out -- and it defaults to the judge word rather than
    # to something guessable, because the default of a destructive control
    # should not be a convenience.
    edits_password: str

    @classmethod
    def load(cls, env_file: Path | str | None = ".env") -> "Settings":
        f = _read_env_file(Path(env_file)) if env_file else {}
        def g(key, default=""):
            return os.environ.get(key, f.get(key, default))
        return cls(
            grafana_url=g("GRAFANA_URL"),
            grafana_sa_token=g("GRAFANA_SA_TOKEN"),
            grafana_cloud_token=g("GRAFANA_CLOUD_TOKEN"),
            grafana_stack_id=g("GRAFANA_STACK_ID"),
            otlp_url=g("OTLP_URL"),
            loki_push_url=g("LOKI_PUSH_URL"),
            loki_user=g("LOKI_USER"),
            gcp_project=g("GOOGLE_CLOUD_PROJECT"),
            gcp_location=g("GOOGLE_CLOUD_LOCATION", "europe-west1"),
            model_vision=g("GEMINI_MODEL_VISION", "gemini-3.7-flash"),
            model_text=g("GEMINI_MODEL_TEXT", "gemini-3.7-flash"),
            model_image=g("IMAGEN_MODEL", "gemini-3.1-flash-image"),
            model_video=g("VEO_MODEL", "veo-3.1-generate-001"),
            # The OLD alias, deliberately. gemini-omni-1.1-flash-preview
            # sits behind an access gate that answers a fake quota error:
            # probed 2026-09-01, effective quota granted and reading 10
            # everywhere Google will show it, and request #1 of a fresh
            # minute still 429s. The alias works today on this project. It
            # deprecates 2026-09-30 -- after the deadline -- and the day
            # 1.1 unlocks, OMNI_MODEL flips it without a deploy... of code.
            # The bare "gemini-omni-1.1-flash" answers "Unsupported model
            # interaction" on Vertex.
            model_omni=g("OMNI_MODEL", "gemini-omni-flash-preview"),
            model_tts=g("TTS_MODEL", "gemini-2.5-flash-tts"),
            db_path=g("CUSTOMS_DB", "runs/customs.db"),
            # `or` rather than a default argument: .env.example ships both
            # keys empty, and an empty override must fall back to the pin
            # rather than blank the console's embeds.
            grafana_public_overview=(g("GRAFANA_PUBLIC_OVERVIEW")
                                     or _PUBLIC_DASHBOARDS["customs-overview"]),
            grafana_public_timeline=(g("GRAFANA_PUBLIC_TIMELINE")
                                     or _PUBLIC_DASHBOARDS["customs-timeline"]),
            # The embeddable viewer (scripts/deploy_viewer.sh). Empty when
            # it is not deployed, and every screen falls back to the
            # server-rendered PNGs it used before.
            grafana_viewer_url=g("GRAFANA_VIEWER_URL", "").rstrip("/"),
            # The two doors. Not real authentication -- there is one shared
            # word per door and it travels in a cookie -- but enough that a
            # link posted somewhere public does not hand a stranger the
            # generation budget.
            # The webhook's shared secret. Grafana cannot sign a request, so
            # the contact point carries the token in its URL and the route
            # refuses anything else. Empty means open, which is what the
            # offline tests and a laptop run want; deploy.sh always sets it.
            webhook_token=g("WEBHOOK_TOKEN", ""),
            # Signs the role cookie. Random per process when unset, which
            # is right for a laptop (cookies die with the server) and wrong
            # for production, where deploy.sh mints one into Secret Manager.
            session_secret=g("SESSION_SECRET", "") or secrets.token_urlsafe(32),
            # No published default. The old one ("DEVPOST") is in a public
            # repo, and it is the word that lifts the spend ceiling: an
            # unset password now means the door stays shut rather than
            # standing open with a known key.
            judge_password=g("JUDGE_PASSWORD", ""),
            visitor_password=g("VISITOR_PASSWORD", ""),
            edits_password=g("EDITS_PASSWORD", g("JUDGE_PASSWORD", "")),
        )

settings = Settings.load()

# --- footage this instance will not show --------------------------------
#
# Devpost puts the rights to everything in a submission on the entrant,
# and this archive grew during development out of whatever was to hand: a
# Chanel spot with a famous actor in it, a Bond clip, three reels of studio
# cartoons, a Doritos Super Bowl ad, half a dozen brand films and two stock
# clips. None of it is ours to publish, Omni and Veo both refuse most of it
# at the input anyway, and a rights-clearance tool is the last thing that
# should be borrowing footage. What stays is the corpus this project made
# with Veo: ember_lounge, solstice_rooftop, voltage_runway and the test ad.
#
# Hidden, not deleted, and deliberately so: every row, frame and log line
# stays exactly where it is, and the console stops listing, querying and
# serving them. Deleting a name from this tuple brings its run back
# whole. docs/audit/archive-before-ip-sweep.json is the inventory as it
# stood when the list was drawn.
#
# Names are file stems, which is what `asset` means everywhere in this
# system: the label on every metric and log line, the Grafana variable,
# and what asset_key() reduces a run to.
# Empty, on the owner's instruction: every run in the archive is shown.
#
# This held twenty-four stems for a while -- borrowed footage that came into
# the corpus while the tool was being built, hidden rather than deleted so
# nothing was lost. The owner wants those clearances back in the app, which
# is their call to make; the mechanism below is left intact and tested, so
# putting a stem back in this tuple hides it again everywhere at once: the
# archive listing, the run's own pages, every cross-run Loki query, the
# agent's composed queries and the framed dashboards (via
# scripts/stamp_withheld_dashboards.py).
WITHHELD_ASSETS: tuple[str, ...] = ()


def is_withheld(asset: str) -> bool:
    """Is this asset stem one the console will not show?"""
    return asset in WITHHELD_ASSETS


def scope_logql(expr: str) -> str:
    """A LogQL expression with every stream selector scoped to the corpus.

    For queries this project does not write: agent mode hands the model the
    label schema and lets it compose its own LogQL, which is the whole
    point of that screen and also a way for a withheld film to be named in
    an answer. Every selector mentioning this app gets the matcher, once.
    """
    matcher = withheld_matcher()
    if not matcher or not expr:
        return expr
    return re.sub(
        r'\{([^{}]*app\s*=\s*"customs"[^{}]*)\}',
        lambda m: "{" + m.group(1) + ("" if "asset!~" in m.group(1) else matcher) + "}",
        expr)


def withheld_matcher(label: str = "asset") -> str:
    """A LogQL label matcher excluding the withheld corpus, or "".

    Returned with its leading comma so it drops into a stream selector:
    `{app="customs", kind="finding"<here>}`. Negative rather than an
    allowlist, because a visitor's own clearance has to keep appearing on
    the cross-run boards the moment it lands, and only these named films
    are the problem.
    """
    if not WITHHELD_ASSETS:
        return ""
    # Backticks, and a narrow escape. LogQL lexes a double-quoted string
    # Go-style before the regex engine ever sees it, so re.escape's `\-`
    # comes back as "parse error: invalid char escape"; a backtick string
    # is raw, and only RE2's own metacharacters need escaping inside it.
    alts = "|".join(re.sub(r"([.^$*+?()\[\]{}|\\`])", r"\\\1", a)
                    for a in WITHHELD_ASSETS)
    return f", {label}!~`^({alts})$`"
