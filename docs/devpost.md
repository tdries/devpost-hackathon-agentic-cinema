# Devpost submission copy

Paste-ready. Track: Grafana Labs.

## Tagline (under 200 chars)

Upload a commercial, tick 98 markets, and an AI crew tells you which shot breaks which law, where it's written, then fixes the shot when a Grafana alert says to.

## Project story

### Inspiration

I've spent enough time around people who ship commercials to know how market clearance actually works. The agency cuts a master. Someone in legal checks it for the home market, maybe the two or three biggest others. Then the same file goes out to twenty countries with a different end card, and everyone hopes.

Most of the time it's fine. When it isn't, it's expensive in one of two ways. Either a regulator or a broadcaster bounces it (France won't take alcohol on TV, Clearcast wants to see every spot before it airs in the UK, Quebec won't let you advertise to kids), and now you're re-cutting three days before the flight date. Or nobody bounces it, it airs, and you find out from Twitter that the gesture in shot four means something else in the Gulf, or that the ad reads completely differently in China. Pepsi, Dolce & Gabbana, H&M. None of those were caught by a process, because there isn't one. There's a checklist in someone's head and a lawyer you can call.

I wanted to see if an AI crew could do the boring version of that job properly: watch the film once, check it against every market you're shipping to at the same time, tell you exactly which shot breaks which rule and where the rule is written, and then actually fix the shot if you let it.

The Grafana angle came from noticing that a clearance run is basically telemetry. Risk goes up and down along the timecode. Every finding is a log line with a statute attached. And "this is about to air somewhere it can't" is an alert. So instead of building yet another dashboard inside the app, I made Grafana the place where the run lives.

### What it does

You upload the spot and tick the markets. Then you watch it go through.

The crew watches the film once and writes down what's in it, in plain terms: a woman raises a glass of red wine, a man in shorts, a flag on the wall, text that says "clinically proven". No opinions at this stage. Then one adjudicator per market picks up that list and checks it against that market's rules. I've built 21 rule packs covering 98 jurisdictions, from a global baseline down through the EU, sixteen countries and eighty individual broadcasters. Every rule points at a real law or broadcaster code, and every finding comes back with a live link to it, found through Google Search at the moment of judging. Market tiles go from pending to cleared, at risk or blocked as the verdicts come in.

Behind that, the crew builds its own Grafana dashboards for the run: risk per market along the timecode, every finding as an annotation, and alert rules. When Grafana fires an alert on a blocking finding, it calls back into the app and a remediator has a go at the shot. There are five ways to fix a span, from a cheap one-frame patch up to Gemini Omni rewriting the whole shot as video, or Veo 3.1 generating new motion between two edited frames -- and for those two you can type what you want to happen, which is added to the instruction the model gets. Then a verifier runs the same analysis again on the new footage. If the problem is gone and nothing new broke, the metric drops and Grafana closes its own alert.

What you get out is what a clearance desk would actually hand over: a decision per market with the evidence, a localised master, a clearance certificate as a PDF with every finding and statute on it, and marker files (CSV and CMX3600 EDL) so an editor can drop the findings straight onto their timeline in Resolve or Premiere, colour-coded, with the reason in the note.

Two places where it stops itself. If a rule is about a protected characteristic, say the Saudi or UAE rules on showing same-sex couples, it won't auto-edit. It names the rule and hands it to a person. And there's a daily budget. Once the day's remaining money in Mimir drops below the price of the most expensive fix, an alert pauses automatic remediation so the thing can't burn through the card overnight while nobody's watching.

### How I built it

The main idea is: look at the film once, judge it many times. Looking is expensive. Opinions are cheap.

A Gemini vision model does the looking, shot by shot, and is only allowed to describe what it sees under a fixed set of 18 categories (alcohol and tobacco, modesty and dress, gestures, religious symbols, children, health claims, and so on). One adjudicator agent per market, running in parallel on Google Cloud Agent Builder (ADK), takes those descriptions and matches them to its YAML rule pack, then grounds each citation through Google Search. A finding is always: this observation, this rule, this link. That split turned out to matter a lot. If a finding is wrong, you can tell straight away whether the model saw something that wasn't there or whether the rule is badly written.

The whole crew is one ADK sequential agent: ingest, analyst, adjudicators, guard, publisher. The publisher is the properly agentic bit. It has five tools, three of them Grafana MCP calls, and it decides itself what to build, reads what came back, handles failures, and writes the overview text into the dashboard description. That's a real MCP write on every run, in its own words.

Google and Grafana are wired together in three directions, and all three are running on the live instance:

| Direction | What happens |
|---|---|
| The crew writes into Grafana | Metrics go to Mimir over OTLP, including per-market risk on the film's own clock. Observations, findings and verdicts go to Loki with market and rule id as labels, so "all the French findings" is a label filter. One annotation per finding. Dashboards and alert rules are created through the official Grafana MCP server. |
| Grafana drives the crew | Alert rules evaluate every 30 seconds. When a blocking score crosses the line for a given asset, market and rule, Grafana posts to a webhook on the Cloud Run service, and that's what wakes the remediator. A Grafana alert is what starts a Veo render. |
| A click in Grafana starts a fix | The timeline grid on the launch board is half console, half Grafana. The app draws the category icons and scene thumbnails as the axes; the squares in between are a live Grafana status-history panel with its own axes hidden. Click a square and a data link fires a fix on that scene. |

A few things fell out of having everything in Loki and Mimir. Every caption the analyst ever wrote is a log line, so frame search is just asking Gemini to match your question against those captions across every run. An intelligence board reads across all runs: which subjects your creative keeps tripping on, which markets are actually hard. And every Gemini call reports its token usage with OpenTelemetry's standard GenAI attributes, by job, so "what did the citations cost us" is a query.

On the Google side: Gemini for vision, text, grounding, image editing and TTS, Veo 3.1 for generating bridge footage, Gemini Omni for video-to-video, embeddings for the frame index, all on Vertex AI, deployed on Cloud Run with state mirrored to Cloud Storage. The test commercials were made with Veo and Omni too, on purpose, with known problems planted in them. Google's tools made the ad and then failed it.

The console is FastAPI with server-sent events, no frontend build. Grafana Cloud won't let you iframe it, so the live panels come from a stock Grafana OSS viewer that has no data of its own and reads the same Cloud Loki and Mimir the crew writes to, read-only, locked to the crew's labels.

### Challenges I ran into

**Prometheus won't take old timestamps**, and a film's data lives on the film's clock. So each run is mapped onto the wall clock at the moment it starts:

$$t_{	ext{wall}} = t_0 + n$$

Wall second $t_0 + n$ is video second $n$, and the dashboard time range is pinned to the run. The x-axis reads as timecode because it is timecode. If you open the dashboards on "last 6 hours" you get No data, and everyone does that first.

**Grafana Cloud can't be embedded.** I tried fourteen URL variants with real browser headers. Every one comes back with `frame-ancestors 'none'`, and the admin setting to change it is locked on Cloud. That's why the self-hosted viewer exists. Its image renderer also won't go smaller than 1000 by 500, so anything card-sized is drawn by the app from a query instead.

**The order kept changing.** LogQL's `sort_desc` doesn't define what happens on ties. Same query, five runs, five different orders for seven films with the same score. The console and the panel are two separate executions, so I couldn't trust either. Fixed with stable sort transformations in Grafana and a matching sort in Python, plus the detail that Grafana compares strings case-insensitively and Python doesn't.

**A 43-second cigarette ad cleared the EU clean.** Shot detection found two shots, the sampler took two frames per shot, and the whole ad was judged on four stills. Frame count now scales with duration. Same file, re-run: blocked, tobacco rule firing.

**You can't patch a wine bar.** Six prop swaps passed the technical check and the verifier threw every one out, because a bar full of people drinking isn't "a shot with a bottle in it". The planner now escalates to Omni when the problem runs through the whole scene. I also measured how much each method disturbs footage outside the thing you asked it to change: a patch is about 44 dB PSNR, Omni is anywhere from 28 down to 13. Omni re-renders the shot. "Only change what I named" is what you asked for, not what you get, and the console now says so.

**Model listings lie.** Models that show up in the list can 404 when you generate. Veo 3.1 only does 4, 6 or 8 seconds. The newer Omni preview sits behind a quota error that granting quota doesn't clear. Everything gets probed with a real call now.

**One thread per model call** took the container down twice mid-run. A clearance makes over a hundred Gemini calls; I'd shipped token reporting as a thread each. It's one worker draining a bounded queue now.

**The guard had to be un-promptable.** It reads two fields from the rule pack and the finding's class, never the model's reasoning, and never calls a model. If a prompt can grant it, a prompt can take it away.

### Accomplishments that I'm proud of

The loop closes on the live instance with nobody in it. Grafana alert, remediator edits the span, technical check on length, resolution, audio and drift, verifier re-watches with the same analyst that found the problem, metric drops, Grafana clears the alert. The showcase run in the archive is one spot cleared for eight markets across all four levels of the ladder: 63 findings, 28 verified fixes, 2 refusals handed to a human with the statute attached.

The Grafana dashboard is a control surface, not a report. The agent builds it, the alerts it wrote trigger generation, and clicking a panel spends real money on a Veo render, inside a budget that is itself a Mimir series with an alert on it.

Every rule in the 21 packs points at a real law or code, and a French alcohol finding comes back with a live Legifrance link. If a citation can't be resolved, the finding's severity is capped and it can't trigger a fix, so a hallucinated rule shows up as a visibly weaker finding instead of a wrong edit. The certificate puts all of that on one page a legal team would accept. And there are over 600 offline tests, including one that checks the filter for withheld footage is stamped into every dashboard.

### What I learned

Keep the watching and the judging apart. Once the analyst only says what's there and the adjudicators only say what each market thinks about it, findings get cheap, parallel, and arguable in a useful way.

Observability isn't something you bolt onto an agent system afterwards. When the metrics run on the film's clock, the logs carry the statute and the alert is the trigger, Grafana stops being where you watch the crew and becomes part of it.

Put guardrails in rules, not prompts. Give citations a failure mode. And measure what an edit actually changed, because the model's own account of its edit is a promise.

### What's next for The Media Customs

A market is a YAML file, not code, so adding one is writing rules down. The broadcaster level is where the packs are thinnest and where the real clearance pain sits. Campaign memory, so the crew can answer "has this brand tripped this rule before" from its own history. A review inbox for the decisions the guard hands to a person. And Omni over spans longer than ten seconds, with the sound carried through.

## Built with

python, fastapi, google-cloud-agent-builder, google-adk, vertex-ai, gemini, gemini-omni, veo, google-search-grounding, google-cloud-run, google-cloud-storage, grafana, grafana-cloud, grafana-mcp, mimir, loki, opentelemetry, ffmpeg, sqlite, reportlab

## Links

- GitHub repo: https://github.com/tdries/devpost-hackathon-agentic-cinema
- Live instance: https://customs-app-akap4ao72a-ew.a.run.app -- **nothing is gated for reading**. The archive, every run, every finding with its statute and citation, the rule library, frame search, the intelligence board and the Grafana inventory are all open: click "I'm a Devpost judge" and you are in. The pinned "Start here" run shows the whole loop end to end. The word DEVPOST is only needed to SPEND: starting a clearance, judging more markets or running a fix calls models on a real card, and the judge door lifts the visitor's EUR 1.00 a day cap on that
- Demo video: https://youtu.be/YOUTUBE_VIDEO_ID (record and replace)

## Track

Grafana Labs

## Team

Tim Dries (solo)
