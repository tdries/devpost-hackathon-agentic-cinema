# YouTube copy

## Title options

1. The Media Customs: an AI crew that clears your ad for 98 markets
2. I built an AI ad-clearance crew that fixes the shots it blocks
3. The Media Customs, built with Gemini, Veo and Grafana

## Description

One commercial, eight countries, and nobody can tell us where it is legal to air.
The Media Customs is an AI crew that watches your ad once, judges it against
every market in parallel, and then re-renders the shots that fail.

TRY IT
Live app, nothing gated for reading: https://customs-app-akap4ao72a-ew.a.run.app
Code: https://github.com/tdries/devpost-hackathon-agentic-cinema
Hackathon: https://agentic-cinema.devpost.com (Agentic Cinema, Grafana Labs track)

WHAT IT DOES
1. Watches the film once and writes neutral, timecoded observations
2. Judges those facts against 98 jurisdictions at the same time, from a global
   baseline down to individual broadcasters
3. Cites a real statute for every finding, resolved live with Google Search
4. Fixes the failing seconds with Gemini Omni or Veo 3.1, then verifies the fix
   with the same analyst that failed the shot
5. Hands back a decision per market, a localized master, a PDF clearance
   certificate, and marker files for Resolve or Premiere

CHAPTERS
0:00 One commercial, eight markets
0:28 The whole loop in one diagram
1:03 The archive, and starting a run
1:19 The crew at work, and the verdict per market
1:37 The timeline, live from Grafana
1:47 France, and the statute behind the finding
1:58 Fixing the shot, priced before you press
2:23 Agent mode
2:36 Rule library, frame search, intelligence
2:48 Everything it built in Grafana

WHY GRAFANA IS A PARTICIPANT, NOT A REPORT
The publisher agent builds its own dashboards and alert rules during the run,
through the Grafana MCP server. The alerts it wrote are what trigger generation:
a blocking finding trips a rule, the webhook wakes the remediator, and a fix
begins because a chart said so. Click a square on a live Grafana panel and it
starts a priced edit on that scene. When the fix verifies, Grafana resolves its
own alert.

THE NUMBERS
98 jurisdictions, 21 market packs, 128 rules, 18 taxonomy dimensions
9 dashboards, 40 panels, 3 alert rules, all built by the agent
5 fix methods, every one priced in euro before it runs
A daily budget that is itself a metric, with an alert that pauses the loop

BUILT WITH
Python, FastAPI, Google Cloud Agent Builder (ADK), Gemini, Gemini Omni, Veo 3.1,
Vertex AI, Grafana Cloud (Mimir, Loki, MCP), ffmpeg, SQLite, Cloud Run

NOTES
Everything on screen is the real deployed app, not a mockup. The commercial being
cleared was generated with Veo and rewritten with Gemini Omni, so the footage is
ours and the tools that made the ad are the ones that failed it.

#AI #Grafana #Gemini #Veo #GoogleCloud #AIAgents #AdTech #Observability
