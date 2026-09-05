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
