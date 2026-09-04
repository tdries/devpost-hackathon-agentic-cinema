"""The Grafana inventory, and the drift that would make it a lie.

The page this feeds is the one place a reader can see the whole Grafana
surface at once, which makes it the one page where being out of date is
worse than being absent: a judge reading "four metric series" while the
crew writes five has been told something false about the claim the whole
project rests on.

So these tests do not check the prose. They check that the inventory and
the code that writes to Grafana still name the same things.
"""
import json
import re
from pathlib import Path

from customs import grafana_map, grafana_ops

TELEMETRY = Path("src/customs/telemetry.py").read_text()


def test_every_series_telemetry_pushes_is_in_the_inventory():
    """The metric names live as string literals at the push sites, so this
    reads them straight out of the module. Backticked mentions in the
    docstring are not matched: only a quoted name is a name that gets
    pushed."""
    pushed = set(re.findall(r'"(customs_[a-z_]+)"', TELEMETRY))
    named = {series["name"] for series in grafana_map.SERIES}

    assert pushed == named, (
        f"telemetry pushes {sorted(pushed - named)} that the inventory does not "
        f"name, and names {sorted(named - pushed)} that it does not push")


def test_every_loki_stream_kind_is_in_the_inventory():
    kinds = set(re.findall(r'"kind":\s*"([a-z]+)"', TELEMETRY))
    kinds |= set(re.findall(r'_OBS_LABEL_KIND\s*=\s*"([a-z]+)"', TELEMETRY))
    named = {stream["kind"] for stream in grafana_map.STREAMS}

    assert kinds == named, f"stream kinds drifted: {kinds ^ named}"


def test_every_dashboard_on_disk_is_read_with_its_panels():
    """The dashboards are parsed from the same files ensure_dashboards
    pushes, so the count is the truth by construction. What this catches is
    a file that stops parsing, or a uid the inventory cannot see."""
    files = sorted(grafana_ops.DASHBOARD_DIR.glob("*.json"))
    boards = grafana_map.dashboards()

    assert len(boards) == len(files) and files, "one entry per dashboard file"
    for path, board in zip(files, boards):
        raw = json.loads(path.read_text())
        body = raw.get("dashboard", raw)
        assert board.uid == body.get("uid", path.stem)
        assert board.title == body.get("title", board.uid)
        assert len(board.panels) == len(body.get("panels", []))
        assert board.shown_in, f"{board.uid} does not say where it is seen"

    totals = grafana_map.totals()
    assert totals["panels"] == sum(len(b.panels) for b in boards)


def test_every_write_operation_declares_its_transport():
    """Straight from the table the runtime itself consults, so an operation
    added to grafana_ops appears here without anybody remembering to add
    it. The assertion is that none of them is silently blank."""
    rows = grafana_map.operations()

    assert len(rows) == len(grafana_ops.MAPPING)
    for row in rows:
        assert row["http"], f"{row['op']} has no REST path"
        # mcp may be empty: that is the honest answer for the four
        # operations mcp-grafana 1.1.0 has no write tool for.
        assert row["op"] in grafana_ops.MAPPING


def test_the_alerting_summary_is_the_rules_themselves():
    alerting = grafana_map.alerting()

    assert alerting["contact_point"] == grafana_ops.CONTACT_POINT_NAME
    assert alerting["interval_s"] == grafana_ops.ALERT_GROUP_INTERVAL_S
    assert len(alerting["rules"]) == len(grafana_ops.ALERT_RULES)
    for shown, real in zip(alerting["rules"], grafana_ops.ALERT_RULES):
        assert shown["uid"] == real["uid"] and shown["expr"] == real["expr"]


def test_the_inventory_never_touches_the_network(monkeypatch):
    """It is read off disk and out of two modules. If it ever grows a live
    query, the page it feeds becomes the slowest one in the console, which
    is the opposite of what it is for."""
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - the point is not calling it
        raise AssertionError("the Grafana inventory made a network call")

    monkeypatch.setattr(httpx, "get", explode, raising=False)
    monkeypatch.setattr(httpx, "post", explode, raising=False)
    monkeypatch.setattr(httpx, "request", explode, raising=False)

    grafana_map.dashboards.cache_clear()
    grafana_map.totals()
    grafana_map.dashboards()
    grafana_map.alerting()
    grafana_map.operations()
    grafana_map.stack()
