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

  var STATE_WORDS = { error: "not evaluated", at_risk: "at risk" };
  var word = function (state) { return STATE_WORDS[state] || state; };

  /* The icon system (see docs/design/icons): every state and agent has one
     mark, drawn once as <symbol>s in base.html and referenced by id here. */
  var STATE_ICONS = { cleared: 1, at_risk: 1, blocked: 1, pending: 1, error: 1 };
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
        var state = market.errored ? "error" : market.clearance;
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
    stream.addEventListener("mission", function (message) {
      var event = JSON.parse(message.data);
      var empty = feed.querySelector(".empty");
      if (empty) { empty.remove(); }

      var line = document.createElement("div");
      line.className = "line new" + (event.message.indexOf("stage_error") >= 0 ? " err" : "");
      var when = document.createElement("span");
      when.className = "t";
      when.textContent = event.clock;
      var who = document.createElement("span");
      who.className = "a a-" + event.agent;
      who.innerHTML = '<svg class="ic"><use href="#i-' +
        (AGENT_ICONS[event.agent] ? event.agent : "pipeline") + '"/></svg>';
      who.appendChild(document.createTextNode(event.agent));
      var what = document.createElement("span");
      what.className = "m";
      what.textContent = event.message;
      line.appendChild(when);
      line.appendChild(who);
      line.appendChild(what);
      feed.appendChild(line);

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
          if (mode) { localStorage.setItem(KEY, mode); }
          else { localStorage.removeItem(KEY); }
        } catch (e) { /* private mode: the choice just does not persist */ }
        mark();
      });
    });

    mark();
  })();
