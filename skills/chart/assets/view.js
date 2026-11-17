// Everything the page draws is computed HERE, in the browser, from whatever store it is
// handed: the artifact's database when the viewer can reach it, the page's own embedded
// snapshot when it cannot. ONE implementation on purpose — the python side stopped
// building views when the database became the store (2026-09-21), because two copies of
// this logic would drift and the drift would show up as two different charts.
//
// Store in: a list of documents, one per (machine, 7-day window):
//   {m: "<machine>", w: <window reset, unix>, tz: <minutes from UTC>, updated: <unix>,
//    rows: [[ts, acct, fable|null, h5|null, h5_resets_at|null], ...]}
// View out: the shape the renderer consumes — tabs, series segments, ramps, stats,
// tables and the strings around them.
var UsageView = (function () {
  "use strict";

  var GAP = 3600;          // no reading for this long: the line goes dotted rather than guess
  var SLOPE_7D = 8;        // claudette's throttle curve: cap = 100 - 8 * days remaining
  var WEEK = 7 * 86400;
  var BUCKET = 300;        // two machines inside one bucket are one reading
  var KEEP = 5;            // windows offered as tabs (the database keeps every one)
  var NOTE_GAP = GAP;      // a machine's track breaks after the same silence as the line
  var POOL = "Fable";
  var VOL_TITLE = "Each 5-hour window, filling up";
  var ROLL_TITLE = "Which machine was working";
  var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  // ---------------------------------------------------------------- time
  //
  // Labels must read in the zone the readings were taken in, not the one the page is
  // opened in: these are the user's own windows, and a reset at 07:00 must not read as
  // 04:00 because a laptop crossed a border. When the viewer's zone already matches the
  // pinned offset — the normal case — we format with local Date methods, which get DST
  // right; only a viewer in a different zone falls back to the fixed shift, where a DST
  // boundary inside a window can put a day rule an hour off.
  function spell(sec) {
    var h = sec / 3600, m = sec / 60;
    if (h === 1) return "an hour";
    return (h >= 1 && h === Math.round(h)) ? h + " hours" : m + " minutes";
  }

  function clock(tzMin) {
    var localMatches = (new Date().getTimezoneOffset() * -1) === tzMin;
    function parts(t) {
      var d = new Date(t * 1000);
      if (localMatches) {
        return { y: d.getFullYear(), mo: d.getMonth(), d: d.getDate(), dow: d.getDay(),
                 h: d.getHours(), mi: d.getMinutes() };
      }
      d = new Date((t + tzMin * 60) * 1000);
      return { y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(),
               dow: d.getUTCDay(), h: d.getUTCHours(), mi: d.getUTCMinutes() };
    }
    function two(n) { return (n < 10 ? "0" : "") + n; }
    return {
      parts: parts,
      day: function (t) { var p = parts(t); return MON[p.mo] + " " + p.d; },
      dayKey: function (t) { var p = parts(t); return p.y + "-" + two(p.mo + 1) + "-" + two(p.d); },
      dow: function (t) { return DOW[parts(t).dow]; },
      wdate: function (t) { var p = parts(t); return DOW[p.dow] + " " + MON[p.mo] + " " + p.d; },
      hm: function (t) { var p = parts(t); return two(p.h) + ":" + two(p.mi); },
      dt: function (t) { var p = parts(t); return DOW[p.dow] + " " + MON[p.mo] + " " + p.d +
                                ", " + p.h + ":" + two(p.mi); },
      // first midnight strictly after t. Stepping a flat 86400 lands at 23:00 or 01:00
      // across a DST change, so the guess is pulled back onto midnight from whichever
      // side it fell on — the day rules have to agree with the dates in the table.
      midnightAfter: function (t) {
        var p = parts(t);
        var next = t - (p.h * 3600 + p.mi * 60) - (t % 60) + 86400;
        var q = parts(next);
        next += (q.h >= 12 ? (24 - q.h) * 3600 : -q.h * 3600) - q.mi * 60;
        return next <= t ? next + 86400 : next;
      },
      label: function () {
        try {
          var n = new Date().toLocaleTimeString(undefined, { timeZoneName: "short" });
          return localMatches ? (n.split(" ").pop() || "") : "UTC" +
            (tzMin < 0 ? "-" : "+") + Math.abs(tzMin / 60);
        } catch (e) { return ""; }
      }
    };
  }

  // ---------------------------------------------------------------- store
  //
  // Documents in, one merged store out. A document belongs to exactly one machine, so
  // the only merge left is the rare case of two machines rendering inside the same
  // 5-minute bucket — where the old rules still hold: usage only rises inside a window,
  // so the higher reading is the later truth, and the 5-hour band prefers the reading
  // from the newer 5-hour window rather than the larger percentage.
  function mergeDocs(docs) {
    var names = [], byName = {};
    (docs || []).forEach(function (d) {
      if (!d || !d.m || !d.w || !d.rows) return;
      if (byName[d.m] === undefined) {
        byName[d.m] = { id: d.m, tz: d.tz, points: 0, last: 0, first: Infinity, until: 0 };
      }
      if (d.tz !== undefined && d.tz !== null) byName[d.m].tz = d.tz;
      // when this machine last read its whole snapshot folder: everything up to here is
      // COVERED — a stretch with no reading before it is known silence, not a hole
      if (+d.updated > byName[d.m].until) byName[d.m].until = +d.updated;
    });
    names = Object.keys(byName).sort();
    var wins = {}, acts = {};
    (docs || []).forEach(function (d) {
      if (!d || !d.m || !d.w || !d.rows) return;
      var reset = +d.w, src = names.indexOf(d.m), start = reset - WEEK;
      var keyed = wins[reset] || (wins[reset] = {});
      // who was reporting, kept per machine and NOT bucketed: the merged points above
      // keep one reading per 5-minute bucket, so two machines working at the same hour
      // leave one of them out — which is precisely what the per-machine track must show.
      // Keyed by timestamp, so a stray superseded generation cannot double-count.
      var aw = acts[reset] || (acts[reset] = {});
      var mine = aw[d.m] || (aw[d.m] = {});
      d.rows.forEach(function (r) {
        var ts = +r[0], acct = +r[1];
        if (!(ts >= start - 3600 && ts <= reset + 3600)) return;      // not this window
        if (!(acct >= 0 && acct <= 100)) return;
        mine[ts] = 1;
        var who = byName[d.m];
        if (ts < who.first) who.first = ts;
        if (ts > who.until) who.until = ts;
        var fab = (r[2] === null || r[2] === undefined) ? null : +r[2];
        if (fab !== null && !(fab >= 0 && fab <= 100)) fab = null;
        var h5 = (r[3] === null || r[3] === undefined) ? null : +r[3];
        var h5r = (r[4] === null || r[4] === undefined) ? null : +r[4];
        if (h5 !== null && !(h5 >= 0 && h5 <= 100)) { h5 = null; h5r = null; }
        var k = Math.floor(ts / BUCKET), cur = keyed[k];
        if (!cur) { keyed[k] = [ts, acct, fab, src, h5, h5r]; return; }
        if (acct > cur[1]) { cur[0] = ts; cur[1] = acct; cur[3] = src; }
        if (fab !== null && (cur[2] === null || fab > cur[2])) cur[2] = fab;
        if (h5 !== null && h5r && (cur[5] === null || h5r > cur[5] ||
                                   (h5r === cur[5] && h5 > (cur[4] || 0)))) {
          cur[4] = h5; cur[5] = h5r;
        }
      });
    });
    var windows = Object.keys(wins).map(Number).sort(function (a, b) { return a - b; })
      .map(function (reset) {
        var pts = Object.keys(wins[reset]).map(function (k) { return wins[reset][k]; });
        pts.sort(function (a, b) { return a[0] - b[0]; });
        return { reset: reset, points: pts, acts: acts[reset] || {} };
      }).filter(function (w) { return w.points.length; });
    windows.forEach(function (w) {
      w.points.forEach(function (p) {
        var s = byName[names[p[3]]];
        if (s) { s.points++; s.last = Math.max(s.last, p[0]); }
      });
    });
    // Coverage: from a machine's earliest reading to the last time it collected. The
    // status line snapshots every render, so inside that span a machine with no reading
    // was not working — which the roll draws as a line, and leaves bare outside it.
    var cover = {};
    names.forEach(function (n) {
      var b = byName[n];
      if (isFinite(b.first)) cover[n] = [b.first, Math.max(b.until, b.first)];
      else b.first = null;
    });
    windows.forEach(function (w) { w.cover = cover; });
    return { machines: names.map(function (n) { return byName[n]; }), windows: windows };
  }

  // ---------------------------------------------------------------- pieces

  function segments(points) {
    var segs = [], cur = [];
    points.forEach(function (p) {
      if (cur.length && p[0] - cur[cur.length - 1][0] > GAP) { segs.push(cur); cur = []; }
      cur.push([p[0], p[1]]);
    });
    if (cur.length) segs.push(cur);
    return segs;
  }

  // One ramp per 5-hour window observed inside this 7-day window. The short allowance
  // climbs and never falls back inside its own window, so a reading LOWER than the one
  // before it is stale (two machines, one cache older) — the ramp plots the running
  // maximum, while the raw reading stays in the store and in the tooltip. A window
  // nobody observed produces no ramp: absent is not idle.
  function fiveHourRamps(points, span) {
    var groups = {};
    points.forEach(function (p) {
      if (p[4] === null || !p[5]) return;
      (groups[p[5]] || (groups[p[5]] = [])).push([p[0], p[4]]);
    });
    return Object.keys(groups).map(Number).sort(function (a, b) { return a - b; })
      .map(function (h5r) {
        var obs = groups[h5r].sort(function (a, b) { return a[0] - b[0]; });
        var t0 = h5r - 5 * 3600, t1 = h5r;
        if (t1 <= span[0] || t0 >= span[1]) return null;
        var run = 0, climb = obs.map(function (o) { run = Math.max(run, o[1]); return [o[0], run]; });
        return {
          t0: t0, t1: t1, points: climb, peak: run, n: obs.length,
          // did we watch it open? a ramp whose first reading is already high started
          // before anyone was looking, and must not read as a climb from zero
          from_start: (obs[0][0] - t0) <= 900 || obs[0][1] <= 2
        };
      }).filter(Boolean);
  }

  // One track per machine: where that machine was working inside this window. A note
  // spans a run of its readings less than NOTE_GAP apart — a note is PRESENCE (a session
  // open and rendering), never an amount: the two plots above are where the amounts are.
  // A lone reading keeps a zero-length note; the renderer is what gives it a minimum
  // width, because "visible" is a pixel question and this file holds seconds.
  // Alphabetical, the same order as the sources table: a reader tab-hopping looks for
  // one machine, and a lane that moves between tabs makes them hunt for it every time.
  // `known` is the part of the span the machine's logs cover (see mergeDocs): drawn as a
  // line under its notes, so a gap inside it reads as "not working" and a gap outside it
  // as "no data". A machine covered all window long with no note still gets its lane.
  function tracks(acts, span, cover) {
    var names = {};
    Object.keys(acts || {}).forEach(function (n) { names[n] = 1; });
    Object.keys(cover || {}).forEach(function (n) {
      var c = cover[n];
      if (c && c[0] < span[1] && c[1] > span[0]) names[n] = 1;
    });
    return Object.keys(names).map(function (name) {
      var ts = Object.keys((acts || {})[name] || {}).map(Number).filter(function (t) {
        return t >= span[0] - 3600 && t <= span[1] + 3600;
      }).sort(function (a, b) { return a - b; });
      var c = cover && cover[name];
      var known = c ? [Math.max(c[0], span[0]), Math.min(c[1], span[1])] : null;
      if (known && !(known[1] > known[0])) known = null;
      if (!ts.length && !known) return null;
      var notes = [];
      if (ts.length) {
        var cur = [ts[0], ts[0], 1];
        for (var i = 1; i < ts.length; i++) {
          if (ts[i] - cur[1] > NOTE_GAP) { notes.push(cur); cur = [ts[i], ts[i], 1]; }
          else { cur[1] = ts[i]; cur[2]++; }
        }
        notes.push(cur);
      }
      return {
        id: name, notes: notes, n: ts.length, known: known,
        first: ts.length ? ts[0] : null, last: ts.length ? ts[ts.length - 1] : null,
        on: notes.reduce(function (s, nt) { return s + (nt[1] - nt[0]); }, 0)
      };
    }).filter(Boolean).sort(function (a, b) { return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0); });
  }

  function capCurve(span, reset) {
    var out = [];
    for (var t = span[0]; t <= span[1]; t += 3600) {
      out.push([t, Math.round(Math.max(0, Math.min(100, 100 - SLOPE_7D *
        Math.max(0, (reset - t) / 86400))) * 10) / 10]);
    }
    return out;
  }

  function windowView(w, prevW, now, C) {
    var reset = w.reset, span = [reset - WEEK, reset], pts = w.points;
    var acctPts = pts.map(function (p) { return [p[0], p[1]]; });
    var fablePts = pts.filter(function (p) { return p[2] !== null; })
                      .map(function (p) { return [p[0], p[2]]; });

    var rules = [], t = C.midnightAfter(span[0]);
    while (t < span[1]) { rules.push({ t: t, label: C.dow(t) }); t = C.midnightAfter(t); }
    var xticks = [];
    for (var i = 0; i <= 7; i++) {
      var tt = Math.round(span[0] + (span[1] - span[0]) * i / 7);
      xticks.push({ t: tt, label: C.day(tt) });
    }

    var acctNow = acctPts.length ? acctPts[acctPts.length - 1][1] : 0;
    var fableNow = fablePts.length ? fablePts[fablePts.length - 1][1] : null;
    var live = span[0] <= now && now <= span[1];

    // same elapsed point in the window before this one
    var prevVal = null, prevTxt = "no earlier window held";
    if (prevW) {
      var at = live ? now : (acctPts.length ? acctPts[acctPts.length - 1][0] : span[1]);
      var want = (prevW.reset - WEEK) + (at - span[0]);
      var cands = prevW.points.filter(function (p) { return p[0] <= want + 3600; });
      if (cands.length) {
        prevVal = cands[cands.length - 1][1];
        var d = acctNow - prevVal;
        prevTxt = d ? ((d > 0 ? "+" : "") + d + " points vs this one") : "level with this one";
      }
    }

    var ramps = fiveHourRamps(pts, span);
    var lanes = tracks(w.acts, span, w.cover);
    var busyLanes = lanes.filter(function (l) { return l.notes.length; }).length;
    // what the band actually draws at each reading, so the hover dot lands ON the step
    var drawn = {};
    ramps.forEach(function (r) { r.points.forEach(function (p) { drawn[p[0]] = p[1]; }); });
    var busiest = ramps.reduce(function (a, b) { return (!a || b.peak > a.peak) ? b : a; }, null);
    var hoursLeft = (reset - now) / 3600;

    var stats = [
      { key: "acct", label: "Account", value: acctNow + "%",
        sub: live ? "of this window's allowance" : "where the window ended" },
      { key: "fable", label: POOL + " pool",
        value: fableNow === null ? "—" : fableNow + "%", sub: "its own allowance" },
      { key: "acct", label: "Week before, same point",
        value: prevVal === null ? "—" : prevVal + "%", sub: prevTxt }
    ];
    if (live) {
      stats.push({ key: null, label: "Window resets",
                   value: hoursLeft >= 1 ? (Math.round(hoursLeft) + "h") : "<1h",
                   sub: C.dt(reset) });
    } else {
      stats.push({ key: null, label: "Busiest 5h stretch",
                   value: busiest ? (busiest.peak + "%") : "—",
                   sub: busiest ? C.dt(busiest.t0) : "none observed" });
    }

    var days = {}, order = [];
    pts.forEach(function (p) {
      var k = C.dayKey(p[0]), d = days[k];
      if (!d) { d = days[k] = { first: p[0], last: p[0], acct: p[1], fable: p[2], n: 0, opened: p[1] }; order.push(k); }
      d.last = Math.max(d.last, p[0]);
      d.acct = Math.max(d.acct, p[1]);
      d.n++;
      if (p[2] !== null) d.fable = (d.fable === null) ? p[2] : Math.max(d.fable, p[2]);
    });
    var rows = [], prevMax = null;
    order.forEach(function (k) {
      var d = days[k], base = prevMax === null ? d.opened : prevMax;
      prevMax = d.acct;
      rows.push([C.wdate(d.first), C.hm(d.first) + "–" + C.hm(d.last), d.acct + "%",
                 "+" + Math.max(0, d.acct - base),
                 d.fable === null ? null : d.fable + "%", d.n]);
    });

    return {
      key: String(reset),
      // the tab strip is nothing but weeks, so "Week of" was a word repeated five times
      tab: live ? "This Week" : C.day(span[0]),
      span: span, live: live,
      acct: { segments: segments(acctPts), last: acctPts.length ? acctPts[acctPts.length - 1] : null },
      fable: { segments: segments(fablePts), last: fablePts.length ? fablePts[fablePts.length - 1] : null },
      cap: capCurve(span, reset), ramps: ramps, lanes: lanes,
      rules: rules, xticks: xticks,
      index: pts.map(function (p) {
        return [p[0], p[1], p[2], p[3], p[4], drawn[p[0]] === undefined ? null : drawn[p[0]]];
      }),
      stats: stats,
      title: C.day(span[0]) + " → " + C.dt(reset) + "  ·  " + pts.length + " readings",
      volLabel: VOL_TITLE + "  ·  " + ramps.length + " observed",
      rollLabel: ROLL_TITLE + "  ·  " + busyLanes + " machine" +
                 (busyLanes === 1 ? "" : "s") + " reporting" +
                 (lanes.length > busyLanes ? ", " + (lanes.length - busyLanes) + " idle" : ""),
      table: { summary: "Day by day",
               cols: ["Day", "Readings between", "Account", "Gained", POOL, "Readings"],
               rows: rows }
    };
  }

  // ---------------------------------------------------------------- view

  function buildView(store, now, opts) {
    opts = opts || {};
    var wins = store.windows;
    if (!wins.length) return null;
    // the zone of the machine that reported last — the zone the user is working in, which
    // is what every label used to be pinned to when python built the page
    var newest = store.machines.reduce(function (a, b) { return (!a || b.last > a.last) ? b : a; }, null);
    var tzMin = (newest && newest.tz !== undefined && newest.tz !== null)
      ? newest.tz : (new Date().getTimezoneOffset() * -1);
    var C = clock(tzMin);

    var shown = wins.slice(-KEEP);
    var views = shown.map(function (w) {
      var i = wins.indexOf(w);
      return windowView(w, i > 0 ? wins[i - 1] : null, now, C);
    });                   // oldest first: the strip reads left to right like the x-axis,
                          // so the next tab over is the next week (owner, 2026-09-23)

    var total = wins.reduce(function (s, w) { return s + w.points.length; }, 0);
    var machines = store.machines.filter(function (m) { return m.points; });

    return {
      h1: "Usage Chart (as seen by claudette)",
      lede: "The rolling 7-day allowance as a percentage already spent, read off the " +
            "status line on every machine that reports. Underneath, one step per 5-hour " +
            "window: it climbs while that short allowance fills, then holds — it never " +
            "falls. The hairline where the next window opens is the restart from zero.",
      prov: total + " readings from " + machines.length + " machine" +
            (machines.length === 1 ? "" : "s") + "  ·  " + wins.length + " window" +
            (wins.length === 1 ? "" : "s") + " held, " + shown.length + " shown" +
            (opts.source ? "  ·  " + opts.source : "") +
            (C.label() ? "  ·  times in " + C.label() : ""),
      now: now,
      cardTitle: "Percent of the 7-day window consumed",
      volTitle: VOL_TITLE,
      rollTitle: ROLL_TITLE,
      alt: "Line chart of a seven-day allowance window: percent consumed over time for " +
           "the whole account and for the " + POOL + " pool, against the throttle " +
           "allowance curve, with a rising step per five-hour window below it, and under " +
           "that one track per machine marking when it had a session reporting.",
      seriesNames: ["Whole account", POOL + " pool"],
      sourceNames: store.machines.map(function (m) { return m.id; }),
      legend: [{ key: "cap", label: "Throttle Limit" },
               { key: "acct", label: "Account" },
               { key: "fable", label: POOL }],
      views: views,
      note: "<b>A dotted stretch is a gap in the record, not a quiet spell.</b> A reading " +
            "exists only while some machine had a session open and rendering, so after " +
            spell(GAP) + " without one the line goes dotted \u2014 joined so the eye can " +
            "follow it, unfilled because nothing was seen there \u2014 and a 5-hour window nobody " +
            "observed gets no step rather than a flat one. A 5-hour step holds its level from " +
            "the last reading to the reset — that level is what was seen, not a claim about " +
            "the quiet after it. The red dashed curve is the claudette throttle " +
            "(100 &minus; 8 &times; days left): above it, heavy tools get paced. " +
            "<b>The bottom track is one lane per machine</b>, newest first: a bar covers a " +
            "run of that machine's readings less than " + spell(NOTE_GAP) + " apart \u2014 the " +
            "same silence that dots the line above, so one quiet stretch reads the same in " +
            "both \u2014 " +
            "so it says <i>when that machine was working</i> — never how much it spent. " +
            "The lanes are the only place a second machine working the same hour is " +
            "visible: the lines above keep one reading per five minutes. <b>The " +
            "readings live in this artifact's own database</b>, one document per machine and " +
            "window — every machine writes only its own, nothing is ever merged away, and " +
            "the chart shows the " + KEEP + " most recent windows that hold data.",
      sources: { summary: "Which machines contributed",
                 cols: ["Machine", "Readings held", "Last reading"],
                 rows: store.machines.map(function (m) {
                   return [m.id, m.points, m.last ? C.dt(m.last) : "—"];
                 }) }
    };
  }

  return { mergeDocs: mergeDocs, buildView: buildView, KEEP: KEEP, GAP: GAP,
           NOTE_GAP: NOTE_GAP };
})();

if (typeof module !== "undefined" && module.exports) module.exports = UsageView;
