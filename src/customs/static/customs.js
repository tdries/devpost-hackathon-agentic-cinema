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
            "link instead. The server fetches that itself."
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
          button.classList.add("sparkle");
          button.textContent = "Uploading the master…";
        }, 0);
      }
    });
  }

  /* An agent's message is data, not markup: it carries model-written text
     and file paths, so it is escaped before it reaches innerHTML. */
  /* The paragraph that explains the screen, folded away.
     Almost every screen opened with a paragraph about the screen, so the
     first thing on a page whose subject is a verdict was prose about the
     page. It is still one click away, the choice is remembered across
     screens, and with no JS it simply stays visible -- which is what it
     did before. Two pages opt out with .keep: on the door and the
     "already analysed" page the prose IS the content. */
  (function () {
    var KEY = "customs-explain";
    var open = false;
    try { open = window.localStorage.getItem(KEY) === "open"; } catch (e) { open = false; }
    var prose = document.querySelectorAll("h1.pagetitle + p.subline:not(.keep)");
    Array.prototype.forEach.call(prose, function (para) {
      var title = para.previousElementSibling;
      if (!title) { return; }
      var button = document.createElement("button");
      button.type = "button";
      button.className = "explain-btn";
      button.title = "What this screen is";
      button.setAttribute("aria-label", "What this screen is");
      button.innerHTML = '<svg class="ic"><use href="#i-noted"/></svg>';
      para.hidden = !open;
      button.setAttribute("aria-expanded", open ? "true" : "false");
      button.addEventListener("click", function () {
        var showing = para.hidden;
        para.hidden = !showing;
        button.setAttribute("aria-expanded", showing ? "true" : "false");
        try {
          window.localStorage.setItem(KEY, showing ? "open" : "closed");
        } catch (e) { /* private window: the toggle still works this visit */ }
      });
      title.appendChild(button);
    });
  })();

  /* ---------- 1d. live panel or drawn chart, per card ----------
     Every archive card carries both: the panel Grafana draws and the SVG
     this app draws. Live is the default because it is the real thing, and
     the switch is remembered for the whole archive at once -- nobody wants
     to make that choice thirty-five times.

     The live frame is inert markup (data-src) until a card is actually
     approached, and then one at a time. A lazy iframe loads the moment it
     is anywhere near the viewport, and thirty-nine of them booting a
     Grafana each is what kept a real browser off this page for thirty
     seconds. */
  (function () {
    /* The key is versioned. Drawn was the default for one deploy, and
       every reader who loaded the archive in that window has "drawn"
       remembered in their browser -- so flipping the default back to live
       changed nothing for exactly the people who had already been here.
       A new key ignores the old answer once, and remembers the new one. */
    var KEY = "customs-cardviz2";
    var cards = document.querySelectorAll(".cardviz");
    if (!cards.length) { return; }

    /* Live by default: Grafana's own panel of the same answer. The
       console's drawing holds the slot until the frame paints, and is
       where a reader lands who clicks across. */
    var mode = "live";
    try { mode = window.localStorage.getItem(KEY) || "live"; } catch (e) { mode = "live"; }

    var pending = [];
    var booting = 0;
    /* Three at a time, not one and not all of them. A beat between them was
       never a queue: the next frame started 300ms later whether the last one
       had painted or not, so nine panels booted a Grafana each inside three
       seconds, the single small viewer instance took all nine at once, and
       not one of them finished. Measured on the deployed archive: zero cards
       live after a hundred seconds, every one of them showing the drawing
       instead. A slot opens when a panel paints, and three of them share the
       viewer without swamping it. */
    var MAX_LIVE = 3;

    var boot = function () {
      while (booting < MAX_LIVE && pending.length) {
        var frame = pending.shift();
        if (!frame || frame.dataset.src === undefined) { continue; }
        booting += 1;
        (function (f) {
          var freed = false;
          var free = function () {
            if (freed) { return; }
            freed = true;
            booting -= 1;
            boot();
          };
          /* The card shows its own drawing until this frame has actually
             painted, so a reader scrolling the archive never meets an empty
             white box where a chart belongs. */
          f.addEventListener("load", function () {
            var card = f.closest(".cardviz");
            if (card) { card.classList.add("booted"); }
            free();
          }, { once: true });
          f.src = f.dataset.src;
          delete f.dataset.src;
          /* A panel that never paints must not hold its slot forever. */
          window.setTimeout(free, 15000);
        })(frame);
      }
    };

    /* Near enough to be worth a Grafana. The observer below says the same
       thing on scroll; this is for the cards that are already on screen when
       the page arrives, and for the ones on screen when a reader switches
       back to live. */
    var near = function (card) {
      var box = card.getBoundingClientRect();
      var tall = window.innerHeight || document.documentElement.clientHeight;
      return box.top < tall + 300 && box.bottom > -300;
    };

    var wake = function (card) {
      var frame = card.querySelector(".cardlanes.live");
      if (!frame || frame.dataset.src === undefined) { return false; }
      if (card.dataset.viz !== "live") { return false; }
      if (!near(card)) { return false; }
      if (pending.indexOf(frame) === -1) { pending.push(frame); }
      boot();
      return true;
    };

    /* Hovering a dot plays that moment of the film.
       Two things have to be true for that: the chart has to be real markup
       rather than an <img> -- no event reaches inside a picture -- and the
       moment has to be mapped, because the card's clip is the whole film
       in about five seconds. The chart carries the film's length and each
       dot the second it sits at, so the fraction is the same in both. */
    var scrub = function (card, holder) {
      var svg = holder.querySelector("svg");
      var row = card.closest(".runcard");
      var video = row ? row.querySelector("video.runthumb") : null;
      var span = svg ? parseFloat(svg.dataset.duration) : 0;
      if (!video || !span) { return; }
      holder.addEventListener("pointerover", function (event) {
        var dot = event.target.closest ? event.target.closest(".lane-dot") : null;
        if (!dot) { return; }
        if (video.preload !== "auto") { video.preload = "auto"; video.load(); }
        var at = parseFloat(dot.dataset.t) / span;
        var seek = function () {
          try { video.currentTime = Math.max(0, Math.min(0.999, at)) * (video.duration || 0); } catch (e) {}
          var playing = video.play();
          if (playing && playing.catch) { playing.catch(function () {}); }
        };
        if (video.readyState >= 1) { seek(); }
        else { video.addEventListener("loadedmetadata", seek, { once: true }); }
      });
    };

    /* The drawn chart arrives as an <img> and is swapped for the markup the
       first time this reader asks for drawn: it costs a fetch nobody makes
       who never leaves the live panel. */
    var inline = function (card) {
      var img = card.querySelector("img.cardlanes.drawn");
      if (!img || card.dataset.inlined) { return; }
      card.dataset.inlined = "1";
      window.fetch(img.getAttribute("src"))
        .then(function (r) { return r.ok ? r.text() : ""; })
        .then(function (svg) {
          if (!svg || svg.indexOf("<svg") === -1) { return; }
          var slot = document.createElement("span");
          slot.className = "cardlanes drawn";
          slot.innerHTML = svg;
          img.parentNode.replaceChild(slot, img);
          scrub(card, slot);
        })
        .catch(function () { /* the <img> stays, and still draws */ });
    };

    var apply = function (card) {
      card.dataset.viz = mode;
      var picks = card.parentElement
        ? card.parentElement.querySelectorAll("[data-viz-pick]") : [];
      Array.prototype.forEach.call(picks, function (button) {
        var on = button.dataset.vizPick === mode;
        button.classList.toggle("on", on);
        button.setAttribute("aria-pressed", on ? "true" : "false");
      });
      if (mode === "live") { wake(card); } else { inline(card); }
    };

    Array.prototype.forEach.call(cards, apply);

    document.addEventListener("click", function (event) {
      var button = event.target.closest ? event.target.closest("[data-viz-pick]") : null;
      if (!button) { return; }
      event.preventDefault();
      mode = button.dataset.vizPick === "drawn" ? "drawn" : "live";
      try { window.localStorage.setItem(KEY, mode); } catch (e) { /* private window */ }
      Array.prototype.forEach.call(document.querySelectorAll(".cardviz"), apply);
    });

    if (window.IntersectionObserver) {
      var watcher = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          /* Only stop watching a card that actually took its panel. A card
             passed over while the reader was on the drawing would otherwise
             be unobserved having booted nothing, and switching back to live
             left it drawn for the rest of the visit. */
          if (entry.isIntersecting && wake(entry.target)) {
            watcher.unobserve(entry.target);
          }
        });
      }, { rootMargin: "300px" });
      Array.prototype.forEach.call(cards, function (card) { watcher.observe(card); });
    } else {
      Array.prototype.forEach.call(cards, wake);
    }
  })();

  function escapeHtml(text) {
    var d = document.createElement("div");
    d.textContent = text == null ? "" : String(text);
    return d.innerHTML;
  }

  /* ---------- 1a2. continue where you left off ----------
     Run pages remember themselves; app-level screens offer the way back.
     The id is validated twice -- written from our own URL match, and
     matched again before it is ever put into the page. */

  (function () {
    var here = location.pathname.match(/^\/runs\/(run_[a-z0-9]{6,32})(\/|$)/);
    try {
      if (here) { localStorage.setItem("customs-last-run", here[1]); return; }
      var last = localStorage.getItem("customs-last-run");
      if (!last || !/^run_[a-z0-9]{6,32}$/.test(last)) { return; }
      var nav = document.querySelector(".runnav .screens");
      if (!nav || document.querySelector(".crumb")) { return; }
      var link = document.createElement("a");
      link.className = "tab tab-continue";
      link.href = "/runs/" + last;
      link.innerHTML = '<svg class="ic"><use href="#n-board"/></svg>' +
        'continue: <span class="mono">' + last + "</span>";
      nav.appendChild(link);
    } catch (e) {}
  })();


  /* ---------- 1a3. the landing's before/after, in lockstep ----------
     Two muted looping videos of identical length drift apart over
     minutes; a gentle corrector keeps the after glued to the before so
     the comparison stays honest. */

  (function () {
    var groups = {};
    document.querySelectorAll("video[data-lockstep]").forEach(function (v) {
      var key = v.getAttribute("data-lockstep") || "pair";
      (groups[key] = groups[key] || []).push(v);
    });
    Object.keys(groups).forEach(function (key) {
      var pair = groups[key];
      if (pair.length !== 2) { return; }
      var lead = pair[0], follow = pair[1];
      window.setInterval(function () {
        if (lead.paused || follow.paused) { return; }
        if (Math.abs(follow.currentTime - lead.currentTime) > 0.12) {
          follow.currentTime = lead.currentTime;
        }
      }, 500);
    });
  })();


  /* ---------- 1a3b. the landing's fix rotator ----------
     One fix on screen at a time, large enough to judge. It advances every
     seven seconds and stops for good the moment somebody picks a tab,
     because a slide that moves while you are reading it is worse than one
     that never moves. Hovering pauses it too.

     It also pauses the videos it is not showing: six autoplaying loops on
     the front page is four more decoders than anyone is looking at. And a
     lane restarts from zero when it comes round, so before and after are
     always compared from the same frame. */

  (function () {
    var show = document.getElementById("fixshow");
    if (!show) { return; }
    var lanes = [].slice.call(show.querySelectorAll(".lane"));
    var tabs = [].slice.call(show.querySelectorAll(".fixtab"));
    if (lanes.length < 2 || tabs.length !== lanes.length) { return; }

    var EVERY = 7000;
    var at = -1, timer = null, taken = false;
    var calm = window.matchMedia
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    var paint = function (want) {
      var next = (want + lanes.length) % lanes.length;
      if (next === at) { return; }
      at = next;
      lanes.forEach(function (lane, n) {
        var on = n === at;
        lane.classList.toggle("off", !on);
        lane.querySelectorAll("video").forEach(function (v) {
          if (!on) { v.pause(); return; }
          try { v.currentTime = 0; } catch (e) { /* not seekable yet */ }
          var playing = v.play();
          if (playing && playing.catch) { playing.catch(function () {}); }
        });
      });
      tabs.forEach(function (tab, n) {
        tab.classList.toggle("on", n === at);
        tab.setAttribute("aria-selected", n === at ? "true" : "false");
      });
    };

    var stop = function () {
      if (timer) { window.clearInterval(timer); timer = null; }
    };
    var start = function () {
      if (calm || taken || timer) { return; }
      timer = window.setInterval(function () { paint(at + 1); }, EVERY);
    };

    tabs.forEach(function (tab, n) {
      tab.addEventListener("click", function () {
        taken = true;
        stop();
        paint(n);
      });
    });
    show.addEventListener("mouseenter", stop);
    show.addEventListener("mouseleave", start);
    show.addEventListener("focusin", stop);

    show.classList.add("on");
    paint(0);
    start();
  })();


  /* ---------- 1a3c. the archive's load-more ----------
     Nine films with the page, the rest on request. The server renders the
     same partial the first nine came from, so a second page cannot drift
     from the first. With scripting off the button is not there and the
     noscript link goes to ?all=1. */

  (function () {
    var button = document.getElementById("loadmore");
    var list = document.getElementById("runlist");
    if (!button || !list) { return; }
    var busy = false;

    button.addEventListener("click", function () {
      if (busy) { return; }
      busy = true;
      var next = parseInt(button.getAttribute("data-next"), 10) || 0;
      var total = parseInt(button.getAttribute("data-total"), 10) || 0;
      button.disabled = true;
      var was = button.innerHTML;
      button.textContent = "Loading...";
      fetch("/runs?fragment=1&offset=" + next)
        .then(function (r) { return r.text(); })
        .then(function (html) {
          var slot = document.createElement("div");
          slot.innerHTML = html;
          var added = 0;
          while (slot.firstChild) {
            var node = slot.firstChild;
            slot.removeChild(node);
            if (node.nodeType === 1 && node.classList.contains("runcard")) { added++; }
            list.appendChild(node);
          }
          next += added;
          button.setAttribute("data-next", next);
          if (added === 0 || next >= total) {
            button.parentNode.removeChild(button);
          } else {
            button.disabled = false;
            button.innerHTML = was.replace(/\(\d+ more/, "(" + (total - next) + " more");
          }
          busy = false;
        })
        .catch(function () {
          button.disabled = false;
          button.innerHTML = was;
          busy = false;
        });
    });
  })();


  /* ---------- 1a4. the activity beacon ----------
     Wherever you are in the console, whatever is running shows in the
     topbar: runs mid-analysis, fixes in the making. Fed by /ops/busy,
     the same answer the deploy gate trusts. */

  (function () {
    var beacon = document.getElementById("beacon");
    if (!beacon) { return; }
    var RUN_ID = /^run_[a-z0-9]{6,32}$/;
    var paint = function (d) {
      var runs = (d.running_runs || []).filter(function (r) { return RUN_ID.test(r); });
      var fixes = (d.remediating_runs || []).filter(function (r) { return RUN_ID.test(r); });
      if (!d.busy || (!runs.length && !fixes.length && !(d.remediating_findings || []).length)) {
        beacon.hidden = true;
        beacon.classList.remove("sparkle");
        return;
      }
      var parts = [];
      if (runs.length) { parts.push(runs.length + " run" + (runs.length === 1 ? "" : "s") + " analysing"); }
      var fixCount = (d.remediating_findings || []).length;
      if (fixCount) { parts.push(fixCount + " fix" + (fixCount === 1 ? "" : "es") + " running"); }
      beacon.textContent = parts.join(" · ") || "working";
      beacon.href = runs.length ? "/runs/" + runs[0] + "/mission"
                  : fixes.length ? "/runs/" + fixes[0] : "/runs";
      beacon.classList.add("sparkle");
      beacon.hidden = false;
    };
    var poll = function () {
      fetch("/ops/busy", { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d) { paint(d); } })
        .catch(function () {});
    };
    poll();
    window.setInterval(poll, 9000);
  })();


  /* ---------- 1a5. a run card plays itself on hover ----------
     The clip is preload="none", so nothing is fetched until a pointer
     lands on the card; leaving rewinds it so the next hover starts at the
     top. A card whose master is gone answers 404 and simply keeps its
     poster. */

  document.querySelectorAll("video[data-hoverplay]").forEach(function (video) {
    var card = video.closest("a") || video;
    var stop = function () {
      video.pause();
      try { video.currentTime = 0; } catch (e) {}
    };
    /* Warm it as the pointer approaches the card, not when the page loads:
       thirty-nine clips fetching metadata up front is thirty-nine range
       requests over six connections, and the archive stopped loading. */
    var warm = function () {
      if (video.preload !== "auto") { video.preload = "auto"; video.load(); }
    };
    card.addEventListener("pointerenter", warm);
    card.addEventListener("focusin", warm);
    card.addEventListener("mouseenter", function () {
      warm();
      var playing = video.play();
      if (playing && playing.catch) { playing.catch(function () {}); }
    });
    card.addEventListener("mouseleave", stop);
    /* a phone has no hover: tapping the card navigates, so leave it alone */
  });


  /* ---------- 1a6. the market room's scene panels ----------
     Closed by default; a scene with a fix already running opens itself
     server-side. Keyboard reachable, because a table row that acts like a
     button has to behave like one. */

  document.querySelectorAll("tbody.mkscene > .scene-row").forEach(function (row) {
    var body = row.parentNode;
    var toggle = function () {
      var open = body.hasAttribute("data-open");
      if (open) { body.removeAttribute("data-open"); }
      else { body.setAttribute("data-open", ""); }
      row.setAttribute("aria-expanded", open ? "false" : "true");
    };
    row.addEventListener("click", function (e) {
      /* a click on the frames opens the frame, not the panel */
      if (e.target.closest("a")) { return; }
      toggle();
    });
    row.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  });


  /* ---------- 1a7. the market room's scene filter ----------
     The strip used to jump to an anchor, which on a long market meant
     scrolling back up to pick the next one. Clicking a scene now shows
     only that scene, opened; clicking it again, or All, shows everything.
     Without JavaScript the anchors still jump, which is why they are
     anchors. */

  /* Both strips behave the same way: the market room filters its scene
     panels, the frame board filters its scene cards. */
  [["mk-scenes", "tbody.mkscene", ".scene-row", /^mk-/],
   ["fb-scenes", ".fb > .sc", null, /^sc-/]].forEach(function (spec) {
    var strip = document.getElementById(spec[0]);
    if (!strip) { return; }
    var panels = document.querySelectorAll(spec[1]);
    var chips = strip.querySelectorAll("[data-scene]");
    var current = "";

    var apply = function (scene) {
      current = scene;
      chips.forEach(function (c) {
        c.classList.toggle("on", (c.getAttribute("data-scene") || "") === scene);
      });
      panels.forEach(function (panel) {
        var row = spec[2] ? panel.querySelector(spec[2]) : panel;
        var id = row ? (row.id || "").replace(spec[3], "") : "";
        var show = !scene || id === scene;
        panel.hidden = !show;
        /* a filtered-to scene opens itself: you asked for exactly it */
        if (scene && show && spec[2]) {
          panel.setAttribute("data-open", "");
          if (row) { row.setAttribute("aria-expanded", "true"); }
        }
      });
    };

    chips.forEach(function (chip) {
      chip.addEventListener("click", function (e) {
        e.preventDefault();
        var scene = chip.getAttribute("data-scene") || "";
        apply(scene === current ? "" : scene);
      });
    });
  });


  /* ---------- 1c. "show what was spotted" ----------
     A green rectangle over the evidence frame, per thumbnail, off by
     default. The box is fetched once and cached on the element: an
     observation made before the analyst was asked for one has to be
     located by a model, which costs a call, so it happens on the first
     tick and never again. Nothing is ever drawn into the image itself --
     that PNG is what a remediation edits and what Veo is anchored on. */

  /* The cutting room reuses this: same markup, same route, same reason
     (the box is never drawn into the PNG a remediation edits). */
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
        + (markets ? ", " + markets : "");
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
        if (fill) {
          fill.style.width = data.progress.pct + "%";
          /* the colours stop flowing when the run does */
          fill.classList.toggle("done", !!data.done);
        }
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
    /* The stage sentences, from the server, so a group that arrives over
       SSE reads exactly like one the server already rendered. */
    var PROSE = {};
    try { PROSE = JSON.parse(document.getElementById("stage-prose").textContent); }
    catch (e) {}

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
      var said = PROSE[agent] || ["Working", "The crew is busy on this run."];
      el.innerHTML =
        '<summary><svg class="ic"><use href="#i-' + markId + '"/></svg>' +
        '<span class="gtext"><span class="gtitle"></span>' +
        '<span class="gprose"></span><span class="gm mono"></span></span>' +
        '<span class="ga a-' + agent + '"></span>' +
        '<span class="gc mono">0 events</span>' +
        '<span class="gt mono"></span></summary>' +
        '<div class="glines"></div><span class="gbar"><i></i></span>';
      el.querySelector(".gtitle").textContent = said[0];
      el.querySelector(".gprose").textContent = said[1];
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

    /* Every Grafana panel on the page carries the theme in its URL, and
       the URL was built by the server from the cookie below. On a toggle
       the cookie is right for the NEXT page load and the panels on this
       one are still the old colour, so re-source them in place: one query
       parameter, same panel, same window. An iframe reload is cheap and
       the alternative is a white chart on a black page.

       Grafana's own theme param is light|dark; the console's is
       studio|mission. They are the same two states under two names. */
    var repaint = function (mode) {
      var want = mode === "mission" ? "dark" : "light";
      document.querySelectorAll("iframe[src], img[src]").forEach(function (el) {
        var src = el.getAttribute("src") || "";
        if (src.indexOf("theme=") === -1) { return; }
        var next = src.replace(/([?&])theme=(light|dark)/, "$1theme=" + want);
        if (next !== src) { el.setAttribute("src", next); }
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
        try {
          document.cookie = "customs-theme=" + (mode || "mission") +
                            ";path=/;max-age=31536000;samesite=Lax";
        } catch (e) { /* the server falls back to light, which is the default */ }
        repaint(mode || "mission");
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
      /* Not a spinner. The agent records every tool call as it makes it,
         and /agent/progress hands that list back while the turn is still
         running -- so what the page shows is what is happening, in the
         order it happened, rather than a word that means "wait". */
      var thinking = bubble("agent", "", "pending");
      thinking.querySelector(".ag-body").innerHTML =
        '<div class="ag-phases">' +
        '  <div class="ag-bar"><i></i></div>' +
        '  <ol class="ag-steps"><li class="on">reading the question</li></ol>' +
        '</div>';
      var steps = thinking.querySelector(".ag-steps");
      var shown = 0;
      var watch = window.setInterval(function () {
        fetch("/agent/progress?session=" + encodeURIComponent(session))
          .then(function (r) { return r.json(); })
          .then(function (p) {
            var phases = (p && p.phases) || [];
            if (phases.length <= shown) { return; }
            var live = steps.querySelector("li.on");
            if (live) { live.classList.remove("on"); live.classList.add("done"); }
            phases.slice(shown).forEach(function (what, i) {
              var li = document.createElement("li");
              li.textContent = what;
              li.className = (shown + i === phases.length - 1) ? "on" : "done";
              steps.appendChild(li);
            });
            shown = phases.length;
            log.scrollTop = log.scrollHeight;
          })
          .catch(function () {});
      }, 600);
      var stopWatching = function () { window.clearInterval(watch); };

      var body = new FormData();
      body.append("message", text);
      body.append("session", session);
      body.append("run", runId);

      fetch("/agent/ask", { method: "POST", body: body })
        .then(function (r) { return r.json().catch(function () { return null; }); })
        .then(function (data) {
          stopWatching();
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
          nextMoves(data.follow_ups);
          log.scrollTop = log.scrollHeight;
        })
        .catch(function () {
          stopWatching();
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
          if (dropText) { dropText.textContent = chosen.name + ", run " + id; }
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

    /* The rail under the conversation, rewritten after every turn.

       It opened with seven suggestions and then never changed again: from
       the second message on, the operator faced a blank box and had to
       invent the next question, and the invented one is usually the one
       the console cannot do. The server sends what to ask next, derived
       from the tools the turn actually called, so the rail is always about
       what is on screen now -- and never empty. */
    var suggest = document.getElementById("agent-suggest");

    var nextMoves = function (moves) {
      if (!suggest || !moves || !moves.length) { return; }
      suggest.innerHTML = "";
      /* The starters are a folded accordion until the first turn; what
         replaces them is a flat rail about the answer on screen, so it
         opens and keeps a summary -- a <details> with neither is a rail
         nobody can see. */
      var isFold = suggest.tagName === "DETAILS";
      if (isFold) { suggest.open = true; }
      suggest.classList.remove("guided");
      var label = document.createElement(isFold ? "summary" : "span");
      label.className = "sugg-label label";
      label.textContent = "What next";
      suggest.appendChild(label);
      moves.forEach(function (m) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "sugg";
        b.setAttribute("data-say", m.say);
        b.title = m.say;
        b.innerHTML = '<svg class="ic"><use href="#' + m.icon + '"></use></svg>';
        b.appendChild(document.createTextNode(m.label));
        suggest.appendChild(b);
      });
    };

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      ask(input.value);
    });
    /* Delegated, because the rail is replaced after every turn and a
       listener bound to the opening buttons would die with them. */
    document.addEventListener("click", function (event) {
      var b = event.target.closest && event.target.closest(".sugg");
      if (b && b.getAttribute("data-say")) { ask(b.getAttribute("data-say")); }
    });
  })();

/* ---------- /edits: the pair plays together, and picture or sound ----------
   The two clips are the same seconds of the same shot, so they are worth
   nothing apart: hovering a card plays both from the top, leaving it stops
   and rewinds both. Sound edits are left alone -- they carry their own
   controls, and a revoice that starts talking because a pointer crossed it
   is a page nobody can read next to.

   The toggle is remembered, because a reviewer working through the sound
   edits does not want to re-pick it on every visit. */
(function () {
  var grid = document.querySelector(".editgrid");
  if (!grid) { return; }
  var KEY = "customs-editkind";

  var pick = function (kind) {
    grid.dataset.kind = kind;
    var buttons = document.querySelectorAll("[data-edit-kind]");
    Array.prototype.forEach.call(buttons, function (b) {
      var on = b.dataset.editKind === kind;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    try { window.localStorage.setItem(KEY, kind); } catch (e) { /* private */ }
  };

  var want = "video";
  try { want = window.localStorage.getItem(KEY) || "video"; } catch (e) { want = "video"; }
  pick(want === "audio" ? "audio" : "video");

  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("[data-edit-kind]") : null;
    if (!button) { return; }
    event.preventDefault();
    pick(button.dataset.editKind === "audio" ? "audio" : "video");
  });

  /* Removing an edit deletes a database row, two stills and a rendered
     clip, and the reply only comes back once all of that is on disk. Long
     enough that the card sitting there reads as a button that did nothing.

     So the card goes the moment the form is sent, and the deletion carries
     on behind it. If the server refuses -- the wrong word, or a change that
     is already gone -- the card comes back with the reason on it, which is
     the only honest way to do this optimistically: never pretend a refusal
     was a success. Without fetch the form posts as it always did. */
  grid.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !form.action || form.action.indexOf("/delete") < 0) { return; }
    var card = form.closest ? form.closest(".editcard") : null;
    if (!card || !window.fetch || !window.FormData) { return; }
    event.preventDefault();

    var body = new FormData(form);
    var refusal = form.querySelector(".editrefused");
    if (refusal) { refusal.remove(); }
    card.setAttribute("data-going", "");
    /* out of the layout once the collapse has played, not before, or the
       cards below it jump before the eye has followed this one */
    var hide = window.setTimeout(function () { card.hidden = true; }, 320);

    fetch(form.action, { method: "POST", body: body, redirect: "follow" })
      .then(function (res) {
        if (!res.ok || (res.url && res.url.indexOf("wrong=") >= 0)) {
          throw new Error("refused");
        }
        card.remove();
        var count = document.querySelector("[data-edit-kind='" +
          (card.dataset.kind === "audio" ? "audio" : "video") + "'] b");
        if (count) {
          var left = parseInt(count.textContent, 10);
          if (!isNaN(left) && left > 0) { count.textContent = String(left - 1); }
        }
      })
      .catch(function () {
        window.clearTimeout(hide);
        card.hidden = false;
        card.removeAttribute("data-going");
        var note = document.createElement("p");
        note.className = "label editrefused";
        note.textContent = "That is not the word. Nothing was removed.";
        form.appendChild(note);
        var pw = form.querySelector("input[type=password]");
        if (pw) { pw.value = ""; pw.focus(); }
      });
  });

  Array.prototype.forEach.call(grid.querySelectorAll("[data-pairplay]"),
    function (pair) {
      if (pair.classList.contains("editpair-snd")) { return; }
      var clips = pair.querySelectorAll("video");
      var card = pair.closest(".editcard") || pair;
      card.addEventListener("mouseenter", function () {
        Array.prototype.forEach.call(clips, function (clip) {
          if (clip.preload !== "auto") { clip.preload = "auto"; clip.load(); }
          try { clip.currentTime = 0; } catch (e) {}
          var playing = clip.play();
          if (playing && playing.catch) { playing.catch(function () {}); }
        });
      });
      card.addEventListener("mouseleave", function () {
        Array.prototype.forEach.call(clips, function (clip) {
          clip.pause();
          try { clip.currentTime = 0; } catch (e) {}
        });
      });
    });
})();

/* The walkthrough on the front page: the thumbnail is a button, and the
   iframe is written in when it is pressed. Nothing of YouTube's is loaded
   before that -- no player, no cookies -- on the page most likely to be
   opened and closed again. Autoplay on the injected frame, because the
   press that asked for the video should be the press that plays it. */
(function () {
  var face = document.querySelector(".vidface");
  if (!face) { return; }
  face.addEventListener("click", function () {
    var id = face.dataset.video;
    var start = parseInt(face.dataset.start, 10) || 0;
    if (!/^[\w-]{6,20}$/.test(id || "")) { return; }
    var frame = document.createElement("iframe");
    frame.src = "https://www.youtube-nocookie.com/embed/" + id
      + "?autoplay=1&rel=0&modestbranding=1" + (start ? "&start=" + start : "");
    frame.title = "The Media Customs walkthrough";
    frame.allow = "accelerometer; autoplay; clipboard-write; encrypted-media; "
      + "gyroscope; picture-in-picture; web-share";
    frame.setAttribute("allowfullscreen", "");
    face.replaceWith(frame);
  });
})();

/* ---------- one framed panel at a time ----------
   Grafana is a single small instance and a page that asks it for four
   panels at once gets 5xx back for some of them: "the server encountered
   an error, try again in 30 seconds", rendered inside the iframe where it
   looks like a chart with no data. The archive's cards have always been
   queued for this reason; every other framed panel is queued the same way
   now -- inert markup until it is approached, then one every 300ms, and
   the wrapper is marked `booted` when the frame has actually painted so a
   placeholder can step aside.

   With JavaScript off these frames stay empty, which is the same trade the
   cards already make: thirty Grafanas booting at once is a page nobody can
   load. */
(function () {
  var frames = document.querySelectorAll("iframe[data-src]:not(.cardlanes)");
  if (!frames.length) { return; }
  var queue = [];
  var busy = false;

  var boot = function () {
    if (busy || !queue.length) { return; }
    var frame = queue.shift();
    if (!frame || frame.dataset.src === undefined) { boot(); return; }
    busy = true;
    frame.addEventListener("load", function () {
      var box = frame.parentElement;
      if (box) { box.classList.add("booted"); }
    }, { once: true });
    frame.src = frame.dataset.src;
    delete frame.dataset.src;
    window.setTimeout(function () { busy = false; boot(); }, 300);
  };

  var wake = function (frame) {
    if (frame.dataset.src === undefined) { return; }
    if (queue.indexOf(frame) === -1) { queue.push(frame); }
    boot();
  };

  if (window.IntersectionObserver) {
    var watcher = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          wake(entry.target);
          watcher.unobserve(entry.target);
        }
      });
    }, { rootMargin: "400px" });
    Array.prototype.forEach.call(frames, function (f) { watcher.observe(f); });
  } else {
    Array.prototype.forEach.call(frames, wake);
  }
})();

/* The tab rows scroll sideways on a phone, so the tab you are ON has to be
   brought into view: otherwise a run screen opens showing LAUNCH BOARD
   while you are in the cutting room, and the row looks like it is lying. */
(function () {
  var rows = document.querySelectorAll(".runnav .wrap");
  Array.prototype.forEach.call(rows, function (row) {
    var on = row.querySelector(".tab.on");
    if (!on || row.scrollWidth <= row.clientWidth) { return; }
    var want = on.offsetLeft - (row.clientWidth - on.offsetWidth) / 2;
    row.scrollLeft = Math.max(0, want);
  });
})();

/* ==========================================================================
   THE FLOW PICTURE, PLAYED
   The front page's diagram walks itself through its eight moves: this sets
   data-step on the <svg> and the stylesheet does the rest -- everything not
   part of the step goes quiet, the arrow carrying it gets a heartbeat, and
   the detail panel on the left shows what that step is actually looking at.
   It plays once when it scrolls into view, because a diagram that will not
   hold still is a diagram nobody can read, and the button replays it.
   ========================================================================== */

(function () {
  var figure = document.getElementById("flow");
  if (!figure) { return; }
  var svg = figure.querySelector(".flowsvg");
  var button = document.getElementById("flow-play");
  var now = document.getElementById("flow-now");
  var items = Array.prototype.slice.call(figure.querySelectorAll(".flow-steps li"));
  var LAST = 8;
  var DWELL = 1700;
  var timer = null;

  var light = function (n) {
    if (!n) {
      svg.removeAttribute("data-step");
      figure.removeAttribute("data-step");
      items.forEach(function (li) { li.classList.remove("on"); });
      if (now) { now.textContent = ""; }
      if (button) { button.textContent = "play the flow"; }
      return;
    }
    svg.setAttribute("data-step", n);
    figure.setAttribute("data-step", n);
    items.forEach(function (li, i) { li.classList.toggle("on", i === n - 1); });
    if (now) { now.textContent = "step " + n + " of " + LAST; }
  };

  var stop = function () {
    if (timer) { window.clearTimeout(timer); timer = null; }
    light(0);
  };

  var play = function (from) {
    if (timer) { window.clearTimeout(timer); }
    var n = from || 1;
    if (button) { button.textContent = "stop"; }
    var tick = function () {
      light(n);
      if (n >= LAST) {
        /* it ends on the whole picture again, which is the state a reader
           wants to be left in */
        timer = window.setTimeout(stop, DWELL + 600);
        return;
      }
      n += 1;
      timer = window.setTimeout(tick, DWELL);
    };
    tick();
  };

  if (button) {
    button.addEventListener("click", function () {
      if (timer) { stop(); } else { play(1); }
    });
  }

  /* once, on approach, and never again on this page view */
  if (window.IntersectionObserver &&
      !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    var seen = false;
    var watcher = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting && !seen) {
          seen = true;
          watcher.disconnect();
          window.setTimeout(function () { play(1); }, 400);
        }
      });
    }, { threshold: 0.45 });
    watcher.observe(figure);
  }
})();

/* ==========================================================================
   THE TOUR
   Two engines. The deck is a carousel on /tour: arrows, dots, swipe, keys
   and an autoplay that advances on its own until you touch something. The
   walk is the same story spotlit on the live console -- it drives the app,
   navigating from screen to screen, because a spotlight over a real market
   room IS the market room and a screenshot of one is not.
   ========================================================================== */

(function () {
  var deck = document.getElementById("tour");
  if (!deck) { return; }

  var slides = Array.prototype.slice.call(deck.querySelectorAll(".ts"));
  var dots = Array.prototype.slice.call(deck.querySelectorAll(".tdot"));
  var fill = document.getElementById("tr-fill");
  var counter = document.getElementById("tour-n");
  var prev = document.getElementById("tour-prev");
  var next = document.getElementById("tour-next");
  var autoBtn = document.getElementById("tour-auto");
  var at = 0;
  var auto = true;
  var timer = null;
  var DWELL = 11000;

  var show = function (n) {
    at = Math.max(0, Math.min(slides.length - 1, n));
    slides.forEach(function (slide, i) { slide.hidden = i !== at; });
    dots.forEach(function (dot, i) { dot.classList.toggle("on", i === at); });
    if (fill) { fill.style.width = ((at + 1) / slides.length * 100) + "%"; }
    if (counter) { counter.textContent = String(at + 1); }
    if (prev) { prev.disabled = at === 0; }
    if (next) { next.disabled = at === slides.length - 1; }
    /* An autoplaying deck that keeps going after the last slide would
       loop somebody back to the splash forever. It stops there instead. */
    if (at === slides.length - 1) { stop(); }
    var video = slides[at].querySelector("video");
    if (video && video.paused) { video.play().catch(function () {}); }
  };

  var tick = function () {
    if (!auto) { return; }
    timer = window.setTimeout(function () {
      if (at < slides.length - 1) { show(at + 1); tick(); }
    }, DWELL);
  };
  var stop = function () {
    auto = false;
    if (timer) { window.clearTimeout(timer); timer = null; }
    if (autoBtn) { autoBtn.setAttribute("aria-pressed", "false"); }
  };

  if (prev) { prev.addEventListener("click", function () { stop(); show(at - 1); }); }
  if (next) { next.addEventListener("click", function () { stop(); show(at + 1); }); }
  dots.forEach(function (dot) {
    dot.addEventListener("click", function () {
      stop(); show(parseInt(dot.dataset.tourGo, 10) || 0);
    });
  });
  deck.querySelectorAll("[data-tour-next]").forEach(function (button) {
    button.addEventListener("click", function () { stop(); show(at + 1); });
  });
  if (autoBtn) {
    autoBtn.addEventListener("click", function () {
      if (auto) { stop(); return; }
      auto = true;
      autoBtn.setAttribute("aria-pressed", "true");
      tick();
    });
  }
  document.addEventListener("keydown", function (event) {
    if (event.key === "ArrowRight" || event.key === " ") { stop(); show(at + 1); }
    if (event.key === "ArrowLeft") { stop(); show(at - 1); }
    if (event.key === "Escape") { window.location.href = "/runs"; }
  });
  /* swipe, because a carousel that cannot be swiped is a slideshow */
  var x0 = null;
  deck.addEventListener("touchstart", function (e) { x0 = e.touches[0].clientX; },
                        { passive: true });
  deck.addEventListener("touchend", function (e) {
    if (x0 === null) { return; }
    var dx = e.changedTouches[0].clientX - x0;
    if (Math.abs(dx) > 46) { stop(); show(at + (dx < 0 ? 1 : -1)); }
    x0 = null;
  }, { passive: true });

  var walkBtn = document.getElementById("tour-walk");
  if (walkBtn) {
    walkBtn.addEventListener("click", function () {
      stop();
      fetch("/tour/walk.json").then(function (r) { return r.json(); })
        .then(function (data) {
          try {
            window.sessionStorage.setItem("customs-walk",
                                          JSON.stringify(data.stops));
          } catch (e) { /* private window: the walk reloads it per page */ }
          if (data.stops && data.stops.length) {
            window.location.href = data.stops[0].path + "?walk=0";
          }
        });
    });
  }

  /* A deep link to one slide: /tour?slide=6 opens on that one. One-based,
     like the counter in the corner, so a link matches what the reader
     sees -- and it is how these slides get captured for the README. */
  var wanted = parseInt(
    new URLSearchParams(window.location.search).get("slide"), 10);
  show(wanted > 0 ? wanted - 1 : 0);
  tick();
})();

/* The walk itself, on every page: ?walk=N says which stop we are at. */
(function () {
  var params = new URLSearchParams(window.location.search);
  if (!params.has("walk")) { return; }
  var index = parseInt(params.get("walk"), 10) || 0;

  var run = function (stops) {
    if (!stops || !stops.length) { return; }
    var stop = stops[Math.max(0, Math.min(stops.length - 1, index))];
    /* `at` may name several hooks, best first: a stop about the guard's
       refusals wants the guard block, and settles for the market header on
       a run where the guard never had to refuse anything. */
    var target = null;
    stop.at.split("|").some(function (hook) {
      target = document.querySelector('[data-tour="' + hook + '"]');
      return !!target;
    });

    var veil = document.createElement("div");
    veil.className = "walk-veil";
    var hole = document.createElement("div");
    hole.className = "walk-hole";
    var bubble = document.createElement("div");
    bubble.className = "walk-bubble";
    var rail = stops.map(function (_s, i) {
      return '<i class="' + (i <= index ? "on" : "") + '"></i>';
    }).join("");
    /* The words go in as text, not as markup -- and not through the
       escapeHtml further up this file, which lives inside another closure:
       reaching for it from here threw a ReferenceError and the whole
       spotlight silently did not paint. */
    bubble.innerHTML =
      '<span class="wb-step">stop ' + (index + 1) + " of " + stops.length +
      '</span><h3></h3><p></p><div class="wb-rail">' + rail + "</div>" +
      '<div class="wb-foot"></div>';
    bubble.querySelector("h3").textContent = stop.title;
    bubble.querySelector("p").textContent = stop.body;
    document.body.appendChild(veil);
    if (target) { document.body.appendChild(hole); }
    document.body.appendChild(bubble);

    var foot = bubble.querySelector(".wb-foot");
    var onwards = document.createElement("button");
    onwards.type = "button";
    onwards.className = "gobtn";
    onwards.innerHTML = '<span class="gobtn-ring"></span>' +
      '<span class="gobtn-face">' +
      (index + 1 < stops.length ? "Next" : "Finish") + "</span>";
    onwards.addEventListener("click", function () {
      if (index + 1 >= stops.length) {
        window.location.href = "/new?tour=done";
        return;
      }
      var to = stops[index + 1];
      window.location.href = to.path + "?walk=" + (index + 1);
    });
    var out = document.createElement("a");
    out.className = "tbtn";
    out.href = window.location.pathname;
    out.textContent = "leave the tour";
    foot.appendChild(onwards);
    foot.appendChild(out);

    var place = function () {
      var pad = 8;
      var box = target ? target.getBoundingClientRect() : null;
      if (box && target) {
        /* Viewport coordinates, and the hole and the bubble are both
           fixed: page coordinates meant adding scrollY to everything and
           landing in the wrong place whenever the scroll had not settled. */
        hole.style.top = (box.top - pad) + "px";
        hole.style.left = (box.left - pad) + "px";
        hole.style.width = (box.width + pad * 2) + "px";
        /* Some hooks are whole lists two thousand pixels tall. A spotlight
           that size lights the page rather than a thing in it, so it stops
           at three quarters of the window and points at the top of it. */
        hole.style.height = Math.min(box.height + pad * 2,
                                     window.innerHeight * 0.74) + "px";
        veil.style.display = "none";        /* the hole's own shadow is the veil */
      }
      var b = bubble.getBoundingClientRect();
      var lit = box ? Math.min(box.height + pad * 2,
                               window.innerHeight * 0.74) : 0;
      var gap = 18;
      var top, left;
      if (box) {
        /* Beside the spotlight when the spotlit thing is narrow enough to
           leave room -- a bubble under a verdict pill covers the headline
           the verdict is about, which is the one thing the reader was sent
           here to look at. Under it, or over it, otherwise. */
        var beside = box.width < window.innerWidth * 0.55;
        var right = window.innerWidth - (box.right + gap) - 12 >= b.width;
        var left_ = box.left - gap - 12 >= b.width;
        if (beside && (right || left_)) {
          left = right ? box.right + gap : box.left - gap - b.width;
          top = box.top + lit / 2 - b.height / 2;
        } else if (box.top + lit + b.height + 30 < window.innerHeight) {
          top = box.top + lit - pad + gap;
          left = Math.min(Math.max(12, box.left),
                          window.innerWidth - b.width - 12);
        } else {
          top = box.top - pad - b.height - gap;
          left = Math.min(Math.max(12, box.left),
                          window.innerWidth - b.width - 12);
        }
      } else {
        top = (window.innerHeight - b.height) / 2;
        left = (window.innerWidth - b.width) / 2;
      }
      /* and it stays on screen whatever the target did */
      bubble.style.top = Math.min(Math.max(top, 12),
                                  window.innerHeight - b.height - 12) + "px";
      bubble.style.left = Math.max(12, left) + "px";
    };

    if (target) {
      var still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      target.scrollIntoView({ behavior: still ? "auto" : "smooth",
                              block: "center" });
      /* after the smooth scroll settles, and again on anything that moves */
      window.setTimeout(place, 420);
    }
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, { passive: true });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { window.location.href = window.location.pathname; }
      if (event.key === "ArrowRight" || event.key === "Enter") { onwards.click(); }
    });
  };

  var cached = null;
  try { cached = window.sessionStorage.getItem("customs-walk"); } catch (e) { cached = null; }
  if (cached) {
    try { run(JSON.parse(cached)); return; } catch (e) { /* refetch below */ }
  }
  fetch("/tour/walk.json").then(function (r) { return r.json(); })
    .then(function (data) {
      try {
        window.sessionStorage.setItem("customs-walk", JSON.stringify(data.stops));
      } catch (e) { /* fine */ }
      run(data.stops);
    });
})();
