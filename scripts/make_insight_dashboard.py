"""Write grafana/dashboards/insight.json: the cross-run intelligence board.

Everything else this project provisions answers a question about ONE run.
Nothing answered the questions an operator asks across all of them -- which
dimension costs us the most, which market is hardest, which rule fires
everywhere, whether the citations hold up -- and those are the questions a
clearance desk actually has.

Built in the tradition Stephen Few argued for, which is mostly a list of
refusals: no pie charts, no 3D, no gauges as decoration, no colour that
does not carry meaning. Bars are sorted by the quantity they encode so the
ranking IS the picture. Colour is one hue for quantity and three for the
one thing where category matters (legal, policy, offence). The matrix is a
table lens, coloured by value, because a categorical 2-D comparison reads
better as a table than as anything Grafana calls a heatmap. The one bullet
graph is Few's own instrument, measuring severity against the threshold
that actually blocks a market.

Written as a script rather than by hand because twelve panels of Grafana
JSON is 900 lines nobody can review, and because the panel ids have to
stay stable: the console embeds them by id.

    python scripts/make_insight_dashboard.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "grafana" / "dashboards" / "insight.json"

LOKI = {"type": "loki", "uid": "grafanacloud-logs"}
PROM = {"type": "prometheus", "uid": "grafanacloud-prom"}

# Google's four, used the way the console uses them: blue is the neutral
# quantity, and the other three only appear where class is the subject.
BLUE, RED, YELLOW, GREEN = "#4285F4", "#EA4335", "#FBBC05", "#34A853"


def target(expr, datasource=LOKI, legend="", instant=False, **kw):
    t = {"refId": kw.pop("refId", "A"), "datasource": datasource, "expr": expr,
         "editorMode": "code"}
    if datasource is LOKI:
        t["queryType"] = "instant" if instant else "range"
    else:
        t["instant"] = instant
        t["range"] = not instant
    if legend:
        t["legendFormat"] = legend
    t.update(kw)
    return t


def panel(pid, kind, title, expr, x, y, w, h, *, description="",
          datasource=LOKI, options=None, defaults=None, overrides=None,
          targets=None, transformations=None, legend="", instant=False):
    p = {
        "id": pid, "type": kind, "title": title, "description": description,
        "datasource": datasource,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets or [target(expr, datasource, legend, instant)],
        "options": options or {},
        "fieldConfig": {
            "defaults": dict({"color": {"mode": "fixed", "fixedColor": BLUE},
                              "mappings": []}, **(defaults or {})),
            "overrides": overrides or [],
        },
    }
    if transformations:
        p["transformations"] = transformations
    return p


def text(pid, title, content, x, y, w, h):
    return {"id": pid, "type": "text", "title": title,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "markdown", "content": content}}


# --- the summary row: five plain numbers, no gauge, no sparkline ---------
# Few's objection to a dashboard's number row is never the numbers, it is
# the dials drawn around them. These are the numbers.

def stat(pid, title, expr, x, unit=""):
    return panel(
        pid, "stat", title, expr, x, 0, 4, 4, datasource=LOKI, instant=True,
        description="Across every clearance this instance has performed.",
        options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                   "values": False},
                 "textMode": "auto", "colorMode": "none",
                 "graphMode": "none", "justifyMode": "auto",
                 "wideLayout": True, "showPercentChange": False},
        defaults={"unit": unit, "decimals": 0})


def ordered(label: str) -> list[dict]:
    """Sort a panel by value descending, ties by name, inside Grafana.

    Every panel the console draws its own axis beside has the same
    exposure: LogQL's sort_desc gets the values right and says nothing
    about how a tie is broken, and the console runs its own execution of
    the query to lay that axis out. Five runs of the hero's expression
    came back in five different arrangements of the seven films sitting at
    severity 95, so the poster under a block was a coin toss with seven
    sides.

    Sorting here is the fix, because here it is defined. A Loki instant
    query arrives as one table -- a label column and `Value #A` -- so the
    two sorts chain on it directly, and sortBy is stable, which is what
    lets the name order survive inside each tie. The console applies the
    same two keys and both halves land on an order neither store promises.
    """
    return [{"id": "sortBy", "options": {"sort": [{"field": label}]}},
            {"id": "sortBy", "options": {"sort": [{"field": "Value #A",
                                                   "desc": True}]}}]


# --- the hero: one block per commercial, the LCD read as a shelf --------
# Every other panel here aggregates. This one enumerates: one bar per film
# this instance has ever judged, lit to its worst recorded severity, in the
# console's own three states -- green cleared, blue noted, red blocking.
# The console draws each film's own poster underneath as the axis, which is
# the whole icon-and-live-Grafana idea taken as far as it goes: Grafana has
# the numbers and has never seen the footage; the console has the footage
# and cannot chart anything live.
#
# Vertical bars arranged left to right, which is what Grafana calls
# orientation "vertical" and what a person calls a row of blocks.
_HERO = panel(
    60, "bargauge", "Every commercial, at its worst moment",
    'sort_desc(max by (asset) (max_over_time({app="customs", kind="finding"} '
    '| json | unwrap severity [$__range])))',
    0, 0, 24, 8, instant=True,
    description="One block per film, lit to the highest severity any market "
                "ever recorded against it. Green cleared, blue noted, red "
                "past 70 -- the line where a finding starts to block a "
                "market. The posters under the blocks are the console's.",
    legend="{{asset}}",
    options={"displayMode": "lcd", "orientation": "vertical",
             "valueMode": "color", "showUnfilled": True,
             "minVizWidth": 8, "minVizHeight": 16, "maxVizHeight": 300,
             "namePlacement": "auto", "sizing": "auto",
             "reduceOptions": {"calcs": [], "fields": "", "values": True}},
    defaults={"min": 0, "max": 100, "decimals": 0,
              "color": {"mode": "thresholds"},
              "thresholds": {"mode": "absolute", "steps": [
                  {"color": GREEN, "value": None},
                  {"color": BLUE, "value": 40},
                  {"color": RED, "value": 70}]}},
    # sort_desc gets the values right and the order only mostly: LogQL
    # says nothing about how a tie is broken, and five executions of this
    # exact query came back in five different arrangements of the seven
    # films sitting at 95. The console runs its own execution to lay the
    # posters out, so "the poster under the block" was a coin toss with
    # seven sides. Sorting happens here instead, where it is defined:
    # reduce the series to rows, order them by name, then by value
    # descending -- a stable sort, so the name order survives inside each
    # tie. The console applies the same two keys and both halves land on
    # an order neither store promises.
    transformations=ordered("asset"),
    )

PANELS = [
    _HERO,
    stat(1, "Findings", 'sum(count_over_time({app="customs", kind="finding"}[$__range]))', 0),
    stat(2, "Commercials judged",
         'count(sum by (asset) (count_over_time({app="customs", kind="observation"}[$__range])))', 4),
    stat(3, "Markets exercised",
         'count(sum by (market) (count_over_time({app="customs", kind="finding"}[$__range])))', 8),
    stat(4, "Rules tripped",
         'count(sum by (rule_id) (count_over_time({app="customs", kind="finding"}[$__range])))', 12),
    stat(5, "Frames observed",
         'sum(count_over_time({app="customs", kind="observation"}[$__range]))', 16),
    stat(6, "Adjudications",
         'sum(count_over_time({app="customs", kind="verdict"}[$__range]))', 20),

    # --- what the world objects to -------------------------------------
    # Sorted horizontal bars: the ranking is the whole point, and reading
    # eighteen taxonomy names is easier down the side than along the bottom.
    panel(10, "barchart", "What gets objected to, every run",
          'sort_desc(sum by (dimension) (count_over_time({app="customs", '
          'kind="finding"}[$__range])))',
          0, 4, 12, 10, instant=True,
          description="Findings by dimension of the taxonomy, across every "
                      "run. Sorted by count: the ranking is the finding.",
          legend="{{dimension}}",
          options={"orientation": "horizontal", "xTickLabelRotation": 0,
                   "showValue": "always", "stacking": "none",
                   "groupWidth": 0.7, "barWidth": 0.8, "fullHighlight": False,
                   "legend": {"showLegend": False, "displayMode": "list",
                              "placement": "bottom", "calcs": []},
                   "tooltip": {"mode": "single", "sort": "none"}},
          defaults={"custom": {"lineWidth": 0, "fillOpacity": 85,
                               "axisBorderShow": False, "gradientMode": "none",
                               "axisLabel": "", "axisPlacement": "auto"}},
          transformations=ordered("dimension"),),

    panel(11, "barchart", "Which markets object most",
          'sort_desc(sum by (market) (count_over_time({app="customs", '
          'kind="finding"}[$__range])))',
          12, 4, 12, 10, instant=True,
          description="Findings by market. A tall bar is a hard jurisdiction, "
                      "not a bad commercial.",
          legend="{{market}}",
          options={"orientation": "horizontal", "xTickLabelRotation": 0,
                   "showValue": "always", "stacking": "none",
                   "groupWidth": 0.7, "barWidth": 0.8, "fullHighlight": False,
                   "legend": {"showLegend": False, "displayMode": "list",
                              "placement": "bottom", "calcs": []},
                   "tooltip": {"mode": "single", "sort": "none"}},
          defaults={"color": {"mode": "fixed", "fixedColor": RED},
                    "custom": {"lineWidth": 0, "fillOpacity": 85,
                               "axisBorderShow": False, "gradientMode": "none"}},
          transformations=ordered("market"),),

    # --- the matrix ----------------------------------------------------
    # A table lens: dimension down, market across, cell coloured by count.
    # Grafana's heatmap panel wants time on x; a categorical matrix is a
    # table, and colouring its cells is what makes it readable at a glance.
    panel(20, "table", "Dimension against market: where the friction is",
          'sum by (dimension, market) (count_over_time({app="customs", kind="finding"}[$__range]))',
          0, 14, 24, 12, instant=True,
          description="Every objection this instance has recorded, as a "
                      "matrix. Read a row to see which jurisdictions care "
                      "about one subject; read a column for what one market "
                      "is strict about.",
          options={"showHeader": True, "cellHeight": "sm",
                   "footer": {"show": False, "reducer": ["sum"], "countRows": False,
                              "fields": ""},
                   "sortBy": []},
          defaults={"custom": {"align": "center", "cellOptions": {
              "type": "color-background", "mode": "gradient"},
              "inspect": False, "filterable": False},
              "color": {"mode": "continuous-BlPu"}, "decimals": 0},
          overrides=[{"matcher": {"id": "byName", "options": "dimension"},
                      "properties": [{"id": "custom.align", "value": "left"},
                                     {"id": "custom.cellOptions",
                                      "value": {"type": "auto"}},
                                     {"id": "custom.width", "value": 230}]}],
          transformations=[
              {"id": "reduce", "options": {"reducers": ["lastNotNull"]}},
              {"id": "extractFields", "options": {"source": "Field",
                                                  "format": "auto"}},
          ]),

    # --- severity ------------------------------------------------------
    panel(30, "histogram", "How severe, across every finding",
          'sum by (rule_id, market, asset) (max_over_time({app="customs", kind="finding"} '
          '| json | unwrap severity [$__range]))',
          0, 26, 9, 9, instant=True,
          description="The severity the adjudicators actually assign. The "
                      "shape matters more than any single bar: a clearance "
                      "desk that only ever sees 90s is not grading.",
          options={"bucketOffset": 0, "combine": False,
                   "legend": {"showLegend": False, "displayMode": "list",
                              "placement": "bottom", "calcs": []}},
          defaults={"custom": {"lineWidth": 0, "fillOpacity": 80,
                               "gradientMode": "none"},
                    "color": {"mode": "fixed", "fixedColor": YELLOW}}),

    # Few's own instrument: a measure against the threshold that matters.
    # 70 is where a finding starts blocking a market (see state.py), so the
    # bar is read against that line rather than against the other bars.
    panel(31, "bargauge", "Worst severity per market, against the blocking line",
          'sort_desc(max by (market) (max_over_time({app="customs", '
          'kind="finding"} | json | unwrap severity [$__range])))',
          9, 26, 9, 9, instant=True,
          description="70 is where a finding begins to block a market. A bar "
                      "past the marker is a market this instance has actually "
                      "held a campaign out of.",
          legend="{{market}}",
          options={"displayMode": "basic", "orientation": "horizontal",
                   "valueMode": "color", "showUnfilled": True,
                   "minVizWidth": 8, "minVizHeight": 10, "maxVizHeight": 300,
                   "namePlacement": "auto", "sizing": "auto",
                   "reduceOptions": {"calcs": [], "fields": "", "values": True}},
          defaults={"min": 0, "max": 100, "decimals": 0,
                    "color": {"mode": "thresholds"},
                    "thresholds": {"mode": "absolute", "steps": [
                        {"color": GREEN, "value": None},
                        {"color": YELLOW, "value": 40},
                        {"color": RED, "value": 70}]}}),

    panel(32, "piechart", "", "", 18, 26, 6, 9),  # replaced below

    # --- over time -----------------------------------------------------
    panel(40, "timeseries", "Objections per day, by class",
          'sum by (klass) (count_over_time({app="customs", kind="finding"}[1d]))',
          0, 35, 16, 9,
          description="Legal is a statute, policy is a broadcaster's own "
                      "rule, offence is taste. Only class earns colour here.",
          legend="{{klass}}",
          options={"legend": {"showLegend": True, "displayMode": "list",
                              "placement": "bottom", "calcs": []},
                   "tooltip": {"mode": "multi", "sort": "desc"}},
          defaults={"custom": {"drawStyle": "line", "lineWidth": 2,
                               "fillOpacity": 12, "showPoints": "auto",
                               "pointSize": 5, "axisBorderShow": False,
                               "gradientMode": "none", "spanNulls": False,
                               "stacking": {"group": "A", "mode": "none"}}},
          overrides=[
              {"matcher": {"id": "byName", "options": "legal"},
               "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": RED}}]},
              {"matcher": {"id": "byName", "options": "policy"},
               "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": YELLOW}}]},
              {"matcher": {"id": "byName", "options": "offence"},
               "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": BLUE}}]},
          ]),

    panel(41, "barchart", "Adjudications: triggered against cleared",
          'sort_desc(sum by (verdict) (count_over_time({app="customs", '
          'kind="verdict"}[$__range])))',
          16, 35, 8, 9, instant=True,
          description="Every rule the adjudicators considered, not only the "
                      "ones that fired. A desk that only records its hits "
                      "cannot say how much it looked at.",
          legend="{{verdict}}",
          options={"orientation": "horizontal", "showValue": "always",
                   "stacking": "none", "groupWidth": 0.7, "barWidth": 0.6,
                   "xTickLabelRotation": 0, "fullHighlight": False,
                   "legend": {"showLegend": False, "displayMode": "list",
                              "placement": "bottom", "calcs": []},
                   "tooltip": {"mode": "single", "sort": "none"}},
          defaults={"custom": {"lineWidth": 0, "fillOpacity": 85,
                               "axisBorderShow": False}},
          overrides=[
              {"matcher": {"id": "byName", "options": "triggered"},
               "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": RED}}]},
              {"matcher": {"id": "byName", "options": "cleared"},
               "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": GREEN}}]},
          ],
          ),

    # --- the specifics -------------------------------------------------
    panel(50, "table", "The rules that fire everywhere",
          'topk(15, sum by (rule_id, klass, market) '
          '(count_over_time({app="customs", kind="finding"}[$__range])))',
          0, 44, 12, 11, instant=True,
          description="Ranked by how often a rule has been tripped across "
                      "every commercial. This is the list a creative director "
                      "should read before the next shoot.",
          options={"showHeader": True, "cellHeight": "sm",
                   "footer": {"show": False, "reducer": ["sum"], "fields": ""},
                   "sortBy": [{"displayName": "Last *", "desc": True}]},
          defaults={"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                               "inspect": False}},
          transformations=[{"id": "reduce", "options": {"reducers": ["lastNotNull"]}},
                           {"id": "extractFields", "options": {"source": "Field"}}]),

    panel(51, "logs", "The finding stream itself",
          '{app="customs", kind="finding"} | json',
          12, 44, 12, 11,
          description="The raw lines every panel above is an aggregation of. "
                      "Nothing on this board is a fixture: it is this stream, "
                      "counted different ways.",
          options={"showTime": True, "showLabels": False, "showCommonLabels": False,
                   "wrapLogMessage": True, "prettifyLogMessage": False,
                   "enableLogDetails": True, "dedupStrategy": "none",
                   "sortOrder": "Descending"}),
]

# Panel 32 stands as a placeholder in the list above so the ids read in
# order there; the real one is a state timeline from Mimir rather than Loki.
# Replaced BY ID, never by index: counting list positions by hand put the
# state timeline over the matrix table on the first run of this script and
# left the placeholder in the board.
_STATE_TIMELINE = panel(
    32, "state-timeline", "Market status over time, from Mimir",
    'max by (market) (customs_market_status)', 18, 26, 6, 9,
    datasource=PROM, legend="{{market}}",
    description="0 cleared, 1 cleared with findings open, 2 blocked. The "
                "only panel here that reads the metric store: a status is a "
                "gauge that changes, which is what Mimir is for, while a "
                "finding is an event with a body, which is what Loki is for.",
    options={"mergeValues": True, "showValue": "never", "alignValue": "left",
             "rowHeight": 0.9, "perPage": 20,
             "legend": {"showLegend": False, "displayMode": "list",
                        "placement": "bottom", "calcs": []},
             "tooltip": {"mode": "single", "sort": "none"}},
    defaults={"custom": {"lineWidth": 0, "fillOpacity": 90,
                         "insertNulls": False, "spanNulls": False},
              "color": {"mode": "thresholds"},
              "mappings": [
                  {"type": "value", "options": {
                      "0": {"text": "cleared", "color": GREEN, "index": 0},
                      "1": {"text": "open findings", "color": YELLOW, "index": 1},
                      "2": {"text": "blocked", "color": RED, "index": 2}}}],
              "thresholds": {"mode": "absolute", "steps": [
                  {"color": GREEN, "value": None}]}})

PANELS[next(i for i, p in enumerate(PANELS) if p["id"] == 32)] = _STATE_TIMELINE

DASHBOARD = {
    "uid": "customs-insight",
    "title": "Customs: Intelligence",
    "description": "Every clearance this instance has performed, read across "
                   "runs rather than one at a time.",
    "tags": ["customs"],
    "timezone": "utc",
    "editable": True,
    "schemaVersion": 39,
    "refresh": "",
    "time": {"from": "now-30d", "to": "now"},
    "panels": PANELS,
}


def main() -> int:
    # The hero owns the top eight rows; every other panel was laid out
    # before it existed, so shift them rather than re-typing every gridPos.
    for p in PANELS:
        if p["id"] != 60:
            p["gridPos"]["y"] += 8
    OUT.write_text(json.dumps(DASHBOARD, indent=2) + "\n")
    kinds = sorted({p["type"] for p in PANELS})
    print(f"{OUT.relative_to(ROOT)}: {len(PANELS)} panels, "
          f"{len(kinds)} chart types ({', '.join(kinds)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
