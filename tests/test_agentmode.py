

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
    assert [p["gridPos"]["y"] for p in spec["panels"]] == [0, 0, 10, 10]
    assert spec["panels"][0]["datasource"]["uid"] == "grafanacloud-logs"
    assert spec["panels"][2]["datasource"]["uid"] == "grafanacloud-prom"
