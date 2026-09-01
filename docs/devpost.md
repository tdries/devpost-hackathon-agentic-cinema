# Devpost submission copy

Paste-ready. Track: Grafana Labs.

## Tagline (under 200 chars)

An AI crew that clears your commercial for 98 jurisdictions in parallel: the statute behind every finding, a Grafana instrument panel it builds itself, and failing shots re-rendered with Veo.

## Project story

### Inspiration

Global brands ship one commercial into dozens of markets and check maybe six of them. The regulated half already carries an invoice: France bans TV alcohol advertising under Loi Evin, Quebec bans ads aimed at children under 13, the UK pre-clears every TV ad through Clearcast, Nigeria requires ARCON vetting. The unregulated half costs more: Pepsi and Kendall Jenner, Dolce & Gabbana in China, H&M's hoodie. Every one of those was caught by the public rather than by a process. Pilots get a pre-flight checklist and an instrument panel. Ad launches get a consultant, a focus group and luck. We built the instrument.

### What it does

You upload a commercial (or paste a YouTube link) and a market list, and you watch it clear customs in real time. A crew of agents watches the film once, records what is actually in it, judges those facts against each market's law, broadcaster code and cultural norms, and builds its own Grafana instrument panel from the result. Market tiles — 21 packs resolving to 98 selectable jurisdictions: a global baseline, the EU, 16 countries and 80 broadcasters — flip from pending to cleared, at risk or blocked as adjudicators return, each finding carrying the statute and a live citation link. When a Grafana alert fires, a Remediator fixes the failing span through one of four priced methods — a single-frame patch, a relight-propagated edit, a full per-frame repaint, or **both ends of the span edited and Veo 3.1 generating the motion between them** — the frame edits land through the finding's own box so only the object changes, everything priced in euro before you press against a €45/day budget. A Verifier then re-runs the same instrument on the changed shots before anything is trusted, and Grafana resolves its own alert. The output is two things: a decision (can this air here, and on what evidence) and a localized master (the version that can). One more thing it deliberately does not do: when a rule targets a protected characteristic, like Saudi Arabia's prohibition on depicting same-sex couples, a rule-layer Guard refuses to auto-edit, names the problem, cites the regulation, and hands it to a human.

### How we built it

The core decision is observe once, judge per market. A Gemini multimodal Analyst watches each shot one time and emits neutral, timecoded observations under a fixed 18-dimension taxonomy, with no verdicts allowed. One Adjudicator agent per selected jurisdiction (Google ADK, run in parallel) joins that single fact set to its resolved YAML market pack — 128 rules across the ladder, every one naming a real statute or code — and grounds every citation with Google Search. A finding is always a join: observation x market rule x citation. The Publisher pushes metrics to Grafana Cloud Mimir over OTLP, finding detail to Loki, and creates dashboards, annotations and alert rules through the official Grafana MCP server. Alerts webhook back into the Cloud Run service and wake the Remediator, which edits with Gemini image editing, TTS and Veo 3.1, then ffmpeg recomposites the master and a craft gate refuses any deliverable that lost frames, resolution or soundtrack. The console is FastAPI with server-sent events and no build step. The test commercial itself was generated with Veo, deliberately loaded with seven documented landmines plus a clean control shot, so Google's own tools made the ad and then failed it.

### Challenges we ran into

Prometheus rejects backdated samples, so we probed the ingest window and mapped each run's timecode onto the wall clock at run start: video second n lands at t0 plus n, and the panel range is pinned to the run, so the x-axis reads as the timecode because it is the timecode. Grafana Cloud iframes cannot carry a bearer token, so embeds run on public dashboards with a server-side image-render fallback behind one interface. Veo model listings lie: models that appear in models.list() can still 404 on generation, so we probe with a real call and pin what actually works — that is also how we learned Veo 3.1 only generates 4, 6 or 8 seconds, and that its prompt enhancement cannot be turned off. And one Cloud Run mystery ate a night: two services went Ready with healthy containers while Google's edge returned 404s with zero request logs, and a rename plus fresh service finally shipped it. The most important design fight was keeping the Guard un-promptable: it reads only rule metadata from the market packs, never model output, so a crafted finding cannot argue its way past it.

### Accomplishments that we're proud of

The loop actually closes, on the live instance: a Grafana alert wakes the Remediator, both anchor frames are edited and checked, Veo generates the bridge, the craft gate accepts the recomposite, the Verifier re-observes the changed shot with the same instrument that found the problem, the metric drops, and Grafana resolves the alert on its own — there are runs in the public archive with a finding marked *verified fixed* and the raw generated clip beside the before/after. At the milestone gate, the crew caught five of the six gate-scored landmines with zero empty rationales, and the French alcohol finding carried a live Legifrance citation; the control shots produced zero false positives. We shipped 21 market packs resolving to 98 jurisdictions with 128 rules, every rule naming a real statute or code, and 501 offline tests keep the whole thing honest.

### What we learned

Facts and judgments want different agents: separating the Analyst (what is in the film) from the Adjudicators (what each market says about it) made findings cheap, parallel and defensible, because "is this fact wrong" and "is this rule wrong" became separable questions. Grounded citations need a failure mode: findings whose citation cannot be resolved to a live source get capped severity and never trigger remediation, which turned hallucination from a risk into a rendered, visible quality signal. And guardrails belong in rule layers, not prompts: anything a prompt grants, a prompt can take away.

### What is next for The Media Customs

More markets: a market is a YAML file, not code, and the pack format is the extension point. A downloadable per-market clearance certificate — the artifact a broadcaster or legal team actually accepts. Campaign memory across assets, so the crew can answer "has this brand tripped this rule before" from its own Grafana history. And a legal-review workflow for the Guard's human decisions, with the statute attached.

## Built with

python, google-gemini, google-adk, grafana, grafana-cloud, mcp, veo, google-cloud-run, ffmpeg, fastapi, sqlite

## Links

- GitHub repo: https://github.com/tdries/td-devpost-agentic-cinema
- Live instance: https://customs-app-akap4ao72a-ew.a.run.app
- Demo video: https://youtu.be/YOUTUBE_VIDEO_ID (record and replace)

## Track

Grafana Labs

## Team

Tim Dries (solo)
