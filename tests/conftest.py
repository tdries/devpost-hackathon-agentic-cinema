"""Test-wide defaults that keep a developer's .env out of the assertions.

The suite loads the real Settings, which reads the real .env, so anything
optional a developer switches on locally silently changes what the console
renders under test. That bit for real the day GRAFANA_VIEWER_URL was set:
the launch board started framing live Grafana and three PNG-shaped
assertions failed, with nothing wrong in the code.

So the optional integrations are OFF by default here, and a test that wants
one says so (see test_the_board_frames_live_grafana_when_the_viewer_is_deployed).
"""
import dataclasses
import os

# The doors now fail closed: an unset password means the door stays shut
# rather than opening to a word published in this repo. So the suite has to
# say what its own words are, exactly as a deployment does, before anything
# imports customs.config and freezes the settings.
os.environ.setdefault("JUDGE_PASSWORD", "test-judge-word")
os.environ.setdefault("VISITOR_PASSWORD", "test-visitor-word")
os.environ.setdefault("EDITS_PASSWORD", "test-edits-word")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
# Endpoints so the push paths are exercised rather than skipped. Nothing
# reaches them: the tests stub telemetry._post and assert on what it was
# handed. Set here so a checkout with no .env behaves exactly like a
# developer's machine, which is what CI is.
os.environ.setdefault("LOKI_PUSH_URL", "https://loki.invalid/loki/api/v1/push")
os.environ.setdefault("OTLP_URL", "https://otlp.invalid")
# The rest of the shape a configured instance has. Values, not credentials:
# the suite stubs every transport, and what these buy is that a checkout
# with no .env takes the same code paths as a developer's machine. Without
# them three tests passed locally and failed on a clean clone, which is the
# difference CI exists to catch.
os.environ.setdefault("GRAFANA_URL", "https://grafana.invalid")
os.environ.setdefault("GRAFANA_STACK_ID", "000000")
os.environ.setdefault("LOKI_USER", "000000")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")

import pytest


@pytest.fixture(autouse=True)
def _no_optional_integrations(monkeypatch):
    from customs import app as app_module

    # The webhook's shared token goes with them. A developer's .env has a
    # real one, and with it every offline webhook test posts without a key
    # and gets the 404 a stranger gets. The test that is ABOUT the token
    # sets its own.
    changed = {}
    if getattr(app_module.settings, "grafana_viewer_url", ""):
        changed["grafana_viewer_url"] = ""
    if getattr(app_module.settings, "webhook_token", ""):
        changed["webhook_token"] = ""
    if changed:
        monkeypatch.setattr(app_module, "settings", dataclasses.replace(
            app_module.settings, **changed))


@pytest.fixture(autouse=True)
def _no_telemetry_pacing(monkeypatch):
    """Telemetry paces and backs off its writes, because Grafana Cloud's
    free plan throttles a share of them whatever the rate. Waiting is the
    behaviour, so a suite that exercised it would wait too: five backoffs
    is seventy-seven seconds, and a run with fifty findings paces fifty
    writes. The one test that is ABOUT the retry replaces this itself."""
    from customs import telemetry

    monkeypatch.setattr(telemetry, "_SLEEP", lambda seconds: None)
