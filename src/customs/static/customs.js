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

  /* The launcher used to let the natural first-time path through to a bare
     plain-text 400 (no market ticked) or Google's raw 413 page (Cloud Run
     kills bodies over 32 MiB before the app sees a byte, whatever the app's
     own limit says). Both are caught here instead, inline, with the form
     state intact. */
  var launcher = document.querySelector("form.launcher");
  if (launcher && asset) {
    var warn = document.getElementById("launchwarn");
    launcher.addEventListener("submit", function (event) {
      var file = asset.files && asset.files[0];
      var url = ((launcher.youtube_url && launcher.youtube_url.value) || "").trim();
      var markets = launcher.querySelectorAll('input[name="markets"]:checked').length;
      var wrong =
        !markets ? "Tick at least one market to clear for." :
        (!file && !url) ? "Hand over a master or paste a YouTube link first." :
        (file && file.size > 30 * 1024 * 1024)
          ? "That file is " + (file.size / (1024 * 1024)).toFixed(0) +
            " MB and this door closes at 30 MB. Paste the ad as a YouTube " +
            "link instead — the server fetches that itself."
          : "";
      if (wrong) {
        event.preventDefault();
        if (warn) {
          warn.textContent = wrong;
          warn.style.color = "var(--blocked)";
          warn.hidden = false;
        }
        return;
      }
      if (warn) { warn.hidden = true; }
      /* One press only, and say the upload is happening: a 30 MB master on
         hotel wifi is otherwise a long, frozen silence. Disable on the next
         tick so the button's value still submits. */
      var button = launcher.querySelector("button[type=submit]");
      if (button) {
        window.setTimeout(function () {
          button.disabled = true;
          button.textContent = "Uploading the master…";
        }, 0);
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

  /* ---------- 1b. the mission feed's two tabs ---------- */

  (function () {
    var buttons = document.querySelectorAll("[data-mtab]");
    if (!buttons.length) { return; }
    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        var want = b.getAttribute("data-mtab");
        buttons.forEach(function (other) {
          var name = other.getAttribute("data-mtab");
          other.classList.toggle("on", name === want);
          var panel = document.getElementById("mtab-" + name);
          if (panel) { panel.hidden = name !== want; }
        });
      });
    });
  })();

  /* ---------- 1c. "show what was spotted" ----------
     A green rectangle over the evidence frame, per thumbnail, off by
     default. The box is fetched once and cached on the element: an
     observation made before the analyst was asked for one has to be
     located by a model, which costs a call, so it happens on the first
     tick and never again. Nothing is ever drawn into the image itself --
     that PNG is what a remediation edits and what Veo is anchored on. */

  document.querySelectorAll(".box-toggle").forEach(function (input) {
    var wrap = input.closest(".fb-frame");
    if (!wrap) { return; }
    var shot = wrap.querySelector(".fb-shot");
    var svg = wrap.querySelector(".fb-box");
    var note = input.parentElement.querySelector(".box-note");
    if (!shot || !svg) { return; }

    var draw = function (box) {
      if (!box || box.length !== 4) {
        svg.hidden = true;
        if (note) { note.textContent = "nothing to box in this frame"; }
        return;
      }
      var y0 = box[0], x0 = box[1], y1 = box[2], x1 = box[3];
      svg.innerHTML = '<rect x="' + x0 + '" y="' + y0 + '" width="' +
        (x1 - x0) + '" height="' + (y1 - y0) + '" rx="8"></rect>';
      svg.hidden = false;
      if (note) { note.textContent = ""; }
    };

    input.addEventListener("change", function () {
      if (!input.checked) { svg.hidden = true; return; }
      if (shot.dataset.box) {
        draw(JSON.parse(shot.dataset.box));
        return;
      }
      if (note) { note.textContent = "locating..."; }
      fetch("/runs/" + shot.dataset.run + "/evidence/" +
            shot.dataset.boxFor + "/box")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          shot.dataset.box = JSON.stringify(d.box || []);
          if (input.checked) { draw(d.box); }
        })
        .catch(function () {
          if (note) { note.textContent = "could not locate it"; }
        });
    });
  });

  /* ---------- 1d. the problem lanes ----------
     Hovering a dot shows the frame the analyst actually read. That is the
     whole reason this chart is inline SVG and not an image: an <img>
     cannot tell you which observation you are pointing at. */

  (function () {
    /* One board has one chart; the archive has one per card. Bind them
       all, or only the first run in the list would answer a hover. */
    document.querySelectorAll(".lanes-wrap").forEach(bindLanes);
  })();

  function bindLanes(wrap) {
    if (!wrap) { return; }
    var peek = wrap.querySelector(".lane-peek");
    var img = peek.querySelector("img");
    var when = peek.querySelector(".when");
    var what = peek.querySelector(".what");
    var run = wrap.getAttribute("data-run");

    wrap.addEventListener("mouseover", function (e) {
      var dot = e.target.closest(".lane-dot");
      if (!dot) { return; }
      var obs = dot.getAttribute("data-obs");
      if (!obs) { return; }
      img.src = "/runs/" + run + "/evidence/" + obs;
      when.textContent = dot.getAttribute("data-t") + "s";
      var markets = dot.getAttribute("data-markets");
      what.textContent = (dot.getAttribute("data-dim") || "").replace(/_/g, " ")
        + (markets ? " — " + markets : "");
      var r = dot.getBoundingClientRect(), w = wrap.getBoundingClientRect();
      peek.style.left = (r.left - w.left + wrap.scrollLeft + r.width / 2) + "px";
      peek.style.top = (r.top - w.top - 10) + "px";
      peek.hidden = false;
    });
    wrap.addEventListener("mouseout", function (e) {
      if (e.target.closest(".lane-dot")) { peek.hidden = true; }
    });
    wrap.addEventListener("click", function (e) {
      var dot = e.target.closest(".lane-dot");
      if (dot && dot.getAttribute("data-obs")) {
        e.preventDefault();
        window.location = "/runs/" + run + "/frames#" + dot.getAttribute("data-obs");
      }
    });
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
          /* A non-OK answer (a redeploy's brief 503) must reschedule too,
             or one bad response freezes the board until a manual reload. */
          if (!data) { window.setTimeout(poll, 10000); return; }
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


  /* ---------- 6b. a disabled fix option explains itself ----------
     A disabled radio ignores the click and the form quietly submits the
     already-checked default -- which once turned three explicit "Regenerate
     with Veo" picks into two centre crops. The click now says why the row
     cannot run instead of pretending nothing happened. */

  document.querySelectorAll(".fix-opt.off").forEach(function (row) {
    row.addEventListener("click", function () {
      var form = row.closest("form");
      var note = form && form.querySelector(".fix-refused");
      if (!note) { return; }
      var name = row.querySelector(".fix-m b");
      var why = row.querySelector(".fix-why");
      note.textContent = (name ? name.textContent : "That option") +
        " cannot run right now: " +
        (why ? why.textContent : "see the note on the option.") +
        " Your current selection is unchanged.";
      note.hidden = false;
    });
  });


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

    /* Cards are the default. List is stored as "list", never as a missing
       key: with cards defaulting, absence means "has not chosen", so
       removing the key would throw an explicit List choice away on the next
       page load -- the same trap the style switch had. */
    var apply = function (view) {
      list.classList.toggle("as-rows", view === "list");
      buttons.forEach(function (b) {
        b.classList.toggle("on", b.getAttribute("data-set-runs") === view);
      });
    };
    var saved = "cards";
    try {
      var stored = localStorage.getItem(KEY);
      if (stored === "list" || stored === "cards") { saved = stored; }
      else if (stored) { localStorage.removeItem(KEY); }
    } catch (e) { /* private mode */ }
    apply(saved);

    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        var view = b.getAttribute("data-set-runs") || "cards";
        try { localStorage.setItem(KEY, view); } catch (e) { /* private mode */ }
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

    /* `html` is the server's own rendering of the same sentence: it has
       the market and rule tables, so it can turn a rule id into its class
       chip and a market into its country chip, and it escapes before it
       marks up. Anything the model wrote can only be RECOGNISED as one of
       ours, never injected as markup. Without it we fall back to text
       nodes, which is what every message used to be. */
    var bubble = function (who, text, icon, html) {
      var el = document.createElement("div");
      el.className = "ag-msg ag-" + who;
      el.innerHTML = '<svg class="ic"><use href="#i-' + icon + '"/></svg>' +
                     '<div class="ag-body"></div>';
      var body = el.querySelector(".ag-body");
      if (html) {
        body.innerHTML = html;
      } else {
        String(text).split("\n").forEach(function (line) {
          if (!line.trim()) { return; }
          var p = document.createElement("p");
          p.textContent = line;
          body.appendChild(p);
        });
      }
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
          if (data.reply) { bubble("agent", data.reply, "adjudicator", data.reply_html); }
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

    /* Upload from inside the conversation.

       It posts to /runs, the same route the upload form uses, and reads
       the run id off the redirect it follows. No second upload endpoint:
       the limits, the rejections and the plain-text 400s are already
       right there and would only have to be reimplemented.

       Markets default to the global baseline and the EU layer so the run
       starts immediately. Adding more is cheap afterwards -- /analysis
       re-judges the observations this run already has, without opening
       the asset again -- and the agent offers it. That is the trade: get
       moving, then refine by talking, rather than filling in a form. */
    var drop = document.getElementById("agent-drop");
    var file = document.getElementById("agent-file");
    var dropText = document.getElementById("agent-drop-text");

    var upload = function (chosen) {
      if (busy || !chosen) { return; }
      busy = true;
      bubble("you", "Uploading " + chosen.name, "human");
      var note = bubble("agent", "", "pending");
      note.querySelector(".ag-body").innerHTML =
        '<span class="ag-think"><span class="spin"></span>uploading and starting the run</span>';

      var body = new FormData();
      body.append("asset", chosen);
      ["GLOBAL", "EU"].forEach(function (m) { body.append("markets", m); });

      fetch("/runs", { method: "POST", body: body })
        .then(function (r) {
          if (!r.ok) { return r.text().then(function (t) { throw new Error(t); }); }
          return r.url;
        })
        .then(function (url) {
          note.remove();
          var id = (String(url).match(/\/runs\/([a-z0-9_]+)/) || [])[1];
          if (!id) { throw new Error("the run started but did not say where"); }
          runId = id;
          document.querySelector(".agent").setAttribute("data-run", id);
          if (dropText) { dropText.textContent = chosen.name + " — run " + id; }
          open("/runs/" + id, "Launch board", "/runs/" + id);
          busy = false;
          ask("I have just uploaded " + chosen.name +
              " and started run " + id +
              " against the global baseline and the EU. Tell me what happens now," +
              " and what you would do next.");
        })
        .catch(function (err) {
          note.remove();
          busy = false;
          var failed = bubble("agent", String(err.message || err).slice(0, 400), "error");
          failed.querySelector(".ag-body").classList.add("ag-err");
        });
    };

    if (file) {
      file.addEventListener("change", function () { upload(file.files[0]); });
    }
    if (drop) {
      ["dragenter", "dragover"].forEach(function (e) {
        drop.addEventListener(e, function (ev) {
          ev.preventDefault(); drop.classList.add("over");
        });
      });
      ["dragleave", "drop"].forEach(function (e) {
        drop.addEventListener(e, function (ev) {
          ev.preventDefault(); drop.classList.remove("over");
          if (e === "drop" && ev.dataTransfer) { upload(ev.dataTransfer.files[0]); }
        });
      });
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      ask(input.value);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".sugg"), function (b) {
      b.addEventListener("click", function () { ask(b.getAttribute("data-say")); });
    });
  })();
