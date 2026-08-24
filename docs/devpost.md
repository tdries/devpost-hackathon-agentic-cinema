# Devpost submission copy

Paste-ready. Track: Grafana Labs.

## Tagline (under 200 chars)

An AI crew that clears your commercial for 15 markets: it cites the statute behind every finding, builds its own Grafana instrument panel, and re-renders the shots that fail.

## Project story

### Inspiration

Global brands ship one commercial into dozens of markets and check maybe six of them. The regulated half already carries an invoice: France bans TV alcohol advertising under Loi Evin, Quebec bans ads aimed at children under 13, the UK pre-clears every TV ad through Clearcast, Nigeria requires ARCON vetting. The unregulated half costs more: Pepsi and Kendall Jenner, Dolce & Gabbana in China, H&M's hoodie. Every one of those was caught by the public rather than by a process. Pilots get a pre-flight checklist and an instrument panel. Ad launches get a consultant, a focus group and luck. We built the instrument.

### What it does

You upload a commercial and a market list, and you watch it clear customs in real time. A crew of agents watches the film once, records what is actually in it, judges those facts against each market's law, broadcaster code and cultural norms, and builds its own Grafana instrument panel from the result. Fifteen market tiles flip from pending to cleared, at risk or blocked as adjudicators return, each finding carrying the statute and a live citation link. When a Grafana alert fires, a Remediator re-renders the failing parts (re-lettering on-screen text in the market's language, swapping props, re-voicing lines) and a Verifier re-runs the same instrument on the changed shots before anything is trusted. The output is two things: a decision (can this air here, and on what evidence) and a localized master (the version that can). One more thing it deliberately does not do: when a rule targets a protected characteristic, like Saudi Arabia's prohibition on depicting same-sex couples, a rule-layer Guard refuses to auto-edit, names the problem, cites the regulation, and hands it to a human.

### How we built it

The core decision is observe once, judge per market. A Gemini multimodal Analyst watches each shot one time and emits neutral, timecoded observations under a fixed 18-dimension taxonomy, with no verdicts allowed. Fifteen per-market Adjudicator agents (Google ADK, one per market, run in parallel) join that single fact set to their YAML market pack and ground every citation with Google Search. A finding is always a join: observation x market rule x citation. The Publisher pushes metrics to Grafana Cloud Mimir over OTLP, finding detail to Loki, and creates dashboards, annotations and alert rules through a self-hosted Grafana MCP server. Alerts webhook back into the Cloud Run service and wake the Remediator, which edits with Gemini image editing and TTS, then ffmpeg recomposites the master. The console is FastAPI with server-sent events and no build step. The test commercial itself was generated with Veo, deliberately loaded with eight documented landmines, so Google's own tools made the ad and then failed it.

### Challenges we ran into

Prometheus rejects backdated samples, so we probed the ingest window and mapped each run's timecode onto the wall clock at run start: video second n lands at t0 plus n, and the panel range is pinned to the run, so the x-axis reads as the timecode because it is the timecode. Grafana Cloud iframes cannot carry a bearer token, so embeds run on public dashboards with a server-side image-render fallback behind one interface. Veo model listings lie: models that appear in models.list() can still 404 on generation, so we probe with a real generate call and pin what actually works. And one Cloud Run mystery ate a night: two services went Ready with healthy containers while Google's edge returned 404s with zero request logs, and a rename plus fresh service finally shipped it. The most important design fight was keeping the Guard un-promptable: it reads only rule metadata from the market packs, never model output, so a crafted finding cannot argue its way past it.

### Accomplishments that we're proud of

The loop actually closes: a Grafana alert wakes the Remediator, the edit lands, the Verifier re-observes the changed shot with the same instrument that found the problem, the metric drops, and Grafana resolves the alert on its own. At the milestone gate, the crew caught five of six planted landmines with zero empty rationales, and the French alcohol finding carried a live Legifrance citation. The two control shots produced zero false positives. We shipped 15 market packs with 114 rules, every rule naming a real statute or code, and 339 offline tests keep the whole thing honest.

### What we learned

Facts and judgments want different agents: separating the Analyst (what is in the film) from the Adjudicators (what each market says about it) made findings cheap, parallel and defensible, because "is this fact wrong" and "is this rule wrong" became separable questions. Grounded citations need a failure mode: findings whose citation cannot be resolved to a live source get capped severity and never trigger remediation, which turned hallucination from a risk into a rendered, visible quality signal. And guardrails belong in rule layers, not prompts: anything a prompt grants, a prompt can take away.

### What's next for Customs

More markets: a market is a YAML file, not code, and the pack format is the extension point. Veo shot regeneration as the heaviest remediation tier, on top of re-lettering, prop substitution and re-voicing. Campaign memory across assets, so the crew can answer "has this brand tripped this rule before" from its own Grafana history. And a legal-review workflow for the Guard's human decisions, with the statute attached.

## Built with

python, google-gemini, google-adk, grafana, grafana-cloud, mcp, imagen, veo, google-cloud-run, ffmpeg, fastapi, sqlite

## Links

- GitHub repo: https://github.com/tdries/td-devpost-agentic-cinema
- Live instance: https://customs-app-akap4ao72a-ew.a.run.app
- Demo video: https://youtu.be/YOUTUBE_VIDEO_ID (record and replace)

## Track

Grafana Labs

## Team

Tim Dries (solo)
