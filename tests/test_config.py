import os
from customs.config import Settings

def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("GRAFANA_URL", "https://x.grafana.net")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p1")
    s = Settings.load(env_file=None)
    assert s.grafana_url == "https://x.grafana.net"
    assert s.gcp_project == "p1"

def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL_VISION", raising=False)
    s = Settings.load(env_file=None)
    assert s.model_vision  # has a default
    assert s.db_path.endswith(".db")
