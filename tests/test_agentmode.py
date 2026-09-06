

def test_chart_spec_maps_instant_and_range_the_way_each_datasource_spells_it():
    """The one trap. Prometheus spells "one value now" as instant/range
    booleans; Loki spells it queryType. Get it wrong and a barchart draws a
    time series instead of bars."""
    from customs import agentmode

    spec = agentmode.chart_spec("t", [
        {"type": "barchart", "source": "loki", "expr": "x", "instant": True},
        {"type": "timeseries", "source": "loki", "expr": "y", "instant": False},
        {"type": "stat", "source": "prom", "expr": "z", "instant": True},
        {"type": "timeseries", "source": "prom", "expr": "w", "instant": False},
    ])
    loki_bar, loki_ts, prom_stat, prom_ts = (p["targets"][0] for p in spec["panels"])

    assert loki_bar["queryType"] == "instant"
    assert "queryType" not in loki_ts
    assert prom_stat["instant"] is True and prom_stat["range"] is False
    assert prom_ts["instant"] is False and prom_ts["range"] is True

    # two per row, and the datasource follows the source name
    assert [p["gridPos"]["x"] for p in spec["panels"]] == [0, 12, 0, 12]
    assert spec["panels"][0]["datasource"]["uid"] == "grafanacloud-logs"
    assert spec["panels"][2]["datasource"]["uid"] == "grafanacloud-prom"


def test_charts_fill_the_space_and_wear_the_app_colours():
    """A single chart should not sit in half a pane, and no chart should
    arrive in Grafana's own palette -- that reads as a different product."""
    from customs import agentmode, state

    one = agentmode.chart_spec("t", [{"type": "timeseries", "expr": "x"}])
    assert one["panels"][0]["gridPos"] == {"h": 24, "w": 24, "x": 0, "y": 0}

    four = agentmode.chart_spec("t", [{"type": "barchart", "expr": "x"}] * 4)
    assert all(p["gridPos"]["w"] == 12 and p["gridPos"]["h"] == 12
               for p in four["panels"])
    assert {p["gridPos"]["y"] for p in four["panels"]} == {0, 12}

    hues = [p["fieldConfig"]["defaults"]["color"]["fixedColor"]
            for p in four["panels"]]
    assert hues == [state.SIGNAL, state.BLOCKED, state.AT_RISK, state.CLEARED]
    assert all(p["fieldConfig"]["defaults"]["color"]["mode"] == "shades"
               for p in four["panels"]), "shades gives each series a tint of the hue"


def test_every_turn_ends_with_somewhere_to_go():
    """Agent mode opened with seven suggestions and then never changed
    them: from the second message on the operator faced a blank box and
    had to invent the next question, and an invented question is usually
    one the console cannot answer.

    Every turn now carries a rail of next moves, derived from the tools
    the turn actually called, so it is about what is on screen. Never
    empty -- a greeting gets the default rail -- and every entry is a
    sentence this agent can act on."""
    from customs.agentmode import Turn, follow_ups

    plain = follow_ups(Turn(reply="Hello."))
    assert plain, "even a greeting ends with somewhere to go"
    assert all(m["say"] and m["label"] and m["icon"] for m in plain)

    # what it just did leads, and the default rail fills the rest
    room = follow_ups(Turn(reply="x", calls=[
        {"tool": "list_runs"}, {"tool": "show", "args": {"view": "market"}}]))
    assert room[0]["say"].startswith("What is the statute"), \
        "the market room it just opened is what the operator is looking at"
    assert len({m["say"] for m in room}) == len(room), "no repeats"
    assert len(room) <= 4

    # a failed turn offers the two questions that get it unstuck
    broken = follow_ups(Turn(error="no such run"))
    assert broken[0]["label"] == "List the runs"
