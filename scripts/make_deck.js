// The Media Customs: the full deck, one slide per screen and per capability.
//   node scripts/make_deck.js  ->  docs/The-Media-Customs-explained.pptx
const pptxgen = require("pptxgenjs");
const fs = require("fs");

const INK = "172125", MUTED = "4A5B63", HAIR = "D5E3E8", PAPER = "F7F9FA";
const BLUE = "4285F4", RED = "EA4335", YEL = "FBBC05", GRN = "34A853", ORA = "F46800";
const MONO = "Courier New", HEAD = "Arial", BODY = "Calibri";
const IMG = "docs/deck/img/";
const W = 13.333, H = 7.5;

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
pres.author = "Tim Dries";
pres.title = "The Media Customs";

const shadow = () => ({ type: "outer", color: "8FA3AB", blur: 14, offset: 3, angle: 90, opacity: 0.32 });

function dark(slide) { slide.background = { color: INK }; }
function light(slide) { slide.background = { color: PAPER }; }

// the repeated furniture: a mono kicker, a big title, an optional deck
function head(slide, kicker, title, opts = {}) {
  const onDark = opts.onDark || false;
  const x = opts.x === undefined ? 0.6 : opts.x;
  slide.addText(kicker.toUpperCase(), {
    x: x + 0.02, y: opts.y || 0.42, w: opts.tw || 9, h: 0.3, isTextBox: true, margin: 0,
    fontFace: MONO, fontSize: 12, bold: true, charSpacing: 2,
    color: onDark ? "8FA6AF" : MUTED,
  });
  slide.addText(title, {
    x, y: (opts.y || 0.42) + 0.36, w: opts.tw || 12.1, h: opts.th || 0.85,
    isTextBox: true, margin: 0, valign: "top", fontFace: HEAD, fontSize: opts.size || 34, bold: true,
    color: onDark ? "FFFFFF" : INK, lineSpacingMultiple: 0.95,
  });
}

// a screenshot, framed like the console frames its own evidence
const SIZES = JSON.parse(fs.readFileSync(IMG + "sizes.json", "utf8"));
function shot(slide, file, x, y, w, maxH) {
  const key = file.split("/").pop();
  const [iw, ih] = SIZES[key] || [1920, 1080];
  let h = (w * ih) / iw;
  if (maxH && h > maxH) {
    const full = w;
    h = maxH; w = (h * iw) / ih;
    x += (full - w) / 2;            // centred in the space it was given
  }
  slide.addShape(pres.ShapeType.roundRect, {
    x: x - 0.07, y: y - 0.07, w: w + 0.14, h: h + 0.14, rectRadius: 0.06,
    fill: { color: "FFFFFF" }, line: { color: HAIR, width: 1 }, shadow: shadow(),
  });
  slide.addImage({ path: file, x, y, w, h });
  return h;
}

function bullets(slide, items, x, y, w, size = 14) {
  slide.addText(items.map((t, i) => ({
    text: t, options: { bullet: true, breakLine: i < items.length - 1 },
  })), {
    x, y, w, h: 3.4, isTextBox: true, margin: 0, valign: "top", fontFace: BODY, fontSize: size,
    color: INK, lineSpacing: size * 1.45, paraSpaceAfter: 8, valign: "top",
  });
}

function card(slide, x, y, w, h, fill = "FFFFFF") {
  slide.addShape(pres.ShapeType.roundRect, {
    x, y, w, h, rectRadius: 0.05, fill: { color: fill },
    line: { color: HAIR, width: 1 }, shadow: shadow(),
  });
}

function stat(slide, x, y, w, big, label, colour = INK) {
  slide.addText(big, {
    x, y, w, h: 0.85, isTextBox: true, margin: 0, valign: "top", fontFace: HEAD, fontSize: 46,
    bold: true, color: colour, align: "left",
  });
  slide.addText(label, {
    x, y: y + 0.82, w, h: 0.6, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 13, color: MUTED,
  });
}

function section(n, kicker, title, blurb) {
  const s = pres.addSlide(); dark(s);
  s.addText(n, {
    x: 0.62, y: 2.1, w: 3, h: 1.6, isTextBox: true, margin: 0, valign: "top", fontFace: HEAD,
    fontSize: 120, bold: true, color: "22333A",
  });
  s.addText(kicker.toUpperCase(), {
    x: 3.4, y: 2.5, w: 9, h: 0.3, isTextBox: true, margin: 0, valign: "top", fontFace: MONO,
    fontSize: 12, bold: true, charSpacing: 2, color: "8FA6AF",
  });
  s.addText(title, {
    x: 3.38, y: 2.72, w: 9.2, h: 1.5, isTextBox: true, margin: 0, valign: "top",
    fontFace: HEAD, fontSize: 34, bold: true, color: "FFFFFF", lineSpacingMultiple: 1.0,
  });
  s.addText(blurb, {
    x: 3.4, y: 4.32, w: 8.6, h: 1.1, isTextBox: true, margin: 0, valign: "top",
    fontFace: BODY, fontSize: 15, color: "B6C6CD", lineSpacing: 22,
  });
  [BLUE, RED, YEL, GRN].forEach((c, i) => s.addShape(pres.ShapeType.rect, {
    x: 3.4 + i * 0.62, y: 5.62, w: 0.5, h: 0.06, fill: { color: c }, line: { width: 0 },
  }));
  return s;
}

// one screen of the console: what it answers, and the screen itself
let screenNo = 0;
function screen(name, path, lines, image, note) {
  screenNo += 1;
  const s = pres.addSlide(); light(s);
  const left = screenNo % 2 === 1;
  const tx = left ? 0.62 : 8.62;
  const ix = left ? 4.95 : 0.6;
  head(s, `${String(screenNo).padStart(2, "0")} ${path}`, name,
       { x: tx, tw: 3.62, th: 0.8, size: 26 });
  bullets(s, lines, tx, 1.78, 3.62, 13.5);
  const ih = shot(s, IMG + image, ix, 1.35, 7.7, 4.55);
  void ih;
  if (note) {
    s.addText(note, {
      x: tx, y: 5.55, w: 3.62, h: 1.1, isTextBox: true, margin: 0, valign: "top",
      fontFace: MONO, fontSize: 10.5, color: MUTED, lineSpacing: 15,
    });
  }
  return s;
}

/* ------------------------------------------------------------------ 1 title */
{
  const s = pres.addSlide(); dark(s);
  s.addImage({ path: IMG + "logo.png", x: 5.62, y: 1.15, w: 2.1, h: 1.06 });
  s.addText("T H E   M E D I A   C U S T O M S", {
    x: 0.6, y: 2.45, w: 12.1, h: 0.9, isTextBox: true, margin: 0, valign: "top", fontFace: HEAD,
    fontSize: 42, bold: true, color: "FFFFFF", align: "center",
  });
  s.addText("An agentic ad-clearance crew. It watches your commercial once, judges it against every market in parallel, and re-renders the shots that fail.", {
    x: 2.4, y: 3.45, w: 8.5, h: 0.8, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 15, color: "B6C6CD", align: "center", lineSpacing: 23,
  });
  [BLUE, RED, YEL, GRN].forEach((c, i) => s.addShape(pres.ShapeType.rect, {
    x: 5.35 + i * 0.68, y: 4.35, w: 0.56, h: 0.07, fill: { color: c }, line: { width: 0 },
  }));
  const marks = [["google.png", "Google Cloud"], ["adk.png", "Agent Builder"],
                 ["omni.png", "Gemini Omni"], ["veo.png", "Veo 3.1"], ["grafana.png", "Grafana"]];
  marks.forEach(([f, label], i) => {
    const x = 2.35 + i * 1.83;
    s.addImage({ path: IMG + f, x: x + 0.42, y: 5.05, w: 0.62, h: 0.62, sizing: { type: "contain", w: 0.62, h: 0.62 } });
    s.addText(label, { x, y: 5.78, w: 1.46, h: 0.3, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 10.5, color: "8FA6AF", align: "center" });
  });
  s.addText("Agentic Cinema hackathon  ·  Grafana Labs track  ·  Tim Dries", {
    x: 0.6, y: 6.62, w: 12.1, h: 0.3, isTextBox: true, margin: 0, valign: "top", fontFace: MONO,
    fontSize: 11, color: "6F868F", align: "center",
  });
  s.addNotes("The Media Customs. One asset, every market, before it ships.");
}

/* ---------------------------------------------------------------- 2 problem */
{
  const s = pres.addSlide(); light(s);
  head(s, "the problem", "One master, twenty markets, six of them checked");
  s.addText("A commercial is cut once and shipped everywhere. The regulated half already carries an invoice, and the unregulated half costs more, because nobody checks it until the public does.", {
    x: 0.6, y: 1.75, w: 6.1, h: 1.3, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 15, color: INK, lineSpacing: 23,
  });
  const risks = [
    ["alcohol_tobacco_drugs", "Alcohol on screen", "France will not broadcast it. Loi Evin, Code de la sante publique L3323-2.", RED],
    ["gesture_body_language", "A thumbs up", "Reads as an insult across the Gulf and parts of West Africa.", BLUE],
    ["comparative_claims", "A claim on the pack", "Comparative advertising has to be substantiated to air in France.", GRN],
  ];
  risks.forEach(([icon, title, body, colour], i) => {
    const y = 3.25 + i * 1.28;
    card(s, 0.6, y, 6.1, 1.12);
    s.addShape(pres.ShapeType.ellipse, { x: 0.85, y: y + 0.26, w: 0.6, h: 0.6,
      fill: { color: PAPER }, line: { color: colour, width: 1.5 } });
    s.addImage({ path: IMG + "dims/" + icon + ".png", x: 0.97, y: y + 0.38, w: 0.36, h: 0.36 });
    s.addText(title, { x: 1.62, y: y + 0.2, w: 4.9, h: 0.32, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 15, bold: true, color: INK });
    s.addText(body, { x: 1.62, y: y + 0.55, w: 4.9, h: 0.5, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 12, color: MUTED, lineSpacing: 16 });
  });
  shot(s, IMG + "problem-shot.png", 7.15, 1.75, 5.55);
  s.addText("Every one of those is fine somewhere and illegal somewhere else. Finding out means a lawyer per market, a broadcaster's reviewer, and luck.", {
    x: 7.15, y: 5.05, w: 5.55, h: 1.1, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 13.5, color: INK, lineSpacing: 20, italic: true,
  });
}

/* --------------------------------------------------------------- 3 what it is */
{
  const s = pres.addSlide(); light(s);
  head(s, "what it is", "You hand it a master. It hands back a decision, and a version that can air.");
  shot(s, IMG + "01-landing.png", 0.6, 2.35, 8.35, 4.2);
  const points = [
    ["98", "jurisdictions judged in parallel, on one fact sheet", BLUE],
    ["128", "rules, every one naming a real statute or code", RED],
    ["5", "ways to fix a failing shot, each priced before it runs", YEL],
    ["3", "alert rules the agent writes itself in Grafana", GRN],
  ];
  points.forEach(([n, label, colour], i) => {
    const y = 2.0 + i * 1.15;
    s.addText(n, { x: 8.6, y, w: 1.15, h: 0.7, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 34, bold: true, color: colour, align: "right" });
    s.addText(label, { x: 9.9, y: y + 0.12, w: 2.85, h: 0.75, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 12.5, color: INK, lineSpacing: 17 });
  });
}

/* ------------------------------------------------------- 4 observe once idea */
{
  const s = pres.addSlide(); light(s);
  head(s, "the core idea", "Observe once, judge many");
  s.addText("Looking at the film is the expensive part. Opinions about what was seen are cheap. So the two never mix.", {
    x: 0.6, y: 1.72, w: 12.1, h: 0.5, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 15, color: MUTED,
  });
  const steps = [
    ["One commercial", "a file, or a link", "8FA3AB"],
    ["The analyst", "watches each shot ONCE, Gemini vision", YEL],
    ["Neutral observations", "timecoded, boxed, no verdicts, 18 dimensions", GRN],
    ["98 adjudicators", "one per jurisdiction, in parallel, own rulebook", BLUE],
    ["Findings", "observation x rule x a citation that resolves", RED],
  ];
  steps.forEach(([t, b, colour], i) => {
    const x = 0.6 + i * 2.47;
    card(s, x, 2.55, 2.25, 1.85);
    s.addShape(pres.ShapeType.rect, { x: x + 0.22, y: 2.78, w: 0.42, h: 0.07,
      fill: { color: colour }, line: { width: 0 } });
    s.addText(t, { x: x + 0.22, y: 2.95, w: 1.85, h: 0.5, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 13.5, bold: true, color: INK });
    s.addText(b, { x: x + 0.22, y: 3.42, w: 1.85, h: 0.85, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 11, color: MUTED, lineSpacing: 15 });
    if (i < 4) s.addText("»", { x: x + 2.3, y: 3.25, w: 0.2, h: 0.3, isTextBox: true,
      margin: 0, fontFace: HEAD, fontSize: 16, color: "9FB3BA" });
  });
  s.addText([
    { text: "The analyst is forbidden an opinion. ", options: { bold: true } },
    { text: "It writes \"a woman raises a glass of red wine\", never \"this violates French law\". Ninety-eight adjudicators then argue about that one sentence, simultaneously, each holding a different rulebook. Which is what makes \"is this fact wrong?\" and \"is this rule wrong?\" separable questions." },
  ], { x: 0.6, y: 4.85, w: 12.1, h: 1.1, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
       fontSize: 14, color: INK, lineSpacing: 22 });
  s.addText("A finding is always a join. Break any leg of it and the finding is not made.", {
    x: 0.6, y: 6.1, w: 12.1, h: 0.4, isTextBox: true, margin: 0, valign: "top", fontFace: MONO,
    fontSize: 12, color: MUTED,
  });
}

/* ------------------------------------------------------------------ 5 flow */
{
  const s = pres.addSlide(); light(s);
  head(s, "the whole loop", "What happens between the upload and the master");
  shot(s, IMG + "flow.png", 0.6, 2.2, 7.5, 3.7);
  const steps = [
    "You hand it a master.",
    "Ingest cuts it into shots and pulls the audio and a transcript.",
    "The analyst watches once and writes timecoded, neutral observations, into Loki as they are made.",
    "That one fact sheet goes to every market at once, each judged against its own rulebook with a grounded citation.",
    "Each finding pushes a severity metric to Mimir on the film's own clock, and an annotation onto the dashboard.",
    "The publisher agent builds the dashboards and the alert rules itself, through the Grafana MCP server.",
    "A blocking finding trips its rule and the webhook wakes the remediator. The fix starts because a chart said so.",
    "The verifier re-observes with the same instrument. The metric drops and Grafana resolves its own alert.",
  ];
  steps.forEach((t, i) => {
    const y = 1.75 + i * 0.62;
    s.addText(String(i + 1), { x: 8.45, y, w: 0.32, h: 0.3, isTextBox: true, margin: 0,
      fontFace: MONO, fontSize: 12, bold: true, color: [BLUE, BLUE, YEL, YEL, ORA, ORA, RED, GRN][i] });
    s.addText(t, { x: 8.85, y: y - 0.03, w: 3.9, h: 0.6, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 11.5, color: INK, lineSpacing: 15 });
  });
  s.addText("The diagram is a screen in the product, and it plays itself.", {
    x: 0.6, y: 6.05, w: 7.5, h: 0.35, isTextBox: true, margin: 0, valign: "top", fontFace: MONO,
    fontSize: 11, color: MUTED,
  });
}

/* ---------------------------------------------------------------- 6 ladder */
{
  const s = pres.addSlide(); light(s);
  head(s, "the market model", "A ladder, not a list");
  s.addText("A market inherits from the one above it. Pick a Belgian broadcaster and you get twelve rules nobody wrote in one place.", {
    x: 0.6, y: 1.72, w: 12.1, h: 0.5, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 15, color: MUTED,
  });
  const rungs = [
    ["GLOBAL", "1 baseline", "what almost every market agrees on", 1.0, GRN],
    ["CONTINENTAL", "1, the EU", "the AVMSD, on top of the baseline", 1.9, YEL],
    ["NATIONAL", "16 countries", "the law of the land, and its regulator", 2.8, BLUE],
    ["CHANNEL", "80 broadcasters", "a channel's own acceptance rules, on top of its country", 3.7, RED],
  ];
  rungs.forEach(([name, count, blurb, reach, colour], i) => {
    const y = 2.55 + i * 1.06;
    card(s, 0.6, y, 10.0, 0.88);
    // the rung's reach, drawn rather than described
    s.addShape(pres.ShapeType.roundRect, { x: 0.85, y: y + 0.62, w: reach, h: 0.11,
      rectRadius: 0.05, fill: { color: colour }, line: { width: 0 } });
    s.addText(name, { x: 0.85, y: y + 0.13, w: 2.4, h: 0.32, isTextBox: true, margin: 0,
      valign: "top", fontFace: MONO, fontSize: 13, bold: true, color: colour });
    s.addText(count, { x: 4.0, y: y + 0.13, w: 2.2, h: 0.32, isTextBox: true, margin: 0,
      valign: "top", fontFace: HEAD, fontSize: 13.5, bold: true, color: INK });
    s.addText(blurb, { x: 6.2, y: y + 0.15, w: 4.1, h: 0.6, isTextBox: true, margin: 0,
      valign: "top", fontFace: BODY, fontSize: 12, color: MUTED, lineSpacing: 16 });
  });
  stat(s, 10.95, 2.62, 2.1, "21", "market packs on disk", INK);
  stat(s, 10.95, 4.05, 2.1, "98", "selectable jurisdictions", BLUE);
  stat(s, 10.95, 5.48, 2.1, "128", "rules, all cited", RED);
  s.addText("A market is a YAML file, not code. Adding one is writing its rules down.", {
    x: 0.6, y: 6.85, w: 9, h: 0.35, isTextBox: true, margin: 0, valign: "top", fontFace: MONO,
    fontSize: 11, color: MUTED,
  });
}

/* ------------------------------------------------------------ 7 taxonomy */
{
  const s = pres.addSlide(); light(s);
  head(s, "the vocabulary", "Eighteen dimensions, and nothing outside them");
  s.addText("The analyst may only emit these, and every one of the 128 rules is written against one of them. That is what makes the join between a fact and a market's opinion a lookup rather than an argument.", {
    x: 0.6, y: 1.7, w: 12.1, h: 0.6, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 14, color: MUTED, lineSpacing: 20,
  });
  const dims = [
    ["alcohol_tobacco_drugs", "Alcohol, tobacco, drugs"], ["religious_symbols_practices", "Religious symbols"],
    ["modesty_dress_body", "Modesty and dress"], ["gesture_body_language", "Gesture"],
    ["food_and_animals", "Food and animals"], ["gender_portrayal", "Gender portrayal"],
    ["sexual_orientation_gender_id", "Orientation, gender id"], ["children_and_minors", "Children and minors"],
    ["national_symbols_politics", "National symbols"], ["health_claims_pharma", "Health claims"],
    ["gambling_and_finance", "Gambling and finance"], ["violence_and_weapons", "Violence and weapons"],
    ["language_profanity_idiom", "Language and idiom"], ["humour_irony_satire", "Humour and satire"],
    ["superstition_number_colour", "Superstition, colour"], ["photosensitivity_sensory", "Photosensitivity"],
    ["text_legibility", "On-screen text"], ["comparative_claims", "Comparative claims"],
  ];
  dims.forEach(([file, label], i) => {
    const col = i % 6, row = Math.floor(i / 6);
    const x = 0.6 + col * 2.06, y = 2.6 + row * 1.42;
    card(s, x, y, 1.92, 1.2);
    s.addImage({ path: IMG + "dims/" + file + ".png", x: x + 0.14, y: y + 0.16, w: 0.42, h: 0.42 });
    s.addText(label, { x: x + 0.14, y: y + 0.66, w: 1.66, h: 0.44, isTextBox: true,
      margin: 0, fontFace: BODY, fontSize: 10.5, color: INK, lineSpacing: 13 });
  });
}

section("01", "part one", "The console, screen by screen",
        "Fifteen screens, each one a way of asking the same question at a different distance: what is in this film, and who objects to it?");

/* --------------------------------------------------------- 8..22 the screens */
screen("The front door", "/", [
  "Two doors: read everything with no password, or bring a commercial and spend.",
  "The strip underneath is a fix that already landed, rotating through three markets.",
  "The ring on the tour door is the working light the whole product uses.",
], "01-landing.png", "Reading is never gated. Only spending is.");

screen("Archive", "/runs", [
  "Every clearance this instance has performed, newest first.",
  "Hover a card and the whole commercial plays as a five second timelapse.",
  "Under it, one lane per category and a mark wherever something was found.",
], "archive.png", "Each card's lanes are a live Grafana panel.");

screen("New clearance", "/new", [
  "Hand it a file, pick the markets, and watch it clear customs live.",
  "The ladder is the picker: baseline, continent, country, broadcaster.",
  "Every run is priced against the day's budget before it starts.",
], "launcher.png", "A 30 second spot takes about four minutes.");

screen("Mission feed", "/runs/{id}/mission", [
  "Every move the crew made, in the order it made them.",
  "Each stage says in plain words what it is doing, with its own shorthand underneath.",
  "Server-sent events, so it is the run happening rather than a log you refresh.",
], "05-mission-feed.png", "The flow diagram, actually running.");

screen("Launch board", "/runs/{id}", [
  "The verdict, market by market, flipping in place as each adjudicator returns.",
  "Cleared, at risk or blocked, with the worst finding on the face of the tile.",
  "Underneath, the crew's own lanes dashboard, live and clickable.",
], "03-launch-board.png", "Four markets clear, France and China block.");

screen("Frame board", "/runs/{id}/frames", [
  "Every scene the crew looked at, how it opens and how it closes.",
  "The neutral sentences the analyst wrote, before any market saw them.",
  "This is the fact sheet all 98 adjudicators are arguing about.",
], "06-frame-board.png", "Facts here. Opinions elsewhere.");

screen("Timeline", "/runs/{id}/timeline", [
  "One grid drawn by two systems: our icons and scene thumbnails as the axes.",
  "The squares between them are Grafana's own status history panel, live.",
  "Click a square and it starts a priced fix on the scene under it.",
], "timeline.png", "A column's width is its share of the film.");

screen("Market room", "/runs/{id}/markets/FR", [
  "One panel per scene, with every finding written against it.",
  "The triggering frame, the rule id, the class, the severity and the window.",
  "The statute in the regulator's own words, and the citation resolved at judging time.",
], "07-market-room.png", "Not a summary. Evidence.");

screen("The fix panel", "inside the market room", [
  "Two questions: what should change, and how it should be done.",
  "Five methods, each priced in euro, each saying where it is a poor fit.",
  "When a patch cannot reach the violation, it says so and offers Veo instead.",
], "fixpanel.png", "Nothing is spent until you press.");

screen("Cutting room", "/runs/{id}/cutting", [
  "The original and the localized master, playing in lockstep.",
  "Both players open on the second that changed.",
  "One edited master per market, written by the remediator, signed off by the verifier.",
], "cutting-fr.png", "Same terrace, same people. Only the drink changed.");

screen("Agent mode", "/agent", [
  "A second ADK surface: one agent with ten tools.",
  "Ask in sentences and it opens the evidence beside you.",
  "It prices any fix before it runs, and refuses to spend past the day's ceiling.",
], "agent-answer.png", "Ten tools, and the whole console as its canvas.");

screen("Rule library", "/library", [
  "Every rule, filed under the observation that can trigger it.",
  "The statute text, the market that holds it, and whether it blocks or advises.",
  "Two rules carry a protected basis, and the Guard reads only that field.",
], "10-library.png", "128 rules, 18 dimensions, 21 packs.");

screen("Frame search", "/search", [
  "Every caption the analyst ever wrote is a Loki line.",
  "So \"which frames show wine\" is a question, not a feature somebody anticipated.",
  "The match is semantic: question and captions go to Gemini together.",
], "search.png", "Across every run this instance has done.");

screen("Intelligence", "/insight", [
  "Every other screen answers a question about one commercial. This one reads across all of them.",
  "17 panels and 8 kinds of chart, out of the same two stores the crew wrote during those runs.",
  "Which subject your creative keeps tripping over, and which jurisdictions are actually hard.",
], "09-intelligence.png", "The console draws the axes. Grafana charts the data.");

screen("Grafana resources", "/grafana", [
  "The whole Grafana surface on one page, read from the definitions the agent provisions from.",
  "9 dashboards and every panel, 9 metric series, 3 log streams, 3 alert rules.",
  "Every write operation, with the MCP tool it uses or the REST call it falls back to.",
], "grafana-res.png", "So it cannot drift into describing a stack nobody has.");

section("02", "part two", "Fixing what it blocked",
        "A finding that only says no is a report. The loop that closes is what makes this a product: edit the seconds that fail, then prove the edit worked.");

/* ------------------------------------------------------------ methods */
{
  const s = pres.addSlide(); light(s);
  head(s, "remediation", "Five ways to fix a shot, priced before you press");
  const methods = [
    ["omni", "Rewrite with Omni", "Gemini Omni rewrites the whole span as video to video", "0.10 EUR per second", "up to 10s", RED],
    ["bridge", "Regenerate with Veo", "both ends of the span edited, Veo 3.1 generates the motion between", "1.88 to 3.68 EUR", "genuine 3D motion", BLUE],
    ["overlay", "Patch one frame", "one Gemini image edit, held over the span", "0.04 EUR", "a locked-off shot", YEL],
    ["track", "Propagate the change", "the same edit, its lighting divided out and multiplied into every live frame", "0.04 EUR", "the thing holds still", GRN],
    ["per_frame", "Repaint every frame", "a repaint of every frame in the span", "0.04 EUR per frame", "a target that deforms", ORA],
  ];
  methods.forEach(([icon, name, what, price, when, colour], i) => {
    const y = 1.68 + i * 1.02;
    card(s, 0.6, y, 12.1, 0.9);
    s.addImage({ path: IMG + "methods/" + icon + ".png", x: 0.85, y: y + 0.19, w: 0.54, h: 0.54 });
    s.addText(name, { x: 1.62, y: y + 0.14, w: 2.9, h: 0.32, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 14, bold: true, color: INK });
    s.addText(when, { x: 1.62, y: y + 0.48, w: 2.9, h: 0.3, isTextBox: true, margin: 0,
      fontFace: MONO, fontSize: 10, color: colour });
    s.addText(what, { x: 4.75, y: y + 0.26, w: 5.6, h: 0.5, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 12.5, color: MUTED, lineSpacing: 16 });
    s.addText(price, { x: 10.5, y: y + 0.26, w: 1.95, h: 0.4, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 13, bold: true, color: INK, align: "right" });
  });
  s.addText("The planner chooses among the patch methods by the observation's own dimension, and it is a pure function with no model call. Veo is never chosen automatically: it regenerates pixels and costs real money, so it only ever runs because a person picked it.", {
    x: 0.6, y: 6.78, w: 12.1, h: 0.42, isTextBox: true, margin: 0, valign: "top",
    fontFace: BODY, fontSize: 11.5, color: MUTED,
  });
}

/* ------------------------------------------------------------ before after */
{
  const s = pres.addSlide(); light(s);
  head(s, "what a fix looks like", "The wine goes. Everything else stays.");
  shot(s, IMG + "ad-before.png", 0.6, 2.0, 5.95);
  shot(s, IMG + "ad-after.png", 6.78, 2.0, 5.95);
  s.addText("ORIGINAL, AS DELIVERED", { x: 0.6, y: 5.55, w: 5.95, h: 0.3, isTextBox: true,
    margin: 0, fontFace: MONO, fontSize: 11, bold: true, color: MUTED });
  s.addText("LOCALIZED FOR FRANCE", { x: 6.78, y: 5.55, w: 5.95, h: 0.3, isTextBox: true,
    margin: 0, fontFace: MONO, fontSize: 11, bold: true, color: GRN });
  s.addText("Gemini Omni rewrote the shot as video to video. Same terrace, same people, same motion, and the beer is now a soft drink. The verifier then re-watched the new footage with the same analyst that failed it, and only then did the finding close.", {
    x: 0.6, y: 5.95, w: 12.1, h: 0.8, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 14, color: INK, lineSpacing: 21,
  });
}

/* ------------------------------------------------------------ safety loop */
{
  const s = pres.addSlide(); light(s);
  head(s, "the safety loop", "Nothing is trusted because a model says so");
  const stages = [
    ["Grafana", "an alert rule crosses its threshold and posts to the webhook", ORA],
    ["Remediator", "plans, prices, edits the span, one edit per shot", BLUE],
    ["Craft gate", "length, resolution, audio and drift, or the file is discarded", YEL],
    ["Verifier", "re-runs the REAL analyst on the changed shots", GRN],
    ["Resolved", "the metric drops and Grafana clears its own alert", GRN],
  ];
  stages.forEach(([t, b, colour], i) => {
    const y = 1.85 + i * 0.96;
    s.addShape(pres.ShapeType.ellipse, { x: 0.75, y: y + 0.06, w: 0.44, h: 0.44,
      fill: { color: colour }, line: { width: 0 } });
    s.addText(String(i + 1), { x: 0.75, y: y + 0.13, w: 0.44, h: 0.3, isTextBox: true,
      margin: 0, fontFace: HEAD, fontSize: 13, bold: true, color: "FFFFFF", align: "center" });
    s.addText(t, { x: 1.4, y: y + 0.02, w: 2.3, h: 0.34, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 15, bold: true, color: INK });
    s.addText(b, { x: 1.4, y: y + 0.36, w: 5.4, h: 0.4, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 12.5, color: MUTED });
  });
  card(s, 7.3, 1.9, 5.4, 4.1, "FFFFFF");
  s.addText("Two things it refuses to do", { x: 7.6, y: 2.12, w: 4.8, h: 0.35, isTextBox: true,
    margin: 0, fontFace: HEAD, fontSize: 16, bold: true, color: INK });
  s.addText([
    { text: "It does not ask the model whether the edit worked.\n", options: { bold: true, breakLine: true } },
    { text: "The verifier re-runs the real analyst pass over the changed shots and asks the same instrument that found the problem whether it still sees it. Then it answers the second half: did anything new break?\n\n", options: { breakLine: true } },
    { text: "It does not keep an edit that damaged the film.\n", options: { bold: true, breakLine: true } },
    { text: "The craft gate measures length, resolution, soundtrack and collateral drift inside the span. A staged file that fails is discarded and the finding goes back to open, with the master untouched." },
  ], { x: 7.6, y: 2.55, w: 4.8, h: 3.6, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
       fontSize: 12, color: INK, lineSpacing: 17 });
  s.addText("An edit that removes a bottle can also remove the finding next to it, or introduce one.", {
    x: 0.6, y: 6.75, w: 12.1, h: 0.35, isTextBox: true, margin: 0, valign: "top", fontFace: MONO,
    fontSize: 11, color: MUTED,
  });
}

/* ------------------------------------------------------------ the guard */
{
  const s = pres.addSlide(); light(s);
  head(s, "the guard", "When the honest answer is not an edit");
  s.addText("When a rule is written on a protected characteristic, editing the ad is not the product working. Naming the problem and handing it to a person is.", {
    x: 0.6, y: 1.75, w: 7.4, h: 0.7, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 15, color: INK, lineSpacing: 22,
  });
  const facts = [
    ["Reads exactly two things", "the pack rule matched by rule id, and the finding's own class"],
    ["Never reads model output", "not the rationale, not the severity, not any generated field"],
    ["Never calls a model", "it is a pure function, so it cannot be talked around"],
    ["Enforced twice", "at adjudication, and again before a single frame is touched"],
  ];
  facts.forEach(([t, b], i) => {
    const y = 2.7 + i * 1.0;
    card(s, 0.6, y, 7.4, 0.86);
    s.addText(t, { x: 0.9, y: y + 0.13, w: 3.1, h: 0.3, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 13.5, bold: true, color: INK });
    s.addText(b, { x: 0.9, y: y + 0.46, w: 6.3, h: 0.32, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 12, color: MUTED });
  });
  card(s, 8.35, 1.78, 4.35, 4.35, "FFFFFF");
  s.addText("HUMAN DECISION REQUIRED", { x: 8.65, y: 2.05, w: 3.8, h: 0.3, isTextBox: true,
    margin: 0, fontFace: MONO, fontSize: 12, bold: true, color: RED });
  s.addText("\"rule basis targets a protected characteristic; human decision required\"", {
    x: 8.65, y: 2.45, w: 3.8, h: 0.9, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 13, italic: true, color: INK, lineSpacing: 19,
  });
  s.addText("The console shows the statute beside the refusal, and offers a person three ways out: approve a manual edit, send it to legal review, or accept the risk. Whichever they choose is recorded on the run and printed on the certificate.", {
    x: 8.65, y: 3.42, w: 3.8, h: 1.55, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 12, color: MUTED, lineSpacing: 17,
  });
  s.addText("Guardrails belong in rule layers, not prompts. Anything a prompt grants, a prompt can take away.", {
    x: 8.65, y: 5.05, w: 3.8, h: 0.95, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 12, bold: true, color: INK, lineSpacing: 17,
  });
}

/* ------------------------------------------------------------ the budget */
{
  const s = pres.addSlide(); light(s);
  head(s, "the money", "The loop that spends is the one that needs a brake");
  s.addText("Fixing a finding by generating video is the one path that can empty a day's allowance while nobody is watching. So the budget is not a config value, it is a metric with an alert on it.", {
    x: 0.6, y: 1.75, w: 12.1, h: 0.6, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 15, color: INK, lineSpacing: 22,
  });
  const cards = [
    ["Priced before it runs", "Every method shows its euro cost in the picker, and the agent quotes a fix before it starts one.", BLUE],
    ["Every charge is a series", "customs_spend_eur_total and customs_budget_remaining_eur go to Mimir on the real clock.", ORA],
    ["The third alert stops the loop", "customs_budget_low fires above the price of the most expensive single fix, and automatic remediation pauses for the rest of the day.", RED],
    ["A person can still spend", "Findings still block, alerts still arrive and are recorded with the reason, and an operator can spend what is left one fix at a time.", GRN],
  ];
  cards.forEach(([t, b, colour], i) => {
    const x = 0.6 + (i % 2) * 6.28, y = 2.7 + Math.floor(i / 2) * 2.0;
    card(s, x, y, 5.8, 1.75);
    s.addShape(pres.ShapeType.rect, { x: x + 0.3, y: y + 0.32, w: 0.42, h: 0.07,
      fill: { color: colour }, line: { width: 0 } });
    s.addText(t, { x: x + 0.3, y: y + 0.5, w: 5.2, h: 0.34, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 15, bold: true, color: INK });
    s.addText(b, { x: x + 0.3, y: y + 0.9, w: 5.2, h: 0.75, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 12.5, color: MUTED, lineSpacing: 17 });
  });
  s.addText("The pause is on the path nobody is watching, which is the only one that needed one.", {
    x: 0.6, y: 6.85, w: 12.1, h: 0.35, isTextBox: true, margin: 0, valign: "top", fontFace: MONO,
    fontSize: 11, color: MUTED,
  });
}

section("03", "part three", "Grafana is a participant, not a picture",
        "The crew writes into Grafana, Grafana triggers the crew, and a click on a Grafana panel starts a generative workflow. All three run on the live instance.");

/* ------------------------------------------------------- three directions */
{
  const s = pres.addSlide(); light(s);
  head(s, "three directions", "What it writes, what wakes it, what a click does");
  const dirs = [
    ["1", "The crew writes into Grafana", [
      "9 dashboards and 40 panels, built by the publisher agent during the run",
      "9 metric series to Mimir over OTLP, on the film's own clock",
      "3 log streams to Loki: observation, finding, verdict, with market and rule as labels",
      "One annotation per finding, and the alert rules that will wake the crew later",
    ], BLUE],
    ["2", "Grafana triggers the crew", [
      "Two rules evaluate every 30 seconds against customs_blocking",
      "Crossing 70 for an asset, market and rule posts to the customs-webhook contact point",
      "That contact point is a route on this service, and it wakes the remediator",
      "An alert in Grafana is what starts a Veo render. Not a cron, not a queue",
    ], ORA],
    ["3", "A click in Grafana generates", [
      "The timeline grid is one visual drawn by two systems",
      "The squares are Grafana's own status history panel, live",
      "A data link fires with the click's coordinate, and the console resolves the finding under it",
      "The fix starts because somebody clicked a chart",
    ], GRN],
  ];
  dirs.forEach(([n, title, points, colour], i) => {
    const x = 0.6 + i * 4.15;
    card(s, x, 1.95, 3.85, 4.55);
    s.addShape(pres.ShapeType.ellipse, { x: x + 0.3, y: 2.2, w: 0.5, h: 0.5,
      fill: { color: colour }, line: { width: 0 } });
    s.addText(n, { x: x + 0.3, y: 2.28, w: 0.5, h: 0.34, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 15, bold: true, color: "FFFFFF", align: "center" });
    s.addText(title, { x: x + 0.3, y: 2.83, w: 3.25, h: 0.7, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 15.5, bold: true, color: INK, lineSpacing: 20 });
    s.addText(points.map((t, k) => ({ text: t, options: { bullet: true, breakLine: k < points.length - 1 } })), {
      x: x + 0.3, y: 3.6, w: 3.25, h: 2.75, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
      fontSize: 11.5, color: MUTED, lineSpacing: 15, paraSpaceAfter: 6,
    });
  });
  s.addText("The MCP server is the official grafana/mcp-grafana v1.1.0, run as a stdio subprocess with a service account token. Seven of its tools are called at runtime: create_folder, update_dashboard, alerting_manage_rules, get_panel_image, query_loki_logs, search_dashboards, get_dashboard_by_uid.", {
    x: 0.6, y: 6.58, w: 12.1, h: 0.55, isTextBox: true, margin: 0, valign: "top",
    fontFace: BODY, fontSize: 11.5, color: MUTED, lineSpacing: 16,
  });
}

/* ------------------------------------------------------- what is in the store */
{
  const s = pres.addSlide(); light(s);
  head(s, "not a claim, four renders", "What is actually in Loki and Mimir");
  shot(s, IMG + "store-loki-observations.png", 0.62, 1.9, 5.9, 2.55);
  shot(s, IMG + "store-loki-findings.png", 6.82, 1.9, 5.9, 2.55);
  s.addText("kind=observation, one line per keyframe. Labels carry app, asset, dimension, flagged and kind; the body carries the analyst's own sentence and which markets objected.", {
    x: 0.62, y: 4.72, w: 5.9, h: 0.9, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 12, color: MUTED, lineSpacing: 16,
  });
  s.addText("kind=finding, the join with the statute in it. market and rule_id are labels, so a market's findings are a selector rather than a scan, and the citation was resolved at judging time.", {
    x: 6.82, y: 4.72, w: 5.9, h: 0.9, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 12, color: MUTED, lineSpacing: 16,
  });
  s.addText("Prometheus rejects backdated samples, so each run is mapped onto the wall clock at t0: video second n lands at t0 plus n. The panel range is pinned to the run, so the x axis reads as the timecode because it is the timecode.", {
    x: 0.62, y: 5.95, w: 12.1, h: 0.8, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 13, color: INK, lineSpacing: 19,
  });
}

/* ------------------------------------------------------------- outputs */
{
  const s = pres.addSlide(); light(s);
  head(s, "what you get out", "The things a clearance desk actually hands over");
  const outs = [
    ["A decision per market", "Cleared, at risk or blocked, with every finding, its statute and a citation that resolves. The evidence, not a summary.", BLUE],
    ["A localized master", "One edited file per market, only the failing seconds touched, checked by the craft gate and signed off by the verifier.", GRN],
    ["A clearance certificate", "A PDF per market: every finding worst first, the rule, the seconds, the statute, the fix and the verifier's own sentence.", RED],
    ["Timeline markers", "CSV and CMX3600 EDL, so an editor sees the findings on their own Resolve or Premiere timeline, coloured by what they mean.", YEL],
  ];
  outs.forEach(([t, b, colour], i) => {
    const x = 0.6 + (i % 2) * 6.28, y = 1.9 + Math.floor(i / 2) * 2.35;
    card(s, x, y, 5.8, 2.05);
    s.addShape(pres.ShapeType.rect, { x: x + 0.32, y: y + 0.34, w: 0.44, h: 0.07,
      fill: { color: colour }, line: { width: 0 } });
    s.addText(t, { x: x + 0.32, y: y + 0.52, w: 5.15, h: 0.36, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 17, bold: true, color: INK });
    s.addText(b, { x: x + 0.32, y: y + 0.95, w: 5.15, h: 0.95, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 12.5, color: MUTED, lineSpacing: 18 });
  });
  s.addText("Colour on the markers is what the finding does rather than how it feels: red where a legal finding is at or over the blocking line, green where the verifier signed a fix off, yellow for the rest, and HUMAN DECISION REQUIRED in the note where the Guard refused.", {
    x: 0.6, y: 6.5, w: 12.1, h: 0.6, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 11.5, color: MUTED, lineSpacing: 16,
  });
}

/* ------------------------------------------------------------- architecture */
{
  const s = pres.addSlide(); light(s);
  head(s, "under it", "One ADK crew, one console, two stores");
  const rows = [
    ["crew.py", "the ADK SequentialAgent: ingest, analyst, adjudicators, guard, publisher"],
    ["analyst.py", "one Gemini vision call per shot, 18 dimension taxonomy, no verdicts"],
    ["adjudicate.py", "the join: observation x rule x grounded citation, severity decided in code"],
    ["guard.py", "un-promptable refusal, reads rule metadata only"],
    ["remediate.py", "five methods, priced, group aware, guarded twice"],
    ["verify.py", "re-runs the real analyst on the changed shots, rules on bystanders"],
    ["media.py", "ffmpeg: shots, flashes, spans, craft gate, thumbnails, previews"],
    ["grafana_ops.py", "MCP first, REST where mcp-grafana 1.1.0 has no write tool"],
    ["telemetry.py", "Mimir over OTLP on the film's own clock, Loki lines, annotations"],
    ["app.py", "FastAPI console: server-sent events, 21 templates, no build step"],
  ];
  rows.forEach(([f, b], i) => {
    const y = 1.8 + i * 0.44;
    s.addText(f, { x: 0.6, y, w: 2.1, h: 0.34, isTextBox: true, margin: 0,
      fontFace: MONO, fontSize: 12, bold: true, color: BLUE });
    s.addText(b, { x: 2.8, y, w: 5.9, h: 0.34, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 12, color: INK });
  });
  card(s, 9.0, 1.8, 3.7, 4.45);
  s.addText("THE NUMBERS", { x: 9.3, y: 2.05, w: 3.1, h: 0.3, isTextBox: true, margin: 0,
    fontFace: MONO, fontSize: 11, bold: true, color: MUTED });
  const nums = [["21", "market packs"], ["98", "jurisdictions"], ["128", "rules"],
                ["18", "dimensions"], ["9", "dashboards, 40 panels"], ["600+", "offline tests"]];
  nums.forEach(([n, l], i) => {
    const y = 2.45 + i * 0.63;
    s.addText(n, { x: 9.3, y, w: 1.0, h: 0.4, isTextBox: true, margin: 0, valign: "top", fontFace: HEAD,
      fontSize: 19, bold: true, color: INK, align: "right" });
    s.addText(l, { x: 10.45, y: y + 0.07, w: 2.0, h: 0.3, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 12, color: MUTED });
  });
  s.addText("Python  ·  FastAPI  ·  Google Cloud Agent Builder (ADK)  ·  Gemini vision, text, TTS and image  ·  Gemini Omni  ·  Veo 3.1  ·  Vertex AI  ·  Grafana Cloud with Mimir, Loki and MCP  ·  ffmpeg  ·  SQLite  ·  Cloud Run", {
    x: 0.6, y: 6.55, w: 12.1, h: 0.6, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 12, color: MUTED, lineSpacing: 17,
  });
}

/* ------------------------------------------------------------- honest limits */
{
  const s = pres.addSlide(); light(s);
  head(s, "honest limits", "Known, deliberate, and worth saying out loud");
  const limits = [
    ["The channel rung is thin", "Three Belgian broadcasters have pack files of their own. The other 77 inherit their country and add nothing yet."],
    ["The corpus is deliberately small", "The archive shows the commercials this project generated with Veo, because they are the ones it holds the rights to."],
    ["Omni refuses third-party footage", "Gemini Omni declines to edit recognisable third-party content. The refusal is quoted verbatim and nothing is charged."],
    ["Veo has a celebrity filter", "A bridge over a shot it reads as a public figure is refused. Never charged, and a retry cannot help: the footage is the refusal."],
    ["Grafana Cloud cannot be framed", "Fourteen URL forms were probed and every one enforces frame-ancestors none, so live panels come from a self-hosted viewer over the same data."],
    ["One Cloud Run instance", "SQLite plus one writer thread is the concurrency model. Two concurrent multi-market clearances is too much for one instance."],
  ];
  limits.forEach(([t, b], i) => {
    const x = 0.6 + (i % 2) * 6.28, y = 1.82 + Math.floor(i / 2) * 1.66;
    card(s, x, y, 5.8, 1.4);
    s.addText(t, { x: x + 0.3, y: y + 0.22, w: 5.2, h: 0.32, isTextBox: true, margin: 0,
      fontFace: HEAD, fontSize: 14, bold: true, color: INK });
    s.addText(b, { x: x + 0.3, y: y + 0.6, w: 5.2, h: 0.7, isTextBox: true, margin: 0,
      fontFace: BODY, fontSize: 11.5, color: MUTED, lineSpacing: 16 });
  });
  s.addText("A hackathon build says what it cannot do. Everything above is in the README, in these words.", {
    x: 0.6, y: 6.82, w: 12.1, h: 0.35, isTextBox: true, margin: 0, valign: "top",
    fontFace: MONO, fontSize: 11, color: MUTED,
  });
}

/* ------------------------------------------------------------------- close */
{
  const s = pres.addSlide(); dark(s);
  s.addImage({ path: IMG + "logo.png", x: 5.72, y: 1.5, w: 1.9, h: 0.96 });
  s.addText("T H E   M E D I A   C U S T O M S", {
    x: 0.6, y: 2.7, w: 12.1, h: 0.8, isTextBox: true, margin: 0, valign: "top", fontFace: HEAD,
    fontSize: 38, bold: true, color: "FFFFFF", align: "center",
  });
  s.addText("One asset, every market, before it ships.", {
    x: 0.6, y: 3.5, w: 12.1, h: 0.4, isTextBox: true, margin: 0, valign: "top", fontFace: BODY,
    fontSize: 16, color: "B6C6CD", align: "center",
  });
  [BLUE, RED, YEL, GRN].forEach((c, i) => s.addShape(pres.ShapeType.rect, {
    x: 5.35 + i * 0.68, y: 4.15, w: 0.56, h: 0.07, fill: { color: c }, line: { width: 0 },
  }));
  s.addText([
    { text: "Live, and nothing is gated for reading", options: { bold: true, color: "FFFFFF", align: "center", breakLine: true } },
    { text: "customs-app-akap4ao72a-ew.a.run.app", options: { color: "CFE0E6", align: "center", breakLine: true } },
    { text: "Code", options: { bold: true, color: "FFFFFF", align: "center", breakLine: true } },
    { text: "github.com/tdries/devpost-hackathon-agentic-cinema", options: { color: "CFE0E6", align: "center" } },
  ], { x: 0.6, y: 4.78, w: 12.1, h: 1.6, isTextBox: true, margin: 0, valign: "top",
       fontFace: MONO, fontSize: 12.5, align: "center", lineSpacing: 22 });
  s.addText("Built on Google Cloud and Grafana  ·  Tim Dries", {
    x: 0.6, y: 6.85, w: 12.1, h: 0.3, isTextBox: true, margin: 0, valign: "top",
    fontFace: MONO, fontSize: 11, color: "9FB6BE", align: "center",
  });
}

pres.writeFile({ fileName: "docs/The-Media-Customs-explained.pptx" })
  .then(f => console.log("wrote", f));
