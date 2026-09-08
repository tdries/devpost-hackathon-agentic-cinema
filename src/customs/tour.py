"""The tour: thirteen slides, then a walk through the real thing.

A first-time visitor met two doors and a paragraph. Either they already knew
what ad clearance was, or the product was a mystery with a colour scheme.

Two halves, deliberately different jobs:

SLIDES (this module's `slides`) are a carousel: a splash, then walkthrough
and value screens that say what the thing is and why anyone would want it.
They are allowed to be bold, because nobody has to read them twice.

THE WALK (`WALK`) is the same story told on the live console. Each stop
names a real page, a real element on it and one sentence about that
element; the page dims, the element is spotlit, and Next goes to the next
stop -- which is usually on another screen, so the tour drives the app.
That is the half that cannot be faked: a spotlight over a real market room
is the market room.

Every number a slide states is read from this instance at render time. A
tour that says "98 jurisdictions" while the packs say otherwise is a
brochure, and this project has spent two days making sure nothing on
screen is a brochure.
"""
from __future__ import annotations

# Where the walk stops, in order. `at` is the element to spotlight, named
# with a data-tour hook rather than a CSS class so a stylesheet change
# cannot silently break the tour. Several hooks separated by "|" are a
# fallback chain: half these elements only exist when there is something
# to put in them (a guard refusal, a live Grafana panel), and a stop that
# points at a hook this instance does not have should spotlight the next
# best thing rather than nothing.
#
# `{run}` and `{market}` are filled in by the app with a clearance that is
# finished and interesting and a room in it that has something to show.
WALK = [
    {"path": "/runs", "at": "archive-start|archive",
     "title": "Every clearance, newest first",
     "body": "One card per film. The pinned one is the run that shows the "
             "whole loop end to end, which is where the tour is going."},
    {"path": "/runs", "at": "card-viz|archive-list",
     "title": "Every card charts its own film, twice",
     "body": "Grafana draws the live panel; this console draws the same "
             "answer as SVG. The icons down the side are the axis Grafana "
             "cannot draw, one per category, six on every card."},
    {"path": "/runs/{run}", "at": "verdict",
     "title": "The verdict, and what is holding it",
     "body": "Not a score. A market either takes this commercial or it "
             "does not, and the ones that do not are named."},
    {"path": "/runs/{run}", "at": "ladder",
     "title": "The whole ladder, one strip",
     "body": "A global baseline, the EU, the countries, and each "
             "country's broadcasters folded behind it. A channel inherits "
             "everything above it, so BE-VRT is judged on rules nobody "
             "wrote in one place."},
    {"path": "/runs/{run}/markets/{market}", "at": "guard|market",
     "title": "What it refuses to do",
     "body": "A rule written on a protected characteristic is not "
             "something this system edits its way around. It names the "
             "problem, cites the statute, and hands it to a person."},
    {"path": "/runs/{run}/markets/{market}", "at": "takeaway",
     "title": "What you take away",
     "body": "The certificate is every finding with its statute and live "
             "citation, as a PDF. The markers are the same spans as "
             "timecodes for Resolve or Premiere, coloured by severity."},
    {"path": "/runs/{run}/markets/{market}", "at": "finding|market",
     "title": "One objection, and the three things it is made of",
     "body": "One observation the analyst recorded, one market's rule, and "
             "a citation retrieved at judging time. Never a model's "
             "opinion on its own."},
    {"path": "/runs/{run}/cutting", "at": "cutting|cutting-pair|cutting-room",
     "title": "Before, after, and what moved",
     "body": "The original and the localized master, side by side, with "
             "the analyst's own sentence on either side of the edit and how "
             "much of the shot the fix disturbed around itself."},
    {"path": "/runs/{run}/timeline", "at": "matrix",
     "title": "Where in the film it goes wrong",
     "body": "Categories down the side, the film's own seconds across the "
             "top, and Grafana's squares in between. Click one and the fix "
             "starts on that scene."},
    {"path": "/insight", "at": "filmstrip",
     "title": "Across every clearance",
     "body": "One block per commercial, lit to the worst severity any "
             "market ever recorded against it, with that film's own first "
             "frame underneath. Grafana holds the numbers; this console "
             "holds the footage."},
    {"path": "/grafana", "at": "inventory",
     "title": "Grafana is upstream, not a report",
     "body": "Nine dashboards, three alert rules and every metric the crew "
             "writes, read from the definitions it provisions from. One of "
             "those rules exists to tell the fix loop to stop."},
    {"path": "/agent", "at": "agent-ask|agent",
     "title": "Or say it in sentences",
     "body": "The same console, with an agent in front of it. Every screen "
             "you have just walked is one of its tools, so what it tells "
             "you is what you would have found by clicking, and it opens "
             "that thing beside its answer."},
]


def slides(*, packs: int, dimensions: int, rules: int, pairings: int,
           dashboards: int, panels: int, series: int, alert_rules: int,
           runs: int, findings: int, budget: float, stops: int,
           tools: int) -> list[dict]:
    """The carousel, with this instance's own numbers in it.

    `kind` drives the layout: splash is the cover, value sells, walkthrough
    teaches, proof shows a thing that actually happened. `art` names what
    the slide draws beside its words.
    """
    return [
        {"id": "splash", "kind": "splash", "art": "logo",
         "eyebrow": "Agentic Cinema  ·  Grafana Labs track",
         "title": "The Media Customs",
         "lede": "Localize your video for every geography and culture, "
                 "before it ships.",
         "body": "An AI crew watches your commercial once, then judges it "
                 "in parallel against every market you ship to, on both "
                 "counts: the law, and the cultural ground it has to land "
                 "on. Gesture, modesty, religious symbols, superstition, "
                 "humour, politics. It builds its own Grafana instrument "
                 "panel as it goes, and re-renders the shots that fail.",
         "stat": f"{packs} jurisdictions  ·  {rules} rules  ·  "
                 f"{dimensions} things watched for"},

        {"id": "problem", "kind": "value", "art": "world",
         "eyebrow": "The problem",
         "title": "Launching a video around the world runs into law "
                  "and culture",
         "lede": "The same thirty seconds is legal in one country and "
                 "unairable in the next.",
         "body": "The same thirty seconds is legal in one country, "
                 "unairable in the next, and fine everywhere except on one "
                 "broadcaster. Today that answer arrives as a fortnight of "
                 "email between an agency, a lawyer and a compliance desk, "
                 "and it arrives per market.",
         "stat": f"{pairings:,} market-rule pairings once inheritance "
                 f"resolves"},

        {"id": "observe", "kind": "walkthrough", "art": "observe",
         "eyebrow": "How it works  ·  1",
         "title": "The film is watched once, then judged market by market",
         "lede": "One AI pass writes down what is on screen. No verdicts, "
                 "on purpose.",
         "body": "One multimodal pass per shot writes neutral, timecoded "
                 "observations under a fixed taxonomy: what is on screen, "
                 "what is said, who is present, what is flashing. No "
                 "verdicts. Every market then judges that one fact sheet, "
                 "which is why two markets can disagree about the same "
                 "second without the film being watched twice.",
         "stat": f"{dimensions} dimensions, from alcohol to superstition"},

        {"id": "ladder", "kind": "walkthrough", "art": "ladder",
         "eyebrow": "How it works  ·  2",
         "title": "A channel is judged on its own rules, its country's "
                  "and its continent's",
         "lede": "Rules are inherited, so nobody writes the same rule "
                 "twice.",
         "body": "Drop a YAML file in, declare its parent, and write only "
                 "what your jurisdiction says differently. A Flemish "
                 "channel is judged against rules nobody wrote in one "
                 "place, and adding a market is a file rather than a "
                 "release.",
         "stat": "global  ·  continental  ·  national  ·  channel"},

        {"id": "join", "kind": "walkthrough", "art": "join",
         "eyebrow": "How it works  ·  3",
         "title": "Every objection names a real rule and quotes the law "
                  "behind it",
         "lede": "One observation, one market's rule, one citation "
                 "retrieved while judging.",
         "body": "Never a model saying something looks risky. A finding "
                 "exists because the analyst recorded a fact, a named rule "
                 "in a real rulebook covers that fact, and a grounded "
                 "search returned the statute that says so. Class and "
                 "severity are decided in code, not by the model.",
         "stat": "every rule names a real statute or code"},

        {"id": "guard", "kind": "proof", "art": "guard",
         "eyebrow": "What it will not do",
         "title": "It will not edit your ad to get around a rule about "
                  "who is in it",
         "lede": "The Guard is a rule layer with no model in it, so it "
                 "cannot be talked round.",
         "body": "Where a rule is written on a protected characteristic, "
                 "this system refuses to edit the footage to satisfy it. It "
                 "names the problem, cites the regulation and hands it to a "
                 "person, who can approve a manual edit, send it to legal "
                 "review, or accept the risk with a reason that is recorded "
                 "everywhere the finding is. It reads rule metadata only, "
                 "so no crafted finding can argue its way past it.",
         "stat": "a refusal is a feature, and it is on the certificate"},

        {"id": "fix", "kind": "walkthrough", "art": "clips",
         "eyebrow": "How it works  ·  4",
         "title": "Five ways to fix it, priced before you press",
         "lede": "Wine becomes juice. The footage stays yours.",
         "body": "A patch on a locked-off shot, a relight propagated "
                 "across a moving one, every frame repainted, the span "
                 "rewritten by Gemini Omni, or both ends edited and Veo "
                 "generating the motion between them. Every method carries "
                 "its price in euro before anything runs.",
         "stat": f"{budget:.0f} EUR a day, and the loop can be told to stop"},

        {"id": "verify", "kind": "proof", "art": "verify",
         "eyebrow": "The part nobody demos",
         "title": "It refuses to sign off a fix that did not work",
         "lede": "Asked to replace a cigar, the model produced a cigarette.",
         "body": "That happened on this instance. The verifier re-observed "
                 "the shot with the same instrument that found the problem, "
                 "the tobacco rule fired again, and the finding went back to "
                 "open with the market still blocked. A craft gate separately "
                 "refuses any master that lost frames, resolution or "
                 "soundtrack. Nothing is trusted because a model said it "
                 "was done.",
         "stat": "the fix loop closes, or it says why it did not"},

        {"id": "grafana", "kind": "walkthrough", "art": "grafana",
         "eyebrow": "How it works  ·  5",
         "title": "The charts are not a report at the end, they start "
                  "the work",
         "lede": "The crew builds its own Grafana while it works.",
         "body": "The Publisher agent builds its own instrument panel "
                 "through the official Grafana MCP server: dashboards, an "
                 "annotation per finding, and the alert rules that will "
                 "later wake the Remediator. Metrics go to Mimir on the "
                 "film's own clock, so a panel's x axis IS the timecode.",
         "stat": f"{dashboards} dashboards  ·  {panels} panels  ·  "
                 f"{series} series  ·  {alert_rules} alert rules"},

        {"id": "loop", "kind": "proof", "art": "loop",
         "eyebrow": "The loop",
         "title": "An alert wakes the crew, not a person",
         "lede": "Grafana fires; the Remediator answers.",
         "body": "A blocking finding pushes a metric, the alert rule on it "
                 "fires, and the webhook wakes the Remediator with the "
                 "finding in hand. It fixes the span, the Verifier "
                 "re-observes, the metric drops and Grafana resolves its "
                 "own alert. A third rule watches the day's budget and "
                 "tells that loop to stop before it spends the card.",
         "stat": "one rule to start the work, one to stop it"},

        {"id": "agent", "kind": "walkthrough", "art": "agent",
         "eyebrow": "How it works  ·  6",
         "title": "Or ask for the whole console in sentences",
         "lede": "Agent mode is not a chatbot bolted on the side.",
         "body": "The same functions the screens call are this agent's "
                 "tools, so it cannot answer out of its own summary of a "
                 "summary: it reads the store the market room reads, and "
                 "it obeys the same refusals: the Guard still blocks a "
                 "protected-basis fix, and the budget still governs Veo. Ask a "
                 "question nobody wrote a screen for and it writes the "
                 "LogQL, builds the dashboard, and opens what it found "
                 "beside its answer.",
         "stat": f"{tools} tools, and every one of them is the product"},

        {"id": "takeaway", "kind": "value", "art": "takeaway",
         "eyebrow": "What you take away",
         "title": "You leave with a signed-off decision and a cut that "
                  "can air",
         "lede": "Files, not screens: a PDF, a marker list and the master "
                 "itself.",
         "body": "The clearance certificate is every finding for one "
                 "market with its statute, its live citation, the fix that "
                 "was applied and the verifier's own words, as a PDF. The "
                 "marker export is the same spans as timecodes for Resolve "
                 "or Premiere, coloured by severity, with the statute in "
                 "the note. Plus the localized master itself.",
         "stat": "certificate PDF  ·  markers CSV and CMX3600  ·  master"},

        {"id": "intelligence", "kind": "value", "art": "intelligence",
         "eyebrow": "Across every run",
         "title": "Thirty clearances show what your creative keeps "
                  "getting wrong",
         "lede": "One run tells you whether one commercial ships.",
         "body": "Thirty tell you what your creative keeps doing wrong, "
                 "which jurisdictions are actually hard, and which rule "
                 "your next shoot should design around. Read straight out "
                 "of the two stores the crew wrote during those runs.",
         "stat": f"{runs} runs and {findings} findings on this instance "
                 f"right now"},

        {"id": "walk", "kind": "cta", "art": "walk",
         "eyebrow": "Enough slides",
         "title": "Now walk the real thing",
         "lede": f"{stops} stops, on the live console.",
         "body": "The tour drives the app from here: it opens each screen, "
                 "points at the thing worth looking at and says one "
                 "sentence about it. Nothing below is a screenshot.",
         "stat": ""},
    ]
