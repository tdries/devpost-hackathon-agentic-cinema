import os
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
        )

settings = Settings.load()
