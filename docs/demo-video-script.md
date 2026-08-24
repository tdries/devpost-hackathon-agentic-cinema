# Demo video script (3 minutes)

The rules reward the working product, not a trailer. Everything below is the real app on the deployed instance, screen-recorded in one take if possible. Practice the run once first so the timings hold; the clearance itself takes about a minute for three markets, which is why the script starts the run early and narrates over it.

Recording setup: 1920x1080, browser at 100 percent zoom, dark room tab only, close the bookmarks bar. Live URL: https://customs-app-akap4ao72a-ew.a.run.app

| # | Time | Screen | Action | Voiceover |
|---|---|---|---|---|
| 1 | 0:00-0:15 | Home | Cold open on the upload form. Pick test_ad.mp4, tick FR, SA, US, hit Launch. | "This is a commercial our own tools generated with Veo. Wine on a terrace, English text, a beach shot, a bold claim. We are about to find out where on earth it can legally air. This is Customs." |
| 2 | 0:15-0:40 | Mission Feed | Switch to the feed while the run starts. Point at analyst lines, then an adjudicator citation check. | "A crew of agents takes over. The Analyst watches the film once and records neutral, timecoded facts. Fifteen adjudicators then judge those facts against each market's actual law, and every citation is checked against a live source with Google Search grounding." |
| 3 | 0:40-1:05 | Launch Board | Tiles flip as adjudicators return: US cleared, FR blocked, SA blocked. Hover the headline. | "France comes back blocked: Loi Evin bans alcohol advertising on television, and the statute is right there. Saudi Arabia, blocked. The US, cleared. The verdict is go or no-go, not a report." |
| 4 | 1:05-1:25 | Launch Board, scroll down | Scroll to the embedded Grafana panels: status bar gauge, risk heatmap. | "And this instrument panel? The crew built it. The Publisher agent provisions these dashboards through the Grafana MCP server during the run: metrics to Mimir, findings to Loki, an annotation for every finding." |
| 5 | 1:25-1:45 | Market Room SA | Open SA. Scroll to the Guard panel on SA-LGBT-01. | "One finding gets special treatment. The ad shows a same-sex couple. Saudi regulation prohibits that, and Customs refuses to edit people out of an ad. The Guard cites the rule and hands the decision to a human. That refusal is a feature, and no prompt can talk it out of it." |
| 6 | 1:45-2:05 | Market Room FR + Grafana alert | Open FR, click Remediate on FR-LANG-01. Cut briefly to the Grafana alert firing. | "In France, the handwritten English tagline violates the Toubon law. A Grafana alert fires on the finding, and the alert is what wakes the Remediator." |
| 7 | 2:05-2:35 | Cutting Room | Before and after players side by side, then the change record with before and after frames. Play both. | "The Remediator re-letters the note in French, in the same handwriting, on the same paper, with Gemini image editing. Then the Verifier re-runs the same instrument that failed the shot. It does not trust the edit; it re-observes it." |
| 8 | 2:35-2:50 | Grafana alert view | Show the alert transitioning to resolved, annotation on the timeline. | "The finding clears, the metric drops, and Grafana resolves its own alert. The loop is closed by evidence, not by hope." |
| 9 | 2:50-3:00 | Launch Board final | Final board state, then the repo README one beat. | "A decision for every market, a statute behind every finding, and a master that can actually ship. Customs. Gemini, ADK and Grafana, clearing ads for launch." |

Fallbacks while recording:

- If the live run is slow, record the run once, then record narration over a second pass through the finished run pages; every screen except Mission Feed renders identically from the store.
- If the Grafana alert view is slow to transition on camera, show the annotations list on the remediation dashboard instead; the resolving annotation carries the same story.
- Keep run_9daf97a80573 (or any finished full run) warm on the instance as a backup tab.
