/* Customs Launch Control.

   Four behaviours, no framework, no build step:
     1. the upload form remembers which file you picked (and accepts a drop)
     2. the launch board polls /status and flips the tiles in place
     3. the mission feed tails the SSE stream
     4. the cutting room plays an original and its localized master in lockstep

   Everything degrades: with JavaScript off the board still renders the state
   it had when the page was served, the feed still shows its backlog, and the
   videos are still two ordinary players. */

(function () {
  "use strict";

  var STATE_WORDS = { error: "not evaluated", at_risk: "at risk",
                      noted: "cleared, notes" };
  var word = function (state) { return STATE_WORDS[state] || state; };

  /* The icon system (see docs/design/icons): every state and agent has one
     mark, drawn once as <symbol>s in base.html and referenced by id here. */
  var STATE_ICONS = { cleared: 1, at_risk: 1, blocked: 1, pending: 1, error: 1, noted: 1 };
  var icon = function (state) {
    var id = STATE_ICONS[state] ? state.replace("_", "-") : "pending";
    return '<svg class="ic"><use href="#i-' + id + '"/></svg>';
  };
  var AGENT_ICONS = { pipeline: 1, ingest: 1, transcription: 1, analyst: 1,
                      adjudicator: 1, guard: 1, publisher: 1, remediator: 1,
                      verifier: 1 };

  /* ---------- 1. the upload form ---------- */

  var drop = document.getElementById("drop");
  var asset = document.getElementById("asset");
  var picked = document.getElementById("picked");

  if (drop && asset && picked) {
    var show = function () {
      var file = asset.files && asset.files[0];
      if (!file) { return; }
      var mb = (file.size / (1024 * 1024)).toFixed(1);
      picked.textContent = file.name + "  //  " + mb + " MB";
      picked.style.color = "var(--signal)";
    };
    asset.addEventListener("change", show);
    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        event.preventDefault();
        drop.style.borderColor = "var(--signal)";
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      drop.addEventListener(name, function () { drop.style.borderColor = ""; });
    });
    drop.addEventListener("drop", function (event) {
      event.preventDefault();
      if (event.dataTransfer && event.dataTransfer.files.length) {
        asset.files = event.dataTransfer.files;
        show();
      }
    });
  }

  /* An agent's message is data, not markup: it carries model-written text
     and file paths, so it is escaped before it reaches innerHTML. */
  function escapeHtml(text) {
    var d = document.createElement("div");
    d.textContent = text == null ? "" : String(text);
    return d.innerHTML;
  }

  /* ---------- 2. the launch board ---------- */

  var board = document.getElementById("board");
  if (board) {
    var runId = board.getAttribute("data-run");
    var headline = document.getElementById("headline");
    var subline = document.getElementById("subline");
    var flag = document.getElementById("flag");
    var strip = document.getElementById("strip");

    var paint = function (data) {
      Object.keys(data.markets).forEach(function (code) {
        var market = data.markets[code];
        var state = market.display || (market.errored ? "error" : market.clearance);
        var tile = document.getElementById("tile-" + code);
        if (tile) {
          tile.className = "tile t-" + state;
          var pill = tile.querySelector('[data-role="state"]');
          if (pill) {
            pill.className = "pill s-" + state;
            pill.innerHTML = icon(state) + word(state);
          }
          var findings = tile.querySelector('[data-role="findings"]');
          var blocked = tile.querySelector('[data-role="blocked"]');
          if (findings) { findings.textContent = market.findings; }
          if (blocked) { blocked.textContent = market.blocked; }
        }
        if (strip) {
          var segment = strip.querySelector('[data-market="' + code + '"]');
          if (segment) {
            segment.className = "v-" + state;
            segment.setAttribute("title", code + ": " + word(state));
          }
        }
      });

      /* the run's own progress, from what the agents have reported */
      var progress = document.getElementById("progress");
      if (progress && data.progress) {
        var fill = document.getElementById("progress-fill");
        var pct = document.getElementById("progress-pct");
        var stage = document.getElementById("progress-stage");
        if (fill) { fill.style.width = data.progress.pct + "%"; }
        if (pct) { pct.textContent = data.progress.pct + "%"; }
        if (stage) { stage.textContent = data.progress.stage; }

        /* The newest thing an agent said, under the bar. The bar can sit on
           one percentage for a minute while the analyst reads a shot, so
           this is what tells the operator the run is alive. Re-triggering
           the animation needs the reflow: without it the class is removed
           and re-added inside one frame and nothing plays. */
        var tick = document.getElementById("progress-tick");
        if (tick && data.ticker && String(data.ticker.id) !== tick.dataset.eventId) {
          tick.dataset.eventId = String(data.ticker.id);
          tick.innerHTML = '<span class="a-' + data.ticker.agent + '">' +
            data.ticker.agent + "</span> " + escapeHtml(data.ticker.message);
          tick.classList.remove("flash");
          void tick.offsetWidth;
          tick.classList.add("flash");
        }
        if (data.done) { progress.remove(); }
      }

      var overall = data.overall;
      if (headline) {
        headline.textContent = "CLEARED FOR LAUNCH IN " + overall.cleared +
          " OF " + overall.total + " MARKETS";
        headline.className = "headline" + (overall.state === "go" ? " go" : "");
      }
      if (flag) {
        var flags = { go: "GO FOR LAUNCH", no_go: "NO GO", pending: "CLEARANCE IN PROGRESS" };
        var states = { go: "cleared", no_go: "blocked", pending: "pending" };
        flag.className = "pill s-" + states[overall.state];
        flag.innerHTML = icon(states[overall.state]) + flags[overall.state];
      }
      if (subline) {
        if (overall.state === "pending") {
          subline.textContent = "The adjudicators are still returning. Tiles flip as each market lands.";
        } else if (overall.failing.length) {
          subline.innerHTML = "<strong>" + overall.failing.join(", ") + "</strong> " +
            (overall.failing.length === 1 ? "is" : "are") +
            " holding the campaign. Open a market room for the statute behind every finding.";
        } else {
          subline.textContent = "Every market cleared. The findings, the citations and the edits are all one click down.";
        }
      }
    };

    var poll = function () {
      fetch("/runs/" + runId + "/status", { headers: { Accept: "application/json" } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data) { return; }
          paint(data);
          /* A finished run still moves: a Grafana alert can wake the
             Remediator an hour later and a resolved finding changes the
             market's clearance. So the poll slows down, it never stops. */
          window.setTimeout(poll, data.done ? 10000 : 2000);
        })
        .catch(function () { window.setTimeout(poll, 5000); });
    };
    window.setTimeout(poll, 2000);
  }

  /* ---------- 3. the mission feed ---------- */

  var feed = document.getElementById("feed");
  if (feed && window.EventSource) {
    var run = feed.getAttribute("data-run");
    var last = feed.getAttribute("data-last") || "0";
    var follow = document.getElementById("follow");
    var counter = document.getElementById("count");
    var live = document.getElementById("live");
    var liveText = document.getElementById("live-text");
    var seen = feed.querySelectorAll(".line").length;

    var mark = function (text, ok) {
      if (liveText) { liveText.textContent = text; }
      if (live) { live.className = "pill " + (ok ? "s-cleared" : "s-pending"); }
      if (live) { live.innerHTML = '<span class="dot"></span>' + text; }
    };

    var stream = new EventSource("/runs/" + run + "/feed?after=" + last);
    stream.addEventListener("open", function () { mark("live", true); });
    stream.addEventListener("error", function () { mark("reconnecting", false); });
    var AGENTS = { pipeline: 1, ingest: 1, transcription: 1, analyst: 1,
                   adjudicator: 1, guard: 1, publisher: 1, remediator: 1,
                   verifier: 1 };

    /* One row per stage: consecutive events from the same agent land in the
       row that is already open, and a different agent closes it and starts
       the next. The bar animates on whichever row is still receiving. */
    var group = function (agent, clock) {
      Array.prototype.forEach.call(feed.querySelectorAll(".grp.live"), function (g) {
        g.classList.remove("live");
      });
      var el = document.createElement("details");
      el.className = "grp live";
      el.setAttribute("data-agent", agent);
      var markId = AGENTS[agent] ? agent : "pipeline";
      el.innerHTML =
        '<summary><svg class="ic"><use href="#i-' + markId + '"/></svg>' +
        '<span class="ga a-' + agent + '"></span>' +
        '<span class="gm"></span><span class="gc mono">0 events</span>' +
        '<span class="gt mono"></span></summary>' +
        '<div class="glines"></div><span class="gbar"><i></i></span>';
      el.querySelector(".ga").textContent = agent;
      el.querySelector(".gt").textContent = clock;
      feed.appendChild(el);
      return el;
    };

    stream.addEventListener("mission", function (message) {
      var event = JSON.parse(message.data);
      var empty = feed.querySelector(".empty");
      if (empty) { empty.remove(); }

      var last = feed.lastElementChild;
      if (!last || !last.classList || !last.classList.contains("grp") ||
          last.getAttribute("data-agent") !== event.agent) {
        last = group(event.agent, event.clock);
      }
      last.classList.add("live");

      var line = document.createElement("div");
      line.className = "line";
      var failed = event.message.indexOf("stage_error") >= 0;
      if (failed) { line.classList.add("err"); last.classList.add("err"); }
      var when = document.createElement("span");
      when.className = "t";
      when.textContent = event.clock;
      var what = document.createElement("span");
      what.className = "m";
      what.textContent = event.message;
      line.appendChild(when);
      line.appendChild(what);
      last.querySelector(".glines").appendChild(line);

      var count = last.querySelector(".glines").children.length;
      last.querySelector(".gc").textContent = count + (count === 1 ? " event" : " events");
      last.querySelector(".gm").textContent = event.message;

      seen += 1;
      if (counter) { counter.textContent = seen + " events"; }
      if (!follow || follow.checked) { feed.scrollTop = feed.scrollHeight; }
    });
    if (!follow || follow.checked) { feed.scrollTop = feed.scrollHeight; }
  }

  /* A panel render can fail (Grafana down, no service account token on this
     machine). Say so in place of a broken image, and keep the link out. */
  Array.prototype.forEach.call(document.querySelectorAll(".embed img"), function (panel) {
    panel.addEventListener("error", function () {
      var box = panel.closest(".embed");
      if (!box) { return; }
      var note = document.createElement("div");
      note.className = "empty";
      note.style.border = "none";
      note.innerHTML = '<span class="big">Panel could not be rendered</span>' +
        "<p>Grafana did not answer the render request. The dashboard itself is still live: use the link below.</p>";
      panel.parentNode.replaceChild(note, panel);
    });
  });

  /* ---------- 4. the cutting room ---------- */

  Array.prototype.forEach.call(document.querySelectorAll(".pair"), function (pair) {
    var players = pair.querySelectorAll("video");
    if (players.length !== 2) { return; }
    var section = pair.closest("section");
    var toggle = section ? section.querySelector(".sync") : null;
    var mirroring = false;

    var linked = function (source, other) {
      var on = function () { return !toggle || toggle.checked; };
      source.addEventListener("play", function () {
        if (!on() || mirroring) { return; }
        mirroring = true;
        other.currentTime = source.currentTime;
        var started = other.play();
        if (started && started.catch) { started.catch(function () {}); }
        mirroring = false;
      });
      source.addEventListener("pause", function () {
        if (!on() || mirroring) { return; }
        mirroring = true;
        other.pause();
        mirroring = false;
      });
      source.addEventListener("seeked", function () {
        if (!on() || mirroring) { return; }
        mirroring = true;
        other.currentTime = source.currentTime;
        mirroring = false;
      });
    };
    linked(players[0], players[1]);
    linked(players[1], players[0]);
  });
})();

  /* ---------- 5. the style-mode switch ---------- */

  (function () {
    var KEY = "customs-theme";
    var root = document.documentElement;
    var buttons = document.querySelectorAll(".theme-switch [data-set-theme]");
    if (!buttons.length) { return; }

    var mark = function () {
      var current = root.getAttribute("data-theme") || "";
      buttons.forEach(function (b) {
        b.classList.toggle("on", b.getAttribute("data-set-theme") === current);
      });
    };

    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        var mode = b.getAttribute("data-set-theme");
        if (mode) { root.setAttribute("data-theme", mode); }
        else { root.removeAttribute("data-theme"); }
        try {
          /* Mission is stored as "mission", not as a missing key. On a phone
             the absence of a key means "default to studio", so removing it
             here would throw the choice away on the next page load. */
          localStorage.setItem(KEY, mode || "mission");
        } catch (e) { /* private mode: the choice just does not persist */ }
        mark();
      });
    });

    mark();
  })();

  /* ---------- 6. the findings view switch ----------
     Detail (the default) is the full row: rationale, citation, evidence
     frame. List is the scannable one: same rows, same thumbnails, minus the
     prose. The choice is per browser, like the style mode. */

  (function () {
    var KEY = "customs-findings-view";
    var panel = document.getElementById("findings");
    var buttons = document.querySelectorAll(".view-switch [data-set-view]");
    if (!panel || !buttons.length) { return; }

    /* List is the default: a market with fifteen findings should open as
       fifteen rows, not five screens. "detail" is the stored opt-out. */
    var apply = function (view) {
      panel.classList.toggle("as-list", view !== "detail");
      buttons.forEach(function (b) {
        b.classList.toggle("on", b.getAttribute("data-set-view") === view);
      });
    };

    var saved = "";
    try { saved = localStorage.getItem(KEY) || ""; } catch (e) { /* private mode */ }
    apply(saved);

    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        var view = b.getAttribute("data-set-view");
        try {
          if (view) { localStorage.setItem(KEY, view); }
          else { localStorage.removeItem(KEY); }
        } catch (e) { /* private mode: the choice just does not persist */ }
        apply(view);
      });
    });
  })();


  /* ---------- 7. a market room with work in flight ----------
     Remediation takes a minute of model calls and ffmpeg. The room is
     server-rendered, so without this the operator watches a row that says
     "working" and never sees it stop. Polls only while something is
     actually running, and stops the moment nothing is. */

  (function () {
    var room = document.querySelector('[data-room]');
    if (!room) { return; }
    var busy = function () { return room.querySelectorAll('[data-busy]').length > 0; };
    if (!busy()) { return; }
    var tick = function () {
      fetch(window.location.pathname, { headers: { "X-Poll": "1" } })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (html) {
          if (!html) { return window.setTimeout(tick, 8000); }
          if (html.indexOf("data-busy") === -1) { window.location.reload(); return; }
          window.setTimeout(tick, 8000);
        })
        .catch(function () { window.setTimeout(tick, 8000); });
    };
    window.setTimeout(tick, 8000);
  })();

  /* ---------- 8. the archive's list or cards ---------- */

  (function () {
    var KEY = "customs-runs-view";
    var list = document.getElementById("runlist");
    var buttons = document.querySelectorAll(".view-switch [data-set-runs]");
    if (!list || !buttons.length) { return; }

    var apply = function (view) {
      list.classList.toggle("as-rows", view !== "cards");
      buttons.forEach(function (b) {
        b.classList.toggle("on", b.getAttribute("data-set-runs") === view);
      });
    };
    var saved = "";
    try { saved = localStorage.getItem(KEY) || ""; } catch (e) { /* private mode */ }
    apply(saved);

    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        var view = b.getAttribute("data-set-runs");
        try {
          if (view) { localStorage.setItem(KEY, view); }
          else { localStorage.removeItem(KEY); }
        } catch (e) { /* private mode */ }
        apply(view);
      });
    });
  })();

  /* ---------- 9. agent mode ----------
     The operator types on the left, the agent answers and opens whatever it
     opened on the right. One turn per submit; the session id keeps the
     conversation together across turns without a login. */

  (function () {
    var form = document.getElementById("agent-ask");
    if (!form) { return; }
    var log = document.getElementById("agent-log");
    var input = document.getElementById("agent-input");
    var canvas = document.getElementById("agent-canvas");
    var viewLabel = document.getElementById("view-label");
    var viewOpen = document.getElementById("view-open");
    var runId = document.querySelector(".agent").getAttribute("data-run") || "";
    var session = "s" + Math.random().toString(36).slice(2, 10);
    var busy = false;

    var bubble = function (who, text, icon) {
      var el = document.createElement("div");
      el.className = "ag-msg ag-" + who;
      el.innerHTML = '<svg class="ic"><use href="#i-' + icon + '"/></svg>' +
                     '<div class="ag-body"></div>';
      var body = el.querySelector(".ag-body");
      String(text).split("\n").forEach(function (line) {
        if (!line.trim()) { return; }
        var p = document.createElement("p");
        p.textContent = line;
        body.appendChild(p);
      });
      log.appendChild(el);
      log.scrollTop = log.scrollHeight;
      return el;
    };

    var open = function (url, label, external) {
      if (!url) { return; }
      canvas.innerHTML = "";
      if (url.indexOf(".png") > 0) {
        /* Grafana refuses to be framed, so a dashboard arrives as a
           server-side render. An image, not an iframe. */
        var shot = document.createElement("img");
        shot.src = url;
        shot.alt = label || "dashboard";
        shot.className = "agent-shot";
        canvas.appendChild(shot);
      } else {
        var frame = document.createElement("iframe");
        frame.src = url;
        frame.setAttribute("title", label || "view");
        canvas.appendChild(frame);
      }
      if (viewLabel) {
        viewLabel.innerHTML = '<svg class="ic"><use href="#n-board"/></svg>' +
                              (label || "view");
      }
      if (viewOpen) { viewOpen.href = external || url; viewOpen.style.display = ""; }
    };

    var ask = function (text) {
      if (busy || !text.trim()) { return; }
      busy = true;
      bubble("you", text, "human");
      input.value = "";
      var thinking = bubble("agent", "", "pending");
      thinking.querySelector(".ag-body").innerHTML =
        '<span class="ag-think"><span class="spin"></span>working</span>';

      var body = new FormData();
      body.append("message", text);
      body.append("session", session);
      body.append("run", runId);

      fetch("/agent/ask", { method: "POST", body: body })
        .then(function (r) { return r.json().catch(function () { return null; }); })
        .then(function (data) {
          thinking.remove();
          if (!data) { bubble("agent", "That turn failed to come back.", "error"); return; }
          if (data.error) {
            var failed = bubble("agent", data.error, "error");
            failed.querySelector(".ag-body").classList.add("ag-err");
          }
          if (data.reply) { bubble("agent", data.reply, "adjudicator"); }
          if (data.calls && data.calls.length) {
            var last = log.lastElementChild.querySelector(".ag-body") || log.lastElementChild;
            var calls = document.createElement("div");
            calls.className = "ag-calls";
            data.calls.forEach(function (c) {
              var chip = document.createElement("span");
              chip.className = "ag-call";
              chip.textContent = c.tool;
              calls.appendChild(chip);
            });
            last.appendChild(calls);
          }
          open(data.view, data.view_label, data.view_external);
          log.scrollTop = log.scrollHeight;
        })
        .catch(function () {
          thinking.remove();
          bubble("agent", "That turn could not be sent.", "error");
        })
        .then(function () { busy = false; });
    };

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      ask(input.value);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".sugg"), function (b) {
      b.addEventListener("click", function () { ask(b.getAttribute("data-say")); });
    });
  })();
