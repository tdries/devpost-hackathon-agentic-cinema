# Customs: Design

**Date:** 2026-08-23
**Hackathon:** Agentic Cinema (Devpost), Grafana Labs track
**Deadline:** 2026-09-09, 14:00 PDT

## 1. What it is

Customs is a clearance room for advertising. You hand it a commercial and a list of
target markets. A crew of agents watches the film, records what is actually in it,
judges those facts against each market's law, broadcaster code and cultural norms,
publishes the result as a live Grafana workspace, and then, when an alert fires,
re-renders the parts of the film that fail so the spot can ship in that market.

The output is two things: a decision (can this air here, and on what evidence) and a
localized master (here is the version that can).

## 2. The problem

Global brands ship a single spot into dozens of markets and check maybe six of them.
The defence today is a cultural consultant, a focus group and luck.

The regulated half is concrete and already carries an invoice:

- The UK requires pre-clearance of every TV ad through Clearcast before it can air.
- France bans alcohol advertising under Loi Evin, and the Toubon law constrains
  on-screen language.
- Quebec bans advertising directed at children under 13 outright.
- Nigeria requires ARCON pre-vetting of advertisements.
- Direct-to-consumer pharmaceutical advertising is lawful in two countries on earth.
- The UK ASA banned harmful gender stereotypes in advertising in 2019.

The unregulated half costs more. Pepsi and Kendall Jenner. Dolce and Gabbana in China.
H&M's "coolest monkey" hoodie. Burger King Vietnam eating with chopsticks. Heineken's
"lighter is better". Every one of those was caught by the public rather than by a
process, at a cost measured in tens of millions.

Nobody has an instrument for this. Customs is the instrument.

## 3. The architectural decision everything hangs off

**Observation and judgement are separate passes.**

Analysts watch the film once and emit neutral, timecoded, market-agnostic facts.
Adjudicators, one per market, judge that same fact set against their own rulebook.

This matters for three reasons:

1. Cost and latency. The expensive multimodal pass happens once, not once per market.
2. Parallelism. Forty adjudicators are forty independent, cheap, text-only agents.
3. Defensibility. When a brand disputes a finding, you separate "is this fact wrong"
   from "is this rule wrong". A single fused pass cannot do that.

A finding is therefore always a join: `observation x market rule x citation`.

## 4. The crew

| Agent | Input | Output | Notes |
|---|---|---|---|
| **Ingest** | video file | shot table, keyframes, audio, transcript, OCR text | ffmpeg for cuts and extraction, Gemini for transcription and OCR |
| **Analyst** | shot + keyframes + audio | neutral observations across 18 dimensions | Gemini multimodal, one call per shot, no verdicts allowed |
| **Adjudicator** | observation set + one market pack | findings with class, severity, rationale, citation | one agent instance per market, run in parallel |
| **Guard** | findings | classified and possibly blocked findings | rule layer, not a model; see section 7 |
| **Publisher** | findings | Grafana metrics, logs, dashboards, alert rules | via Grafana MCP and the Cloud push endpoints |
| **Remediator** | one alerting finding | edited media segment plus a change record | woken by a Grafana alert webhook |
| **Verifier** | localized master | confirmation the finding cleared and nothing new broke | re-runs Analyst and Adjudicator on the changed shots only |

The Verifier closing back onto the Analyst is what makes this a crew rather than a
pipeline. Remediation is not trusted until the same instrument that found the problem
agrees it is gone.

## 5. Observation dimensions

Analysts emit observations under a fixed taxonomy so that market packs can reference
dimensions rather than free text.

```
alcohol_tobacco_drugs        religious_symbols_practices   modesty_dress_body
gesture_body_language        food_and_animals              gender_portrayal
sexual_orientation_gender_id children_and_minors           national_symbols_politics
health_claims_pharma         gambling_and_finance          violence_and_weapons
language_profanity_idiom     humour_irony_satire           superstition_number_colour
photosensitivity_sensory     text_legibility               comparative_claims
```

An observation is neutral and evidential:

```json
{
  "id": "obs_0041",
  "shot_id": "sh_007",
  "t_start": 12.40,
  "t_end": 14.10,
  "dimension": "alcohol_tobacco_drugs",
  "statement": "A glass of red wine sits on the table in front of the left-hand actor.",
  "evidence": {"frame": "kf_007_02.jpg", "bbox": [0.41, 0.55, 0.52, 0.78]},
  "confidence": 0.91
}
```

Note what is absent: no market, no severity, no opinion.

## 6. Market packs

One data file per market, versioned in the repo, extensible without touching code.

```yaml
market: FR
name: France
regulators: [ARPP, CSA/Arcom]
pre_clearance: advisory        # none | advisory | mandatory
rules:
  - id: FR-ALC-01
    dimension: alcohol_tobacco_drugs
    class: legal
    severity: 95
    trigger: "Depiction of alcoholic drink in an advertisement"
    basis: "Loi Evin, Code de la sante publique art. L3323-2"
    source_hint: "legifrance.gouv.fr L3323-2"
    remediable: true
  - id: FR-LANG-01
    dimension: text_legibility
    class: legal
    severity: 60
    trigger: "On-screen commercial text not in French and not accompanied by translation"
    basis: "Loi Toubon, loi 94-665"
    remediable: true
```

Launch pack, chosen to span the distinct regulatory and cultural regimes rather than
the largest economies:

US, UK, France, Germany, Canada (Quebec), Brazil, Saudi Arabia, UAE, Turkey, India,
China, Indonesia, Japan, Thailand, Nigeria.

Fifteen markets with citations is a stronger claim than a hundred and ninety-five
without. The pack format is the extension point and the README says so.

## 7. The guard

Every finding carries a `class`:

- `legal`: a statute or binding regulation in that jurisdiction
- `policy`: a broadcaster, platform or self-regulatory code
- `offence`: no rule, but documented likelihood of causing offence

Auto-remediation is offered only for `legal` and `policy`.

Additionally, if a rule's basis targets a protected characteristic (sexual orientation,
gender identity, religion, race, ethnicity, disability, caste), the finding is marked
`remediation_blocked` with a stated reason, no edit is proposed, and the dashboard
renders it as a human decision rather than a task.

The product line is explicit: Customs will tell you that you cannot ship into a market
without censoring who appears in your ad, and it will not do that for you.

This is a rule layer over the market pack metadata, not a model judgement, so it cannot
be argued out of by a prompt.

## 8. Findings and severity

```json
{
  "asset_id": "ast_01",
  "observation_id": "obs_0041",
  "market": "FR",
  "rule_id": "FR-ALC-01",
  "class": "legal",
  "severity": 95,
  "t_start": 12.40,
  "t_end": 14.10,
  "rationale": "Loi Evin prohibits advertising for alcoholic beverages on television...",
  "citation": {"ref": "Code de la sante publique art. L3323-2", "url": "https://..."},
  "sourced": true,
  "confidence": 0.88,
  "remediable": true,
  "remediation_blocked": false
}
```

Citations come from Gemini with Google Search grounding. A finding whose citation cannot
be resolved to a live source is set `sourced: false`, is capped at severity 40, and is
rendered on the dashboard under an explicit "unsourced" panel. Unsourced findings never
trigger remediation.

Market clearance status is derived, not modelled:

- any unresolved `legal` finding at severity >= 70 → **blocked**
- any unresolved `policy` finding at severity >= 70 → **at risk**
- otherwise → **cleared**

## 9. The Grafana surface

Metrics go to Grafana Cloud Prometheus/Mimir. Finding detail goes to Loki with labels
`{asset, market, class, dimension, rule_id}`. Dashboards and alert rules are created and
updated by the Publisher agent through the Grafana MCP server, not hand-built.

Core series: `customs_risk{asset,market,dimension}` sampled once per video second.
A commercial is a time series and the timecode is the x-axis.

Timecode mapping, pinned so it cannot be argued about later: Prometheus rejects samples
backdated beyond its out-of-order window, so we do not attempt to write real timecodes.
Each run picks `t0 = run start time` and writes video second `n` at wall-clock `t0 + n`.
Samples are therefore current and monotonic, the panel range is `t0` to `t0 + duration`,
and the axis reads as the timecode because it is the timecode, offset by a constant. The
run's `t0` is stored on the run record and on every Loki line so panels and drill-downs
agree. Loki receives finding detail on the same mapped clock.

Six pages:

1. **Clearance Overview**: per-market status tiles, blocked count, "cleared to air in
   9 of 15 markets", worst offending dimension.
2. **Timeline**: market by timecode heatmap. The centrepiece panel. Click a cell,
   drill to the finding and the frame.
3. **Findings**: table with rule, class, severity, citation link, sourced flag.
4. **Market Detail**: one market: regulator, pre-clearance regime, every applicable
   rule, every finding with its statute.
5. **Remediation**: what was changed, before and after frames, what is blocked and why.
6. **Campaign History**: across assets and time. Has this brand tripped this rule before.

Alert rules, also created via MCP:

- `customs_blocking_finding`: any unresolved legal finding at severity >= 70
- `customs_market_at_risk`: market aggregate risk over threshold
- `customs_unsourced_spike`: evidence quality degradation

Alerts route to a webhook contact point on Cloud Run, which wakes the Remediator. The
alert payload carries the series labels, so `{asset, market, rule_id}` plus the sample
timestamp resolves back to exactly one finding through the run's `t0`. The webhook looks
that finding up in the run store rather than trusting anything in the payload body. When
the Verifier confirms a fix, the metric drops and Grafana resolves the alert on its own.
That resolution is the demo's closing beat.

Auth: the hosted Grafana MCP endpoint is interactive OAuth only with no service account
path, so we run `grafana/mcp-grafana` ourselves as a sidecar with a Grafana Cloud service
account token. The agents talk to it over stdio or HTTP inside the Cloud Run service.

The agents read as well as write through MCP. Before remediating, the Remediator queries
campaign history for prior findings on the same rule and what was done about them.

## 10. Remediation and hyper-localization

Ordered by cost and by risk of looking fake:

| Class | Method | In spine |
|---|---|---|
| On-screen text | OCR box, translate in context, re-letter in the show's face, Imagen inpaint, track across the shot | yes |
| Prop substitution | mask the object, Imagen inpaint a market-appropriate replacement, propagate across frames | yes, one prop |
| Audio line | re-voice with Gemini TTS matched to the speaker, or mute | yes, one line |
| Reframe | crop or pan to exclude the element without regenerating pixels | yes |
| Shot regeneration | Veo regenerates the shot for the market | stretch, one shot |

Every edit produces a change record with the source finding, the method, the frames
touched, and before and after stills. Nothing is edited silently.

Hyper-localization is the same machinery pointed further: the wine becomes tea, the
signage becomes local script, the gesture is replaced, the street becomes a local street.
The spine proves the mechanism on text, prop and audio. Veo shot regeneration is the
stretch and ships only once the spine is green.

## 11. Test asset

We generate our own commercial with Veo, deliberately loaded with landmines across
several dimensions. No rights problem, complete control over what trips, and it lets the
demo say that Google's own tools made the ad and then failed it.

Target: 45 to 60 seconds, six to eight shots, at least ten distinct triggers spread
across legal, policy and offence classes, and at least one deliberately blocked-for-
censorship case so the guard is visible in the demo.

## 12. Stack

- **Agents:** Google Agent Development Kit (ADK), Python 3.12
- **Reasoning and vision:** Gemini 3.x multimodal, Google Search grounding for citations
- **Image edit:** Imagen inpainting
- **Video generation:** Veo, for the test asset and the stretch shot regeneration
- **Speech:** Gemini TTS
- **Media mechanics:** ffmpeg
- **Observability platform:** Grafana Cloud free tier (Mimir, Loki, Alerting, Dashboards)
- **MCP:** self-hosted `grafana/mcp-grafana` with a service account token
- **App and webhook:** FastAPI on Cloud Run
- **Run store:** SQLite, one file per run, no server to operate

No Anthropic, OpenAI, AWS or Microsoft model, framework or AI API appears at runtime or
in the dependency tree. This is a hard rule of the contest and it is enforced by a check
in CI.

## 13. Repository layout

```
customs/
  app/            FastAPI front door, upload, progress stream, before/after player
  agents/         ingest, analyst, adjudicator, guard, publisher, remediator, verifier
  markets/        one YAML per market, plus the dimension taxonomy
  media/          ffmpeg helpers, frame and audio extraction, reassembly
  grafana/        dashboard and alert definitions, MCP client
  tests/          pytest, one check per piece of non-trivial logic
  docs/           spec, plan, screenshots, manual
```

## 14. Error handling

Each stage is idempotent per `(asset_id, stage)` and writes its output before the next
stage reads it, so a failed run resumes rather than restarts.

- Model call failure: retry with backoff, three attempts, then record a stage error.
- A stage error does not fail the run. It is published to Grafana as a
  `customs_stage_error` and rendered on the Overview page, because a clearance tool that
  silently skips a shot is worse than one that admits it.
- Citation resolution failure downgrades a finding rather than dropping it.
- Remediation failure leaves the original media untouched and the alert unresolved.
- Input is capped at 120 seconds and 200 MB. Longer inputs are rejected at upload with a
  clear message rather than dying halfway.

## 15. Testing

One runnable check per piece of logic that can be wrong in a way a person would not
notice:

- shot boundary parsing from ffmpeg output
- observation to rule matching by dimension
- severity aggregation and the derived clearance status bands
- the guard's protected-characteristic blocking, including that it cannot be overridden
  by finding content
- timecode to metric sample bucketing
- the no-forbidden-vendor dependency check

Model output quality is checked by a small golden set: a fixed observation set with an
expected finding count per market, tolerant to wording but not to a market's rules being
missed entirely.

## 16. Scope

**Spine, must ship:**

Veo test asset. Fifteen market packs. Ingest, Analyst, Adjudicator, Guard, Publisher.
Six Grafana pages built through MCP. Alerting into remediation. Text, one prop and one
audio line remediated. Verifier closing the loop. Web front door with before and after.
Deployed on Cloud Run. Public repo, Apache-2.0.

**Stretch, only when the spine is green:**

Veo shot regeneration. Campaign History across multiple assets. More markets. Grafana
Incident integration.

**Explicitly out:**

Live or streaming input. Full-length content. Any market pack claim we cannot cite.
Automatic remediation of anything the guard blocks.

## 17. Risks

| Risk | Mitigation |
|---|---|
| Findings are plausible-sounding nonsense | Citations required, unsourced findings capped and visibly labelled, golden-set test |
| Grafana MCP cannot create dashboards unattended | Verified in the first build milestone, before the surface is designed around it; fallback is provisioned dashboards with MCP still used for query, alerting and history |
| Inpainting looks fake and undermines the demo | Prop chosen for a static, well-lit, unoccluded shot; reframe is the fallback edit |
| Scope creep into hyper-localization before the spine works | Stretch items are gated on a green spine, stated in the plan |
| The tool reads as a censorship aid | The guard is a headline feature, not a footnote, and appears in the demo video |
