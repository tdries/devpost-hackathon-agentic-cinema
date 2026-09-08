"""The operator manual: white A4, written once, rendered by headless Chrome."""
import datetime, re, subprocess

IMG = "img/"
pages = []

def page(body, folio_left="The Media Customs", cls="page"):
    pages.append((cls, body, folio_left))

def head(eyebrow, title, lede=""):
    l = f'<p class="lede">{lede}</p>' if lede else ""
    return f'<p class="eyebrow">{eyebrow}</p><h2>{title}</h2>{l}'

def ic(name):
    """One taxonomy, crew or method mark, inline and on the baseline."""
    return f'<img class="ico" src="{IMG}ico/{name}.png">'


DIM = {
    "alcohol": "d-alcohol_tobacco_drugs", "tobacco": "d-alcohol_tobacco_drugs",
    "gesture": "d-gesture_body_language", "modesty": "d-modesty_dress_body",
    "text": "d-text_legibility", "claims": "d-comparative_claims",
}


def shot(src, cap):
    return f'<img class="shot" src="{IMG}{src}"><p class="cap">{cap}</p>'

TODAY = datetime.date.today().strftime("%d %B %Y")

# ------------------------------------------------------------------ cover
page(f'''
<img class="mark" src="{IMG}logo.png">
<h1>THE MEDIA CUSTOMS</h1>
<p class="sub">Operator manual</p>
<div class="brandbar" style="justify-content:center"><i></i><i></i><i></i><i></i></div>
<div class="marks">
  <img src="{IMG}google.png"><img src="{IMG}adk.png"><img src="{IMG}omni.png">
  <img src="{IMG}veo.png"><img src="{IMG}grafana.png">
</div>
<div class="meta">Version 1.0 &nbsp;&middot;&nbsp; {TODAY} &nbsp;&middot;&nbsp; Tim Dries<br>
One asset, every market, before it ships.</div>
''', cls="page cover")

# -------------------------------------------------------------------- toc
# Filled in after every page exists, so a chapter that moves cannot leave a
# wrong number behind. A "continued" page is part of its chapter, not an entry.
TOC_AT = len(pages)
page("", cls="page")

# -------------------------------------------------------------- 01 what it is
page(head("chapter 01", "What this is",
          "An agentic ad-clearance crew. It watches a commercial once, judges it against "
          "every market you ship to in parallel, and re-renders the shots that fail.") + f'''
<p>A commercial is cut once and shipped everywhere. Somebody then has to answer, per
market, whether it can legally air. Today that answer comes from a lawyer for the two or
three biggest markets and from hope for the rest, which is how a gesture that is friendly
here becomes an insult there, and how an ad gets pulled after it airs rather than before.</p>

<p>The Media Customs answers it for all of them at once, and shows its working. Every
finding names the shot, the seconds, the rule, and the statute behind the rule, with a
citation resolved live at the moment of judging. Nothing in the console is a summary of
something you cannot open.</p>

<h3>Who operates it</h3>
<table>
<tr><th>Role</th><th>What they do here</th></tr>
<tr><td><b>Producer or traffic</b></td><td>Starts the clearance, reads the board, exports the certificate and the markers.</td></tr>
<tr><td><b>Legal or compliance</b></td><td>Reads the findings and their statutes, decides the ones the Guard hands over.</td></tr>
<tr><td><b>Editor</b></td><td>Takes the markers into Resolve or Premiere, or reviews what the remediator rendered.</td></tr>
</table>

<h3>What it produces</h3>
<ul>
<li>[[ic:i-cleared]]A decision per market, cleared, at risk or blocked, with the evidence behind each.</li>
<li>[[ic:n-cut]]A localized master per market, with only the failing seconds touched.</li>
<li>[[ic:i-legal]]A clearance certificate as a PDF, every finding and statute on it.</li>
<li>[[ic:n-timeline]]Marker files for an editing suite, colour-coded by what the finding does.</li>
</ul>

<div class="panel note"><h4>One sentence to remember</h4>
A finding is always a join: <b>this observation, times that market's rule, times a citation
that resolves.</b> Break any leg of it and the finding is not made. That is why you can
argue with a finding usefully, and why the system can tell you which leg is wrong.</div>
''')

# ------------------------------------------------------------ 02 how it thinks
page(head("chapter 02", "How it thinks",
          "Looking at the film is the expensive part. Opinions about what was seen are cheap. "
          "So the two never mix.") + f'''
<p>One multimodal analyst watches each shot a single time and writes down what it sees, in
neutral language, with a timecode and a box. It is forbidden an opinion. It writes
<i>a woman raises a glass of red wine</i>, never <i>this violates French law</i>. That sentence is filed under [[ic:d-alcohol_tobacco_drugs]]alcohol, tobacco and drugs, and every rule about drink in every pack is written against that same dimension.</p>

<p>That single fact sheet then goes to every selected market at once. One adjudicator per
jurisdiction reads it against its own rulebook and grounds each citation against a live
source. Ninety-eight adjudicators argue about the same sentence, simultaneously, each
holding a different rulebook.</p>

<h3>Why it is built this way</h3>
<ul>
<li><b>It is cheap.</b> Adding a market costs one more text call, not one more viewing of the film.</li>
<li><b>It is parallel.</b> Markets do not queue behind each other.</li>
<li><b>It is arguable.</b> "Is this fact wrong?" and "is this rule wrong?" become separate
questions with separate answers, which is what makes a disagreement resolvable.</li>
</ul>

<h3>The vocabulary is fixed</h3>
<p>The analyst may only emit observations under eighteen dimensions, and every one of the
128 rules is written against one of them. That is what makes the join between a fact and a
market's opinion a lookup rather than an argument.</p>
<div class="dimgrid"><span>[[ic:d-alcohol_tobacco_drugs]]alcohol, tobacco, drugs</span><span>[[ic:d-religious_symbols_practices]]religious symbols</span><span>[[ic:d-modesty_dress_body]]modesty and dress</span><span>[[ic:d-gesture_body_language]]gesture and body language</span><span>[[ic:d-food_and_animals]]food and animals</span><span>[[ic:d-gender_portrayal]]gender portrayal</span><span>[[ic:d-sexual_orientation_gender_id]]orientation, gender id</span><span>[[ic:d-children_and_minors]]children and minors</span><span>[[ic:d-national_symbols_politics]]national symbols</span><span>[[ic:d-health_claims_pharma]]health claims, pharma</span><span>[[ic:d-gambling_and_finance]]gambling and finance</span><span>[[ic:d-violence_and_weapons]]violence and weapons</span><span>[[ic:d-language_profanity_idiom]]language and idiom</span><span>[[ic:d-humour_irony_satire]]humour and satire</span><span>[[ic:d-superstition_number_colour]]superstition, colour</span><span>[[ic:d-photosensitivity_sensory]]photosensitivity</span><span>[[ic:d-text_legibility]]on-screen text</span><span>[[ic:d-comparative_claims]]comparative claims</span></div>
</table>
<div class="panel"><h4>The ladder</h4>
Markets inherit. A run against a Belgian broadcaster is judged against that channel's own
acceptance rules, Belgian law, the EU directive and the global baseline, in that order.
Twenty-one packs on disk resolve into 98 selectable jurisdictions.</div>
''')

# ----------------------------------------------------------- 03 a run end to end
page(head("chapter 03", "A run, end to end",
          "Eight stages. Each one writes something you can open afterwards.") + f'''
{shot("flow.png", "The same diagram is on the console's front page, and it plays itself. Every stage names the model doing its job underneath it.")}
<div class="panel note"><h4>How long it takes</h4>
About four minutes for a thirty second spot, and proportionally longer for longer films,
because the analyst reads every shot: shot detection, a transcription, an observation pass
per shot, then every market judged in parallel with a grounded citation per finding.</div>
<div class="panel warn"><h4>The arrow that matters</h4>
Stage 6 is the one that makes this different from a report. Nothing polls, and no cron runs.
A threshold crosses on a dashboard the crew built itself, Grafana calls the service, and
generation begins. The fix starts because a chart said so.</div>
''')

page(head("chapter 03, continued", "The eight stages",
          "What each one does, and what it leaves behind for you to open.") + f'''
<table class="stages">
<tr><th>#</th><th>Stage</th><th>What happens</th><th>What it leaves behind</th></tr>
<tr><td class="mono">1</td><td>[[ic:i-human]]<b>You</b></td><td>Hand it a master and tick the markets.</td><td>The run, in the archive.</td></tr>
<tr><td class="mono">2</td><td>[[ic:i-ingest]]<b>Ingest</b></td><td>Cuts the film into shots, pulls the audio and a transcript.</td><td>Shots and keyframes.</td></tr>
<tr><td class="mono">3</td><td>[[ic:i-analyst]]<b>Analyst</b></td><td>Watches each shot once. Neutral, timecoded, boxed observations.</td><td>Loki lines, <code>kind=observation</code>.</td></tr>
<tr><td class="mono">4</td><td>[[ic:i-adjudicator]]<b>Adjudicators</b></td><td>One per market, in parallel, each against its own rulebook, each citation grounded.</td><td>Findings, with statutes.</td></tr>
<tr><td class="mono">5</td><td>[[ic:i-publisher]]<b>Publisher</b></td><td>Pushes metrics and lines, and builds the run's dashboards and alert rules.</td><td>Mimir series, annotations, 9 boards.</td></tr>
<tr><td class="mono">6</td><td>[[ic:i-alert]]<b>Alert</b></td><td>A blocking finding trips a rule Grafana is already evaluating.</td><td>A webhook call into the service.</td></tr>
<tr><td class="mono">7</td><td>[[ic:i-remediator]]<b>Remediator</b></td><td>Plans, prices and edits the failing span. One edit per shot.</td><td>A change record and a staged file.</td></tr>
<tr><td class="mono">8</td><td>[[ic:i-verifier]]<b>Verifier</b></td><td>Re-runs the real analyst on the changed shots.</td><td>Fixed, or the finding back to open.</td></tr>
</table>
<div class="panel"><h4>Where each stage shows up in the console</h4>
Stages 2 to 4 are the mission feed and the frame board. Stage 5 is the launch board and
everything on the Grafana page. Stages 6 to 8 are the market room, the fix panel and the
cutting room. Nothing happens that you cannot afterwards open and read.</div>
''')

# -------------------------------------------------------- 04 before you start
page(head("chapter 04", "Before you start",
          "What you need, what it costs, and the things it will refuse to do.") + f'''
<h3>Access</h3>
<p>Reading is never gated. The archive, every run, every finding with its statute and
citation, the rule library, frame search, the intelligence board and the Grafana inventory
are open to anyone with the link. What is gated is everything that <b>spends or destroys</b>:
starting a clearance, judging more markets, running a fix, asking the agent, and deleting a
change record. Those ask for a word once and remember it in a cookie.</p>

<div class="panel"><h4>Two doors</h4>
The visitor door lifts a daily spending cap of one euro. The judge door lifts it entirely.
Having the word is not being someone: it is a speed bump, and the ceiling behind it is what
actually bounds a stranger's spend.</div>

<h3>What a run needs</h3>
<table>
<tr><th>Input</th><th>Accepted</th><th>Limit</th></tr>
<tr><td>A master</td><td>MP4 or MOV</td><td>Up to 128 seconds, up to 30 MB by upload</td></tr>
<tr><td>A link</td><td>Public video URL</td><td>Same duration cap, fetched at up to 720p</td></tr>
<tr><td>Markets</td><td>Any of 98</td><td>More markets is more cost and more wall clock</td></tr>
</table>

<h3>What it will not do</h3>
<ul>
<li>[[ic:i-guard]]<b>It will not auto-edit a protected characteristic.</b> Two rules in the corpus are
written on one, and a finding matching either is handed to a person instead. Chapter 14.</li>
<li>[[ic:i-alert]]<b>It will not spend past the day.</b> A budget alert pauses automatic remediation when
what is left drops below the price of the most expensive single fix. Chapter 22.</li>
<li>[[ic:i-error]]<b>It will not keep an edit that damaged the film.</b> The craft gate discards it and
the finding goes back to open. Chapter 12.</li>
<li>[[ic:i-verifier]]<b>It will not trust a model's word that a fix worked.</b> The verifier re-observes.</li>
</ul>

<div class="panel stop"><h4>Read this before you upload borrowed footage</h4>
Gemini Omni refuses to edit footage containing recognisable third-party content, and Veo
refuses a shot it reads as depicting a public figure. Neither is charged, and a retry cannot
help: the footage is the refusal. Patch methods still work on that material, but the archive
is public to anyone with the link, so only upload what you have the rights to show.</div>
''')

# ---------------------------------------------------------- 05 the launcher
page(head("chapter 05", "Starting a clearance",
          "The launcher asks two things: the film, and who has to accept it.") + f'''
{shot("launcher.png", "The master on the left, the market ladder on the right.")}
<h3>Step by step</h3>
<ol>
<li><b>Choose the master.</b> Drop a file or paste a link. The duration is checked before
anything is downloaded, so an over-long film is refused without spending.</li>
<li><b>Open the ladder and tick markets.</b> Global and the EU are ticked by default. Countries
sit under the continental rung, broadcasters under their country.</li>
<li><b>Read the price</b>, shown against the day's remaining budget.</li>
<li><b>Begin clearance.</b> The console moves to the mission feed and the crew takes over.</li>
</ol>

<h3>Choosing markets well</h3>
<table>
<tr><th>If you want</th><th>Tick</th></tr>
<tr><td>A fast sanity check</td><td>Global and the EU: two adjudicators, the broadest rules, about a minute.</td></tr>
<tr><td>A real clearance</td><td>Every country you will air in. National law is where the blocking findings live.</td></tr>
<tr><td>A channel delivery</td><td>The broadcaster itself. It inherits its country and adds its own rules.</td></tr>
</table>

<p><b>A run is cheap; a fix is not.</b> Judging costs cents and generation costs euro, so tick
generously here and be deliberate later.</p>
''')

# -------------------------------------------------------- 06 the mission feed
page(head("chapter 06", "Watching it work",
          "The mission feed is the crew's own event log, streamed as it happens.") + f'''
{shot("05-mission-feed.png", "Every move, in the order it was made. Not a spinner.")}
<p>Each stage says what it is doing in plain words, with its own shorthand underneath. A stage
that fails says so here rather than skipping quietly, and counts itself as a metric.</p>

<h3>What to look for</h3>
<table>
<tr><th>Line</th><th>What it tells you</th></tr>
<tr><td class="mono">judge &rarr; FR (6 candidates)</td><td>How many observations that market had to rule on.</td></tr>
<tr><td class="mono">citation check &rarr; FR FR-ALC-01</td><td>A citation being resolved against a live source.</td></tr>
<tr><td class="mono">concept scope, running anyway</td><td>The planner was warned a patch cannot reach this one. Chapter 11.</td></tr>
<tr><td class="mono">craft gate passed: 49.7 dB outside the edit</td><td>How little of the frame outside the edit box moved.</td></tr>
<tr><td class="mono">verify &rarr; re-observing 1 touched shot</td><td>The verifier looking at the new footage.</td></tr>
</table>

<p>The feed is written to the run and survives it, so a finished run shows the same lines in
the same order. That is what makes it evidence rather than reassurance while you wait.</p>
''')

# --------------------------------------------------------- 07 the launch board
page(head("chapter 07", "The verdict",
          "One tile per market, and the instrument panel the crew built underneath it.") + f'''
{shot("03-launch-board.png", "Cleared, at risk or blocked, with the worst finding on the tile.")}
<h3>Reading a tile</h3>
<table>
<tr><th>State</th><th>Means</th><th>Do</th></tr>
<tr><td>[[ic:i-cleared]]<b>Cleared</b></td><td>No finding in this market blocks.</td><td>Take the master and the certificate.</td></tr>
<tr><td>[[ic:i-at-risk]]<b>At risk</b></td><td>Findings exist but none is at the blocking line, or a citation could not be resolved so its severity is capped.</td><td>Read them. This is a judgement call, not a machine one.</td></tr>
<tr><td>[[ic:i-blocked]]<b>Blocked</b></td><td>At least one open, sourced finding is at or over the line.</td><td>Open the market room and fix or accept.</td></tr>
</table>

<p>Under the tiles is the crew's own lanes dashboard, live from Grafana and clickable. It
is not a picture of the run: it is the run's data, drawn by Grafana out of the stores the
crew wrote during the run.</p>

<div class="panel note"><h4>An unresolved citation is not a small thing</h4>
A finding whose citation will not resolve gets capped severity and can never trigger a fix.
That turns a hallucinated rule into a visibly weaker finding rather than a wrong edit.</div>
''')

# ---------------------------------------------------------- 08 the frame board
page(head("chapter 08", "What it saw",
          "Every scene the crew looked at, and the sentences it wrote before any market saw them.") + f'''
{shot("06-frame-board.png", "How each scene opens and closes, with its neutral observations.")}
<p>This is the fact sheet all the adjudicators argued about. If a finding looks wrong, come
here first and decide which leg of the join failed:</p>
<ul>
<li><b>The observation is wrong.</b> The analyst saw something that is not there, or missed
something that is. That is a vision problem, and re-running the run is the answer.</li>
<li><b>The observation is right and the rule is wrong.</b> The market pack needs editing.
That is a YAML file, not code. Chapter 23.</li>
</ul>
<p>Separating those two questions is the whole reason the analyst is forbidden an opinion.</p>

<div class="panel"><h4>Every caption here is also a log line</h4>
Which is why frame search can answer "which frames show wine" across every run this instance
has ever done, without anyone having anticipated the question. Chapter 18.</div>
''')

# ------------------------------------------------------------- 09 the timeline
page(head("chapter 09", "Where the trouble is",
          "One grid, drawn by two systems, that turns a list of findings into a shape.") + f'''
{shot("timeline.png", "Categories down the side, scenes across the top, Grafana in between.")}
<p>The taxonomy icons and the scene thumbnails on the axes are the console's, because no
Grafana panel can put an image on an axis. The squares between them are Grafana's own status
history panel, live. A column is as wide as its scene is long, so a column's share of the
width is its share of the film.</p>

<h3>What you learn here that a list will not tell you</h3>
<ul>
<li>Whether the trouble is one bad scene or spread through the film.</li>
<li>Whether one category is doing all the damage.</li>
<li>Which scene to fix first for the most markets at once.</li>
</ul>

<div class="panel warn"><h4>Clicking a square spends money</h4>
A square is a data link. Clicking it resolves the open finding under it and starts a priced
fix on that scene. It is the fastest route from "there is the problem" to "fix it", which
also means it is the fastest route to a charge. The price is shown before it runs.</div>
''')

# ---------------------------------------------------------- 10 the market room
page(head("chapter 10", "One market in full",
          "The market room is where a finding stops being a colour and becomes an argument.") + f'''
{shot("07-market-room.png", "One panel per scene, every finding written against it.")}
<h3>What a finding carries</h3>
<table>
<tr><th>Field</th><th>Means</th></tr>
<tr><td><b>Rule id</b></td><td>The rule in the market pack, for example <code>FR-ALC-01</code>.</td></tr>
<tr><td><b>Class</b></td><td>legal, policy or offence. Legal blocks; offence is never auto-edited.</td></tr>
<tr><td><b>Severity</b></td><td>0 to 100. Seventy is where a finding starts to block a market.</td></tr>
<tr><td><b>Window</b></td><td>The seconds it covers, which is what a fix will touch.</td></tr>
<tr><td><b>Evidence</b></td><td>The triggering frame, with the analyst's own sentence.</td></tr>
<tr><td><b>Statute</b></td><td>The regulator's own words, and the citation resolved at judging time.</td></tr>
<tr><td><b>Sourced</b></td><td>Whether that citation resolved. Unsourced findings cannot trigger a fix.</td></tr>
</table>
<p>Two takeaways sit at the top of the room: the certificate as a PDF and the marker files,
both per market and both described in chapter 21. When three jurisdictions object to the
same two seconds, that is one shot with three objections rather than three problems, and one
edit answers all of them.</p>
''')

# ------------------------------------------------------------ 11 the fix panel
page(head("chapter 11", "Fixing a shot",
          "The fix panel asks two questions: what should change, and how it should be done.") + f'''
{shot("fixpanel.png", "What should change on the left, how to do it on the right, priced.")}
<p>The left column offers replacements the market allows. The right column offers the five
methods, each with its price in euro and a note where it is a poor fit for this particular
shot. Nothing is spent until you press.</p>
<div class="panel note"><h4>Read the warning above the methods</h4>
When the panel says the element runs through the whole scene, a patch cannot reach it. That
sentence is not decoration: it is the scope classifier, and ignoring it is how you pay for
an edit that the verifier will reject. Chapter 12 has a worked example.</div>
''')

page(head("chapter 11, continued", "The five methods",
          "Each one disturbs a different amount of footage, and costs accordingly.") + f'''
<table>
<tr><th>Method</th><th>What it does</th><th>Right when</th><th>Price</th></tr>
<tr><td>[[ic:m-overlay]]<b>Patch one frame</b><br><span class="mono">overlay</span></td>
    <td>One image edit of one keyframe, held over the span.</td>
    <td>A locked-off shot where nothing moves.</td><td class="mono">0.04 EUR</td></tr>
<tr><td>[[ic:m-track]]<b>Propagate the change</b><br><span class="mono">track</span></td>
    <td>The same edit, its lighting divided out and multiplied into every live frame.</td>
    <td>The thing to change holds still in frame while the shot moves.</td><td class="mono">0.04 EUR</td></tr>
<tr><td>[[ic:m-per_frame]]<b>Repaint every frame</b><br><span class="mono">per_frame</span></td>
    <td>A repaint of each frame in the span.</td>
    <td>A target that deforms or is occluded, where one edit cannot be propagated.</td><td class="mono">0.04 EUR per frame</td></tr>
<tr><td>[[ic:m-omni]]<b>Rewrite with Omni</b><br><span class="mono">omni</span></td>
    <td>Gemini Omni edits the moving footage directly, video to video.</td>
    <td>The element runs through the scene, and the footage is yours.</td><td class="mono">0.10 EUR per second</td></tr>
<tr><td>[[ic:m-bridge]]<b>Regenerate with Veo</b><br><span class="mono">bridge</span></td>
    <td>Both ends of the span are edited, and Veo 3.1 generates the motion between them.</td>
    <td>Genuine 3D motion, where a patch cannot hold.</td><td class="mono">1.88 to 3.68 EUR</td></tr>
</table>

<h3>How the automatic choice is made</h3>
<p>When an alert triggers a fix rather than a person, the planner picks by the observation's
own dimension, and it is a pure function with no model call: <span class="mono">relettering</span> for
[[ic:d-text_legibility]]on-screen text, <span class="mono">prop_swap</span> for an object,
<span class="mono">revoice</span> for a spoken line. It escalates to Omni when the scope
classifier says the element runs through the whole scene, or when the shot moves too much
for a patch to hold.</p>

<div class="panel warn"><h4>Veo is never chosen automatically</h4>
It regenerates pixels and costs real money, so a bridge only ever runs because an operator
picked it, or clicked a data link on a Grafana panel, and the day's budget allowed it.</div>

<div class="panel"><h4>What each method disturbs, measured</h4>
Collateral drift is masked PSNR inside the span, and it is written as a metric on every change.
A patch measures about 44 dB. Omni measures between 13 and 28 dB, because Omni re-renders the
shot. "Only what you name changes" is the instruction, not the pixels. If preserving the
untouched parts of a frame matters more than fixing it convincingly, prefer a patch.</div>
''')

# ------------------------------------------------------- 12 after you press
page(head("chapter 12", "After you press",
          "Three things stand between a generated edit and your master.") + '''
<h3>1. The craft gate</h3>
<p>Before anything is accepted, the staged file is measured: length, resolution, soundtrack,
and how much of the frame outside the edit box moved. A file that lost frames, lost
resolution, lost its audio or disturbed the rest of the picture is discarded, the master is
left untouched, and the finding goes back to open.</p>

<h3>2. The verifier</h3>
<p>The gate cannot tell you whether the edit is any good, only whether it broke something. So
the verifier re-runs the <b>real analyst pass</b> over the changed shots and asks the same
instrument that found the problem whether it still sees it. It then answers the second half:
did anything new break? An edit that removes a bottle can also remove the finding next to it,
or introduce one.</p>

<h3>3. Grafana closing its own alert</h3>
<p>When the finding is verified gone, its blocking metric drops to zero and the alert rule
that fired resolves itself. Nothing tells Grafana the fix worked. The number it was watching
simply stops being true.</p>

<div class="panel stop"><h4>A worked example of a bad fix, and why it was caught</h4>
A real change on a real run: asked to "replace each cigarette, cigar or tobacco pack", Omni
turned a cigar into a cigarette. One banned item for another, which is the smallest edit that
satisfies the sentence and no fix at all. The craft gate passed it, because almost nothing
outside the edit had moved, which is exactly what the gate measures. The verifier then
re-observed the shot, the tobacco rule fired again, and the finding went back to open. Nothing
was signed off. The instruction was what was wrong, and it now says out loud that the
replacement must not itself be alcohol, tobacco, a smoking device or a drug in any form.</div>
''')

# ------------------------------------------------------------ 13 the re-edit
page(head("chapter 13", "When the fix is wrong",
          "The verifier can only ask its own question. A fix can pass it and still be wrong to a person.") + '''
<p>An Omni rewrite once took a tobacco plug out of a hand and left the character holding
something rifle-shaped. It cleared its rule, because the rule was about tobacco. To a human
it was obviously not shippable.</p>

<p>So every change on the <b>my edits</b> screen carries a <b>re-edit this</b> control. You say
what is wrong with it in your own words, and your words become the instruction for the redo.</p>

<h3>How to write a good re-edit reason</h3>
<table>
<tr><th>Instead of</th><th>Write</th></tr>
<tr><td>"this is bad"</td><td>"he should not have a gun in his hands afterwards"</td></tr>
<tr><td>"wrong colour"</td><td>"the replacement bottle should be clear glass, not green"</td></tr>
<tr><td>"still visible"</td><td>"the ashtray on the table is still there in the second half"</td></tr>
</table>
<p>Name the object and where it is. The instruction is handed to the model as you wrote it.</p>

<div class="panel note"><h4>A patch is redone with Omni</h4>
Words only reach a model that is given the span. A patch method edits a still, so it cannot
act on "afterwards" or "in the second half". A re-edit of a patch is therefore promoted to
Omni, which changes the price. It is logged on the run either way, whatever comes back.</div>
''')

# -------------------------------------------------------------- 14 the guard
page(head("chapter 14", "When it will not edit",
          "When a rule is written on a protected characteristic, the honest answer is not an edit.") + f'''
<p>Two rules in the corpus carry a protected basis. A finding matching either is marked
<b>remediation blocked</b> with the verbatim reason <i>rule basis targets a protected
characteristic; human decision required</i>, and the console shows the statute beside it.</p>

<h3>What the Guard reads</h3>
<ul>
<li>The pack rule matched by its rule id.</li>
<li>The finding's own class.</li>
</ul>
<p>That is all. It never reads the rationale, the severity or any other model-authored field,
and it never calls a model. It is a pure function, which is what makes it
<b>un-promptable</b>: a crafted finding cannot argue its way past it. The refusal is enforced
a second time at the point of action, before a single frame is touched.</p>

<h3>[[ic:i-human]]The three ways out, and they are all a person's</h3>
<table>
<tr><th>Choice</th><th>What it means</th></tr>
<tr><td><b>Approve a manual edit</b></td><td>An editor fixes it in the suite. Take the markers.</td></tr>
<tr><td><b>Send to legal review</b></td><td>The statute goes with it. Recorded on the run.</td></tr>
<tr><td><b>Accept the risk</b></td><td>Air it as it is, knowingly. Also recorded, and printed on the certificate.</td></tr>
</table>

<div class="panel"><h4>Which rules carry it</h4>
A pack marks a rule with <span class="mono">protected_basis</span>. Two rules in the corpus
carry it today, both about who may be shown together, and both national law rather than a
broadcaster's preference. Any pack can set it, and any rule that sets it gets this treatment.</div>

<div class="panel note"><h4>Why this is a feature and not a gap</h4>
Guardrails belong in rule layers, not prompts. Anything a prompt grants, a prompt can take
away. A system that quietly edited people out of an ad to satisfy a rule about who may be
shown together would be doing something no operator asked for, in your name.</div>
''')

# ----------------------------------------------------- 15 cutting room
page(head("chapter 15", "Checking the result",
          "The original and the localized master, side by side.") + f'''
{shot("cutting-fr.png", "One pair per market. Both players open on the changed second.")}
<p>One edited master per market, written by the remediator and confirmed by the verifier. The
pairs are stacked by market and each opens on the second that changed rather than at zero, so
you are looking at the edit within a second of arriving.</p>

<h3>Generated content</h3>
<p>A second screen shows everything a model produced for the run: the stills a patch wrote, the
seconds Omni or Veo rendered, and for a bridge the two anchor frames it was handed. Those two
frames are the whole brief, so they are the only way to tell whether Veo invented something.</p>

<p><b>What to check:</b> is the violating thing gone, did anything else change, does the edit
hold for the whole span, and does the cut still work?</p>
''')

# --------------------------------------------------------------- 16 my edits
page(head("chapter 16", "Across every run",
          "My edits is the cross-run cutting room: what has this thing actually changed?") + '''
<p>Every edit the instance has made, newest first, with the frame before and the frame after
side by side and the rule that asked for it named. One card per scene, not per market, and the
card names the market whose objection paid for the render. Picture edits and sound edits are two sides of a toggle,
because one you watch and the other you listen to.</p>

<h3>Removing an edit</h3>
<p>This is the one control in the console that destroys evidence, so it asks for a word and
says exactly what it takes: the change record, both frames and any generated clip. There is
no undo, and they are the only copy.</p>
<ol>
<li>Open <b>remove</b> on the card.</li>
<li>Type the word and press <b>remove this edit</b>.</li>
<li>The card disappears at once. The deletion continues behind it.</li>
</ol>
<div class="panel"><h4>What a card shows you</h4>
The frame before and the frame after, the market that paid for the render, every market that
objected to those seconds, the method that made it, and the change id. Hovering plays both
sides together, so a fix that only works in one frame gives itself away immediately.</div>

<div class="panel note"><h4>The card going does not mean it is gone yet</h4>
Deleting a row, two stills and a rendered clip takes a moment, and a card that sat there
until it finished read as a button that did nothing. So the card is hidden the instant you
press, and the work carries on behind it. <b>If the server refuses</b>, because the word was
wrong or the change was already deleted, the card comes back with the reason under the form
and nothing was removed. A message you can read is the difference between an optimistic
interface and a lying one.</div>
''')

# ------------------------------------------------------------- 17 agent mode
page(head("chapter 17", "Asking in sentences",
          "A second agent, with ten tools and the whole console as its canvas.") + f'''
{shot("agent-answer.png", "It answers on the left and opens the evidence on the right.")}
<p>Agent mode answers by opening the view that proves the answer, and it prices any fix
before it runs. It reads every run in the instance, not just the one you are looking at.</p>

<h3>Questions that work well</h3>
<table>
<tr><th>Ask</th><th>You get</th></tr>
<tr><td>Why is France blocked, and what would a fix cost?</td><td>The rules, the seconds, the statutes, and a price per method.</td></tr>
<tr><td>Show me the frames FR-ALC-01 fired on</td><td>The frames themselves, opened beside the answer.</td></tr>
<tr><td>What should I fix first?</td><td>The shot that closes the most markets for the least money.</td></tr>
<tr><td>Which markets need a human?</td><td>The Guard's refusals, with their statutes.</td></tr>
<tr><td>Build me a dashboard for this run</td><td>A real Grafana dashboard, from the run's own labels.</td></tr>
</table>

<div class="panel warn"><h4>It spends, and it says so first</h4>
Asking costs a model call. Running a fix costs what the fix costs. The agent quotes before it
acts and refuses to spend past the day's ceiling, but it is not a free text box.</div>
''')

# ------------------------------------------- 18 library and search
page(head("chapter 18", "Looking things up",
          "Two reference screens: what the rules say, and where a thing appears.") + f'''
<h3>The rule library</h3>
{shot("10-library.png", "Every rule, filed under the observation that can trigger it.")}
<p>128 rules across 21 packs, each showing the market that holds it, the statute text, whether
it blocks or advises, and which of the eighteen dimensions can trigger it. This is the place
to answer "what would this market object to" before you have run anything.</p>

<h3>Frame search</h3>
<p>Every caption the analyst ever wrote is a Loki line, so "which frames show wine" is a
question rather than a feature somebody anticipated. The match is semantic: the question and
the candidate captions go to Gemini together, so <i>bunnies</i> finds
<i>an animated rabbit character</i> without anybody guessing which word a vision model chose
months ago. Each result says, in the model's own words, why it is there.</p>
<div class="panel"><h4>Two modes</h4>
Semantic is the default and costs a model call. <span class="mono">mode=literal</span> is a
regex over the caption alone and costs nothing: use it when you mean an exact string, a rule
id or a brand name.</div>
''')

# --------------------------------------------------------- 19 intelligence
page(head("chapter 19", "Reading across runs",
          "Every other screen answers a question about one commercial. This one reads across all of them.") + f'''
{shot("09-intelligence.png", "Seventeen panels and eight kinds of chart, from the same two stores.")}
<p>Which subject your creative keeps tripping over, which jurisdictions are actually hard,
the dimension against market matrix, the severity distribution, worst severity per market
against the line where a finding starts to block, market status over time, and the raw finding
stream underneath it all.</p>

<h3>The division of labour, and why it looks like this</h3>
<p>Grafana charts the data because it holds it. The console draws the axis because Grafana has
never heard of an eighteen-part taxonomy or a broadcaster channel's mark. The hero is one
block per commercial, lit to the highest severity any market ever recorded against it, with
that film's own first frame underneath it in the same order.</p>

<div class="panel note"><h4>This is where a pattern shows up before it is expensive</h4>
One blocked ad is a problem. The same dimension blocking four ads in a row is a brief that
needs changing, and it is only visible from here.</div>
''')

# ---------------------------------------------------------------- 20 grafana
page(head("chapter 20", "What it built in Grafana",
          "Not a report produced afterwards. The instrument panel is built during the run, "
          "by an agent, and it is what triggers the work.") + f'''
{shot("grafana-res.png", "The whole surface on one page, read from the definitions the agent provisions from.")}
<table>
<tr><th>What</th><th>How much</th></tr>
<tr><td>[[ic:logo-grafana]]Dashboards, built at runtime</td><td class="mono">9 boards, 40 panels</td></tr>
<tr><td>[[ic:logo-mimir]]Metric series to Mimir over OTLP</td><td class="mono">9 series</td></tr>
<tr><td>[[ic:logo-loki]]Log streams to Loki</td><td class="mono">3 kinds</td></tr>
<tr><td>[[ic:i-alert]]Alert rules</td><td class="mono">3</td></tr>
<tr><td>[[ic:i-pipeline]]Write operations, MCP where a tool exists</td><td class="mono">8</td></tr>
</table>
<p>The crew provisions its own folder, dashboards and alert rules through the official
<span class="mono">mcp-grafana</span> server. Where that release has no write tool for an
operation, it goes over the provisioning API instead and the inventory says so.</p>
<div class="panel note"><h4>Two clocks, and this is the thing people get wrong</h4>
One series is written on the <b>film's own clock</b>: video second n is pushed at wall second
t0 plus n, so a panel's x axis reads as the timecode because it is the timecode. The others
are stamped at the <b>real clock</b>, because an alert rule's evaluation window has to mean
what it says. Point the stock dashboards at "last 6 hours" and they will say No data. Pin the
range to the run.</div>
''')

page(head("chapter 20, continued", "The three alert rules",
          "Two ask Grafana to wake the crew. The third asks it to stop.") + '''
<table>
<tr><th>Rule</th><th>Fires when</th><th>What happens</th></tr>
<tr><td class="mono">customs-blocking</td>
    <td><span class="mono">customs_blocking</span> crosses 70 for an asset, market and rule</td>
    <td>The webhook wakes the remediator on that finding.</td></tr>
<tr><td class="mono">customs-market-down</td>
    <td>A market's status goes to at risk or blocked</td>
    <td>Recorded, and visible on the overview.</td></tr>
<tr><td class="mono">customs-budget-low</td>
    <td><span class="mono">customs_budget_remaining_eur</span> falls to the floor</td>
    <td>Automatic remediation pauses for the rest of the UTC day.</td></tr>
</table>
<p>The rules evaluate every thirty seconds. The contact point is a route on this service, and
the webhook reads <b>labels only</b>: it never trusts an annotation or a message body to tell
it which finding to spend money on.</p>

<h3>The series worth knowing</h3>
<table>
<tr><th>Series</th><th>Clock</th><th>One sample is</th></tr>
<tr><td class="mono">customs_risk</td><td>mapped</td><td>the worst severity in force at video second n</td></tr>
<tr><td class="mono">customs_market_status</td><td>current</td><td>0 cleared, 1 at risk, 2 blocked</td></tr>
<tr><td class="mono">customs_blocking</td><td>current</td><td>the severity of one open, sourced, blocking finding</td></tr>
<tr><td class="mono">customs_stage_error</td><td>current</td><td>how many times one stage failed on this asset</td></tr>
<tr><td class="mono">customs_collateral_drift</td><td>current</td><td>PSNR in dB inside the span of one edit</td></tr>
<tr><td class="mono">customs_spend_eur_total</td><td>current</td><td>what has been charged today</td></tr>
</table>
<div class="panel"><h4>Why the panels in the console are sometimes pictures</h4>
Grafana Cloud answers every embed attempt with an enforcing frame-ancestors policy, so it
cannot be put in an iframe. Where the companion viewer is deployed, the console frames live
panels reading the same stores. Where it is not, it renders server-side PNGs instead.</div>
''')

# ----------------------------------------------------------- 21 what you get
page(head("chapter 21", "What you get out",
          "Four artefacts. Two are for machines, two are for people.") + '''
<h3>The clearance certificate</h3>
<p>A PDF per market, and deliberately not a summary. Every finding the market raised is on it,
worst first, with the rule id, the class, the severity, the seconds it covers, the statute in
the regulator's own words, and the citation the adjudicator retrieved at judging time. A fixed
finding carries the method, the change id and the verifier's own sentence. A finding the Guard
refused says <b>human decision required</b> with the statute behind it, and the decision a
person made. An unsourced rule says that its severity was capped, and why.</p>
<p>It is the document an archive needs three years later when somebody asks why this cut
aired in Riyadh.</p>

<h3>The localized master</h3>
<p>One edited file per market, with only the failing seconds touched, checked by the craft
gate and signed off by the verifier. Downloadable from the cutting room.</p>

<h3>Timeline markers</h3>
<p>Two formats, because the two suites disagree about which they trust: a CSV with the columns
both of them look for, and CMX3600, the oldest interchange there is and the one Resolve imports most
reliably. Both carry the frame rate they were written at, read as the rational number the probe
reports rather than as a float, because 23.976 written as 24 drifts a frame a second and looks
perfectly plausible while it does it.</p>
<table>
<tr><th>Colour</th><th>Means</th></tr>
<tr><td><b>Red</b></td><td>A legal finding at or over the blocking line.</td></tr>
<tr><td><b>Green</b></td><td>The verifier already signed a fix off here.</td></tr>
<tr><td><b>Yellow</b></td><td>Everything else still open.</td></tr>
</table>
<p>A finding the Guard refused carries <b>HUMAN DECISION REQUIRED</b> in its own note, so the
editor reads why while they work.</p>
''')

# ------------------------------------------------------------- 22 the money
page(head("chapter 22", "The money",
          "The loop that can spend is the loop that needs a brake.") + '''
<p>Judging is cheap and generating is not. Every method shows its euro cost in the picker
before you press, and the agent quotes a fix before it starts one. Every charge is written to
Mimir on the real clock as it happens, so spend is a series you can chart rather than a number
somebody reconciles later.</p>

<h3>The daily ceiling</h3>
<table>
<tr><th>Control</th><th>Effect</th></tr>
<tr><td>Generation budget</td><td>A euro ceiling for the whole system, per UTC day.</td></tr>
<tr><td>Visitor cap</td><td>A much smaller ceiling for anyone through the visitor door.</td></tr>
<tr><td>Budget alert</td><td>Fires above the price of the most expensive single fix, and pauses automatic remediation.</td></tr>
</table>

<h3>What the pause does and does not do</h3>
<ul>
<li>Findings still block. Nothing is silently cleared.</li>
<li>Alerts still arrive and are recorded in the feed <b>with the reason</b>.</li>
<li>A person at the console can still spend what is left, one fix at a time.</li>
</ul>
<div class="panel note"><h4>Why the brake is on that path only</h4>
The pause is on the path nobody is watching, which is the only one that needed one. An
operator pressing a button has already seen the price. An alert firing at three in the morning
has not.</div>

<div class="panel warn"><h4>A rough sense of scale</h4>
A judging run across eight markets costs cents. A patch costs about four cents. An Omni
rewrite of a six second span costs about sixty cents. A Veo bridge costs between one and four
euro. A day's budget therefore buys somewhere between a dozen and two dozen bridges, or
hundreds of patches.</div>
''')

# ------------------------------------------------------------ 23 add a market
page(head("chapter 23", "Adding a market",
          "A market is a YAML file, not code. The pack format is the extension point.") + '''
<p>A pack names its market, its parent on the ladder, and its rules. Each rule names the
dimension that can trigger it, the class, the severity it carries, the statute in the
regulator's own words, and a citation reference that has to resolve.</p>
<pre>market: FR
name: France
parent: EU
rules:
  - id: FR-ALC-01
    dimension: alcohol_tobacco_drugs
    class: legal
    severity: 95
    trigger: alcohol is visible or being consumed on screen
    statute: >
      Loi Evin, Code de la sant&eacute; publique art. L3323-2: television
      advertising for alcoholic beverages is prohibited.
    citation_ref: legifrance.gouv.fr LEGIARTI000006688087</pre>

<h3>The four things a good rule has</h3>
<ol>
<li><b>One dimension.</b> If it needs two, it is two rules.</li>
<li><b>A trigger written as what the analyst would see</b>, not as what the law forbids. The
analyst never reads your rule; the adjudicator joins them.</li>
<li><b>A real statute</b>, quoted rather than paraphrased.</li>
<li><b>A citation that resolves.</b> If it does not, findings on this rule get capped severity
and can never trigger a fix, which is the system protecting you from your own typo.</li>
</ol>

<div class="panel"><h4>Inheritance is free</h4>
Declare a parent and you inherit everything above you. A broadcaster pack usually only needs
its own acceptance rules: its country and the continental and global rungs come with it.</div>
<div class="panel stop"><h4>protected_basis is not a severity dial</h4>
Setting it marks a rule as written on a protected characteristic, which routes every finding
on it to a person and blocks automatic editing. Use it for what it means, not to make a rule
feel important.</div>
''')

# ---------------------------------------------------------- 24 running it
page(head("chapter 24", "Running it yourself",
          "Install, configure, provision, deploy.") + '''
<h3>Prerequisites</h3>
<ul>
<li><span class="mono">ffmpeg</span> on the PATH, and Python 3.12.</li>
<li>A Google Cloud project with Vertex AI enabled.</li>
<li>A Grafana Cloud stack, with Mimir and Loki.</li>
</ul>

<h3>Install and run</h3>
<pre># 1. Environment
python3.12 -m venv .venv &amp;&amp; .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in the Google Cloud and Grafana values

# 2. Provision the Grafana surface: dashboards, all three alert rules,
#    the webhook contact point, the share links. Idempotent.
.venv/bin/python scripts/provision_grafana.py

# 3. Clear an ad from the terminal
.venv/bin/python scripts/run_pipeline.py docs/samples/test_ad.mp4 --markets FR,EU,AE

# 4. Or run the console and use a browser
.venv/bin/uvicorn customs.app:app --reload    # http://127.0.0.1:8000</pre>

<h3>Configuration worth knowing</h3>
<table>
<tr><th>Variable</th><th>What it sets</th></tr>
<tr><td class="mono">GOOGLE_CLOUD_PROJECT</td><td>The Vertex project every model call bills to.</td></tr>
<tr><td class="mono">GRAFANA_URL, GRAFANA_SA_TOKEN</td><td>The stack the crew provisions into.</td></tr>
<tr><td class="mono">LOKI_PUSH_URL, OTLP_URL</td><td>Where lines and metrics go.</td></tr>
<tr><td class="mono">WEBHOOK_TOKEN</td><td>The key on the alert contact point. Without it the webhook refuses.</td></tr>
<tr><td class="mono">JUDGE_PASSWORD, VISITOR_PASSWORD</td><td>The two doors.</td></tr>
<tr><td class="mono">EDITS_PASSWORD</td><td>The word that removes a change record.</td></tr>
<tr><td class="mono">VEO_MODEL, OMNI_MODEL</td><td>Pinned model ids, re-probed on a cold start.</td></tr>
</table>

''')

page(head("chapter 24, continued", "Deploying it",
          "Two scripts: the service, and the viewer that lets its panels be framed.") + '''
<pre>scripts/deploy.sh          # the service, secrets, IAM and the Grafana wiring
scripts/deploy_viewer.sh   # the embeddable Grafana viewer beside it</pre>
<p>The deploy script is idempotent and safe to re-run. It enables the APIs, mints the
secrets, sets the runtime service account, builds the container, deploys the revision, and
re-wires the Grafana contact point to the new URL.</p>
<h3>Two things it does to protect you</h3>
<ul>
<li><b>It refuses while work is in flight.</b> A deploy replaces the running container, so a
clearance or a fix mid-render would be discarded. It asks the live service first and stops if
the answer is yes.</li>
<li><b>It re-points the webhook.</b> The alert contact point has to name the service that is
actually running, or a blocking finding would fire an alert into nothing.</li>
</ul>
<div class="panel warn"><h4>Check the revision, not the exit code</h4>
After it finishes, confirm the serving revision actually changed. A build can succeed while
the service keeps serving the old one, and the script's exit code will not tell you.</div>
<div class="panel"><h4>Where state lives</h4>
Runs survive a deploy: the store is mirrored to object storage and restored on boot. A run
killed mid-flight by a deploy is still lost, which is the other reason the script refuses.</div>
''')

# -------------------------------------------------------- 25 troubleshooting
page(head("chapter 25", "When something goes wrong",
          "The failures this system actually has, and what each one means.") + f'''
<table>
<tr><th>What you see</th><th>What it is</th><th>What to do</th></tr>
<tr><td>A dashboard says <b>No data</b></td><td>The time range is wall clock, but the run's risk series is on the film's clock.</td><td>Pin the range to the run, or open the panel from the console.</td></tr>
<tr><td>[[ic:m-omni]]<b>Omni refused the input</b></td><td>The footage contains recognisable third-party content.</td><td>Nothing was charged, and a retry cannot help. Use a patch method, or your own footage.</td></tr>
<tr><td>[[ic:m-omni]]<b>Omni refused the output</b></td><td>The instruction asked for something it will not render, often about a photorealistic person.</td><td>Rewrite the instruction. The footage was fine; the ask was not.</td></tr>
<tr><td>[[ic:m-bridge]]<b>Veo refused the frames</b></td><td>Its filter read the shot as depicting a public figure.</td><td>Never charged. The footage is the refusal.</td></tr>
<tr><td>A fix passed and the finding reopened</td><td>The verifier saw the violation survive, or something new break.</td><td>Read the new finding's words. Often the method could not reach it. Chapter 11.</td></tr>
<tr><td>A fix looks wrong but cleared</td><td>The rule was satisfied and a person still would not ship it.</td><td>Use re-edit and say what is wrong. Chapter 13.</td></tr>
<tr><td>Everything is slow, then a stage errors</td><td>One instance, one writer thread. Two concurrent multi-market clearances are too much.</td><td>Run them one at a time.</td></tr>
<tr><td>The deploy refuses</td><td>The live service reports work in flight.</td><td>Wait. Forcing it discards a real clearance or a fix mid-render.</td></tr>
<tr><td>A fix seems stuck for a long time</td><td>Image generation quota is a small number of requests a minute, project wide.</td><td>Per-frame methods are slow by arithmetic. Prefer Omni for long spans.</td></tr>
</table>

<div class="panel note"><h4>The general rule</h4>
Every stage that fails says so in the mission feed and counts itself as a metric. A tool that
silently skips a shot is worse than one that admits it, so if the feed is quiet the work
really is still running.</div>
''')

# ------------------------------------------------------------- 26 reference
page(head("chapter 26", "Reference",
          "Routes, stores and streams, for looking something up quickly.") + '''
<h3>Console routes</h3>
<table>
<tr><th class="mono">Route</th><th>Screen</th><th>Gated</th></tr>
<tr><td class="mono">/</td><td>[[ic:i-human]]Front door</td><td>no</td></tr>
<tr><td class="mono">/runs</td><td>[[ic:n-runs]]Archive</td><td>no</td></tr>
<tr><td class="mono">/new</td><td>[[ic:n-new]]New clearance</td><td>spends</td></tr>
<tr><td class="mono">/runs/{id}</td><td>[[ic:n-board]]Launch board</td><td>no</td></tr>
<tr><td class="mono">/runs/{id}/mission</td><td>[[ic:n-feed]]Mission feed</td><td>no</td></tr>
<tr><td class="mono">/runs/{id}/frames</td><td>[[ic:i-frame]]Frame board</td><td>no</td></tr>
<tr><td class="mono">/runs/{id}/timeline</td><td>[[ic:n-timeline]]Timeline</td><td>no</td></tr>
<tr><td class="mono">/runs/{id}/markets/{m}</td><td>[[ic:n-market]]Market room</td><td>no</td></tr>
<tr><td class="mono">/runs/{id}/cutting</td><td>[[ic:n-cut]]Cutting room</td><td>no</td></tr>
<tr><td class="mono">/runs/{id}/generated</td><td>[[ic:i-remediator]]Generated content</td><td>no</td></tr>
<tr><td class="mono">/edits</td><td>[[ic:n-cut]]My edits</td><td>removal spends</td></tr>
<tr><td class="mono">/agent</td><td>[[ic:i-adjudicator]]Agent mode</td><td>spends</td></tr>
<tr><td class="mono">/library</td><td>[[ic:n-library]]Rule library</td><td>no</td></tr>
<tr><td class="mono">/search</td><td>[[ic:i-frame]]Frame search</td><td>no</td></tr>
<tr><td class="mono">/insight</td><td>[[ic:i-analyst]]Intelligence</td><td>no</td></tr>
<tr><td class="mono">/grafana</td><td>[[ic:logo-grafana]]Grafana resources</td><td>no</td></tr>
<tr><td class="mono">/webhook/alert</td><td>[[ic:i-alert]]The alert contact point</td><td>keyed</td></tr>
</table>

''')

page(head("chapter 26, continued", "Reference: the stores",
          "What the crew writes, and how to read it back.") + '''
<h3>Log streams in Loki</h3>
<table>
<tr><th class="mono">kind</th><th>One line is</th><th>Labels</th></tr>
<tr><td class="mono">[[ic:i-analyst]]observation</td><td>one keyframe the analyst described</td><td>app, asset, dimension, flagged</td></tr>
<tr><td class="mono">[[ic:i-legal]]finding</td><td>the join, with the statute in it</td><td>app, asset, market, rule_id</td></tr>
<tr><td class="mono">[[ic:i-cleared]]verdict</td><td>one market's answer</td><td>app, asset, market</td></tr>
</table>
<div class="panel"><h4>Per market, without a scan</h4>
Because market and rule id are labels rather than fields, one market's findings are a
selector: <span class="mono">{app="customs", kind="finding", market="FR"}</span>.</div>

<h3>The two clocks, once more</h3>
<table>
<tr><th>Clock</th><th>Series</th><th>Read by</th></tr>
<tr><td><b>Mapped</b>, the film's own</td><td class="mono">customs_risk</td><td>The timeline, the lane strip, the grid.</td></tr>
<tr><td><b>Current</b>, the wall clock</td><td class="mono">customs_market_status, customs_blocking, customs_stage_error, customs_spend_eur_total</td><td>The overview, and all three alert rules.</td></tr>
</table>
<p>Alerting reads only the current-clock series, because an evaluation window has to mean what
it says. The timecode axis reads only the mapped one.</p>

<div class="panel note"><h4>If you remember one thing from this manual</h4>
Every screen here is a different distance from the same question: what is in this film, and
who objects to it? The findings are joins you can take apart, the fixes are priced before they
run, and nothing is signed off because a model said so.</div>
''')

# --------------------------------------------------------------- closing
page(f'''
<img class="mark" src="{IMG}logo.png">
<h1 style="font-size:20pt;letter-spacing:0.16em;font-family:Poppins,Inter,sans-serif">THE MEDIA CUSTOMS</h1>
<p class="sub" style="color:#445a66">One asset, every market, before it ships.</p>
<div class="brandbar" style="justify-content:center"><i></i><i></i><i></i><i></i></div>
<div class="meta">
  <p style="margin-top:8mm">The console, live and open to read:<br>
  <span class="mono">customs-app-akap4ao72a-ew.a.run.app</span></p>
  <p>The code:<br><span class="mono">github.com/tdries/devpost-hackathon-agentic-cinema</span></p>
  <p style="margin-top:6mm;color:#7d9199">Built on Google Cloud and Grafana. Operator manual v1.0.</p>
</div>
''', cls="page cover")

# --------------------------------------------------------- fill in the contents
# Every chapter page announces itself with an eyebrow and a title, so the
# contents can be read off the pages rather than kept in step by hand. A
# "continued" page belongs to the chapter above it and gets no entry.
entries = []
for idx, (cls, body, _fl) in enumerate(pages):
    m = re.search(r'<p class="eyebrow">chapter (\d+)</p><h2>(.*?)</h2>'
                  r'(?:<p class="lede">(.*?)</p>)?', body)
    if not m:
        continue
    entries.append((m.group(1), m.group(2), (m.group(3) or "").split(".")[0],
                    idx + 1))          # +1 for the contents page inserted at TOC_AT

rows = "".join(
    f'<a><span class="n">{n}</span><span class="t">{t}</span>'
    f'<span class="d">{d.lower()}</span><span class="p">{pg}</span></a>'
    for n, t, d, pg in entries)
pages[TOC_AT] = ("page", head("contents", "What is in this manual",
    "It is written to be read in order once, then opened at the chapter you need. Every "
    "screen it describes is live, and every number in it was read from the running system "
    "rather than typed from memory.") + f'<div class="toc">{rows}</div>',
    "The Media Customs")

# ------------------------------------------------------------------- render
BODY = "\n".join(
    f'<section class="{cls}">{body}'
    + ("" if "cover" in cls else
       f'<div class="folio"><span>{fl}</span><span>{i}</span></div>')
    + "</section>"
    for i, (cls, body, fl) in enumerate(pages, 1))

BODY = re.sub(r'\[\[ic:([a-z0-9_-]+)\]\]',
              lambda m: f'<img class="ico" src="{IMG}ico/{m.group(1)}.png">&nbsp;', BODY)

HTML = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>The Media Customs, operator manual</title>
<link rel="stylesheet" href="manual.css"></head><body>{BODY}</body></html>'''

out = "docs/manual-white"
open(f"{out}/manual.html", "w").write(HTML)
print(f"{len(pages)} pages written to {out}/manual.html")

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                "--print-to-pdf=" + str(__import__("pathlib").Path(
                    "docs/The-Media-Customs-manual.pdf").resolve()),
                "file://" + str(__import__("pathlib").Path(f"{out}/manual.html").resolve())],
               check=True, capture_output=True)
print("rendered docs/The-Media-Customs-manual.pdf")
