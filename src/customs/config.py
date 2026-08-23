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
    model_tts: str
    db_path: str

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
            model_tts=g("TTS_MODEL", "gemini-2.5-flash-tts"),
            db_path=g("CUSTOMS_DB", "runs/customs.db"),
        )

settings = Settings.load()
