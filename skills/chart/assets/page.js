(function () {
  "use strict";
  var V = null;

  function el(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt !== undefined) n.textContent = txt;
    return n;
  }
  function svgEl(tag, a) {
    var n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in a) if (a[k] !== null && a[k] !== undefined) n.setAttribute(k, a[k]);
    return n;
  }
  function fillTable(id, cols, rows) {
    var hr = document.querySelector("#" + id + " thead tr"), tb = document.querySelector("#" + id + " tbody");
    hr.textContent = ""; tb.textContent = "";
    cols.forEach(function (c) { hr.appendChild(el("th", null, c)); });
    rows.forEach(function (r) {
      var tr = el("tr");
      r.forEach(function (c) { tr.appendChild(el("td", c === null ? "na" : null, c === null ? "—" : String(c))); });
      tb.appendChild(tr);
    });
  }
  function stamp(t) {
    var d = new Date(t * 1000);
    return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric" }) +
           "  " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }

  // ---------- geometry: one x-axis, three stacked plots ----------
  // The two plots above are fixed; the roll under them grows one lane per machine that
  // reported inside the window on screen, so the height is a per-view number and the
  // viewBox is set in render() rather than once here.
  var W = 1000, M = { t: 30, r: 29, b: 30, l: 34 };   // t carries the plot-1 title
  var BAND = 86, SPLIT = 22;                       // volume band height, gap above it
  var LINE_W = 3;                                  // every data line: account, pool, 5-hour steps
  var LANE = 20, ROLLGAP = 30;                     // one machine's track, gap above the roll
  var iw = W - M.l - M.r;
  var ih = 264;                                    // main plot
  var by0 = M.t + ih + SPLIT, by1 = by0 + BAND;    // band top / baseline
  var roll0 = by1 + ROLLGAP;                       // top of the first lane
  var svg = document.getElementById("chart");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  var view = null, t0 = 0, t1 = 1, floorY = by1, laneMid = {};
  function X(t) { return M.l + (t - t0) / (t1 - t0) * iw; }
  function Y(p) { return M.t + (1 - p / 100) * ih; }
  function BY(p) { return by1 - (p / 100) * BAND; }

  var tip = document.getElementById("tip"), shell = document.getElementById("shell");
  var cursor = -1;
  var cross, dotA, dotF, dotV, dotM, hit;

  function render(v) {
    view = v; t0 = v.span[0]; t1 = v.span[1]; cursor = -1;
    svg.textContent = "";
    var lanes = v.lanes || [];
    laneMid = {};
    floorY = lanes.length ? roll0 + lanes.length * LANE : by1;
    svg.setAttribute("viewBox", "0 0 " + W + " " + (floorY + M.b));

    // The legend lives in the top-left of the plot it describes (owner, 2026-09-22):
    // in the card head it was a caption about "some chart", here it is a key to the three
    // lines directly under it. Stacked top to bottom, throttle first — the limit, then the
    // two lines judged against it — and started below the weekday row, which keeps every
    // weekday label instead of dropping the one the legend used to sit on.
    var legKeys = (V.legend || []).map(function (l, i) {
      return { key: l.key, label: l.label, x: M.l + 6, y: M.t + 26 + i * 14 };
    });

    // every plot names itself and carries its own count, in one typeface
    var pt = svgEl("text", { x: M.l, y: M.t - 9, class: "bandlab" });
    pt.textContent = v.title;
    svg.appendChild(pt);

    var stats = document.getElementById("stats");
    stats.textContent = "";
    v.stats.forEach(function (s) {
      var c = el("div", "stat"), k = el("div", "k");
      if (s.key) k.appendChild(el("i", "key " + s.key));
      k.appendChild(document.createTextNode(s.label));
      c.appendChild(k);
      c.appendChild(el("div", "v", s.value));
      c.appendChild(el("div", "s", s.sub));
      stats.appendChild(c);
    });
    document.getElementById("tblsum").textContent = v.table.summary;
    fillTable("tbl", v.table.cols, v.table.rows);

    if (v.live && V.now && V.now < t1) {
      svg.appendChild(svgEl("rect", { x: X(V.now), y: M.t,
                                      width: M.l + iw - X(V.now), height: floorY - M.t,
                                      fill: "var(--future)" }));
    }

    [0, 25, 50, 75, 100].forEach(function (p) {
      svg.appendChild(svgEl("line", { x1: M.l, x2: M.l + iw, y1: Y(p), y2: Y(p),
                                      stroke: "var(--grid)", "stroke-width": 1 }));
      var tx = svgEl("text", { x: M.l - 8, y: Y(p) + 3.5, class: "tick", "text-anchor": "end" });
      tx.textContent = p + "%";
      svg.appendChild(tx);
    });
    // the band carries its own baseline, a 100% guide and a 50% reference — a step's
    // height is unreadable against two lines 86px apart (owner, 2026-09-21)
    [0, 50, 100].forEach(function (p) {
      svg.appendChild(svgEl("line", { x1: M.l, x2: M.l + iw, y1: BY(p), y2: BY(p),
                                      stroke: "var(--grid)", "stroke-width": 1 }));
    });
    var bh = svgEl("text", { x: M.l - 8, y: BY(50) + 3.5, class: "tick", "text-anchor": "end" });
    bh.textContent = "50%";
    svg.appendChild(bh);
    var bl = svgEl("text", { x: M.l - 8, y: BY(100) + 3.5, class: "tick", "text-anchor": "end" });
    bl.textContent = "100%";
    svg.appendChild(bl);
    var bt = svgEl("text", { x: M.l, y: by0 - 7, class: "bandlab" });
    bt.textContent = v.volLabel;
    svg.appendChild(bt);

    (v.rules || []).forEach(function (r) {
      if (r.t <= t0 || r.t > t1) return;
      // the day rule crosses BOTH plots: they share an x-axis, and a Tuesday that stops
      // at the main plot's floor makes the band below it read as a separate picture
      svg.appendChild(svgEl("line", { x1: X(r.t), x2: X(r.t), y1: M.t, y2: M.t + ih,
                                      stroke: "var(--line)", "stroke-width": 1 }));
      svg.appendChild(svgEl("line", { x1: X(r.t), x2: X(r.t), y1: by0, y2: by1,
                                      stroke: "var(--line)", "stroke-width": 1 }));
      if (floorY > by1) {
        svg.appendChild(svgEl("line", { x1: X(r.t), x2: X(r.t), y1: roll0, y2: floorY,
                                        stroke: "var(--line)", "stroke-width": 1 }));
      }
      var tx = svgEl("text", { x: X(r.t) + 4, y: M.t + 10, class: "marklab" });
      tx.textContent = r.label;
      svg.appendChild(tx);
    });
    v.xticks.forEach(function (tk) {
      var tx = svgEl("text", { x: X(tk.t), y: floorY + 16, class: "tick", "text-anchor": "middle" });
      tx.textContent = tk.label;
      svg.appendChild(tx);
    });

    // 5-hour windows: the climb itself, one step per window, with a hairline where each
    // window opened. Drawn before the main series so the lines sit on top.
    (v.ramps || []).forEach(function (r) {
      var live = v.live && r.t1 >= (V.now || 0);
      var color = live ? "var(--vol-live)" : "var(--vol)";

      // where this window opened — the boundary, drawn even when nothing watched it
      // open. It carries the restart now that no step falls back to zero, so it gets a
      // green of its own: same family as the steps it separates, unmistakably not one.
      // It stops AT the band's 100% line: the 6px it used to poke above that was meant
      // as a tick and read as a value over the ceiling, which is the one thing a
      // percentage chart must never draw (owner, 2026-09-21).
      // Two channels, not one: a vivid green AND a dash pattern. Hue alone was not
      // enough — a dark green line against pale green fills reads as part of the band,
      // which is the one thing this line must never be mistaken for.
      svg.appendChild(svgEl("line", {
        x1: X(r.t0), x2: X(r.t0), y1: BY(100), y2: by1,
        stroke: "var(--vol-mark)", "stroke-width": 1.5,
        "stroke-dasharray": "3 2", opacity: 0.95
      }));

      var pts = r.points;
      if (!pts.length) return;
      var first = pts[0], last = pts[pts.length - 1];
      // the line never comes back down: it stops climbing and holds what the window
      // holds until the reset, and the NEXT window's hairline is the restart from
      // zero. A drawn drop invited reading the fall itself as usage.
      var closed = !live || r.t1 <= (V.now || 0);
      var xEnd = closed ? X(r.t1) : X(last[0]);

      // A window we did NOT watch open gets no vertical wall from zero under its first
      // reading — that stroke drew a climb nobody saw (owner, 2026-09-22). The opening
      // is joined to the first reading by a dotted thread instead, same language as a
      // gap in the plot above, and the fill starts only where the record does.
      var parts = [];
      if (r.from_start) parts.push([X(r.t0), BY(0)]);
      pts.forEach(function (p) { parts.push([X(p[0]), BY(p[1])]); });
      if (closed) parts.push([xEnd, BY(last[1])]);
      var d = parts.map(function (q, i) {
        return (i ? "L" : "M") + q[0].toFixed(2) + " " + q[1].toFixed(2);
      }).join("");
      var x0 = parts[0][0];
      if (!r.from_start) {
        svg.appendChild(svgEl("path", {
          d: "M" + X(r.t0).toFixed(2) + " " + BY(0).toFixed(2) +
             "L" + X(first[0]).toFixed(2) + " " + BY(first[1]).toFixed(2),
          fill: "none", stroke: color, "stroke-width": 1.2,
          "stroke-dasharray": "1.5 3", "stroke-linecap": "round", "stroke-opacity": 0.55
        }));
      }

      svg.appendChild(svgEl("path", {
        d: d + "L" + xEnd.toFixed(2) + " " + BY(0).toFixed(2) +
           "L" + x0.toFixed(2) + " " + BY(0).toFixed(2) + "Z",
        fill: color, "fill-opacity": live ? 0.18 : 0.13, stroke: "none"
      }));
      svg.appendChild(svgEl("path", {
        d: d, fill: "none", stroke: color, "stroke-width": LINE_W,   // same as the lines above
        "stroke-linejoin": "round", "stroke-linecap": "round"
      }));
    });

    // label only the tallest step — a number on every one would be unreadable
    if ((v.ramps || []).length) {
      var top = v.ramps.reduce(function (a, b) { return b.peak > a.peak ? b : a; });
      var tp = top.points[top.points.length - 1];
      top.points.forEach(function (p) { if (p[1] === top.peak) { tp = p; } });
      var lx = X(tp[0]), ly = BY(top.peak);
      var anchor = lx > M.l + iw - 60 ? "end" : "start";
      var lab = svgEl("text", { x: lx + (anchor === "end" ? -5 : 5), y: ly - 4,
                                class: "endlab", fill: "var(--text-1)",
                                "text-anchor": anchor });
      lab.textContent = top.peak + "%";
      svg.appendChild(lab);
    }

    // ---- the roll: one lane per machine, a note wherever it was working ----
    if (lanes.length) {
      var rt = svgEl("text", { x: M.l, y: roll0 - 9, class: "bandlab" });
      rt.textContent = v.rollLabel;
      svg.appendChild(rt);
    }
    lanes.forEach(function (l, i) {
      var top = roll0 + i * LANE, ny = top + 10, mid = ny + 3;
      laneMid[l.id] = mid;
      // the lane's own rule, full width: a machine that was away still HAS a lane, and
      // without the rule its silence reads as the lane not being there at all
      svg.appendChild(svgEl("line", { x1: M.l, x2: M.l + iw, y1: mid, y2: mid,
                                      stroke: "var(--grid)", "stroke-width": 1 }));
      // where the machine's logs cover the window, a thin line in the notes' own colour:
      // a gap on it is known silence, a gap off it is no data (owner, 2026-09-23)
      if (l.known) {
        svg.appendChild(svgEl("line", { x1: X(l.known[0]).toFixed(2), x2: X(l.known[1]).toFixed(2),
                                        y1: mid, y2: mid, stroke: "var(--roll)",
                                        "stroke-width": 1.25, "stroke-opacity": 0.85 }));
      }
      // the name sits at the rightmost end of what the lane holds — the last note or the
      // end of its coverage line, whichever is later (owner, 2026-09-23)
      var endX = l.known ? X(l.known[1]) : M.l;
      l.notes.forEach(function (nt) {
        // a lone reading is a zero-length note and still has to be visible: the minimum
        // is a pixel question, settled here and never in the view builder
        var x = X(nt[0]), w = Math.max(2.5, X(nt[1]) - x);
        if (x + w > endX) endX = x + w;
        svg.appendChild(svgEl("rect", { x: x.toFixed(2), y: ny, width: w.toFixed(2),
                                        height: 6, rx: 1.5, fill: "var(--roll)",
                                        "fill-opacity": 0.85 }));
      });
      // right-aligned on that end, so the eye drops from the name onto where the lane's
      // data stops; with no room to its left it starts there instead
      var est = String(l.id).length * 5.7;
      var lab = svgEl("text", { x: endX.toFixed(2), y: top + 8, class: "rolllab",
                                "text-anchor": (endX - est >= M.l) ? "end" : "start" });
      lab.textContent = l.id;
      svg.appendChild(lab);
      // a lane whose data runs three label-widths or more also names itself where that
      // data starts, left-aligned, so it reads from either end (owner, 2026-09-23). At
      // three widths the two copies keep one width of air between them.
      var startX = l.known ? X(l.known[0]) : Infinity;
      l.notes.forEach(function (nt) { startX = Math.min(startX, X(nt[0])); });
      startX = Math.max(startX, M.l);
      if (isFinite(startX) && endX - startX >= 3 * est) {
        var lab2 = svgEl("text", { x: startX.toFixed(2), y: top + 8, class: "rolllab",
                                   "text-anchor": "start", "aria-hidden": "true" });
        lab2.textContent = l.id;
        svg.appendChild(lab2);
      }
    });

    if (v.cap && v.cap.length > 1) {
      var cd = "";
      v.cap.forEach(function (pt, i) { cd += (i ? "L" : "M") + X(pt[0]).toFixed(2) + " " + Y(pt[1]).toFixed(2); });
      svg.appendChild(svgEl("path", { d: cd, fill: "none", stroke: "var(--cap)",
                                      "stroke-width": 1.5, "stroke-dasharray": "5 4",
                                      "stroke-opacity": 0.9 }));
    }

    function draw(segs, color, wash) {
      // a gap gets a dotted thread and NO fill (owner, 2026-09-21): the eye follows one
      // series instead of reading an ending, while the missing stretch stays visibly
      // missing — nothing is claimed about a level nobody recorded. Under the solid
      // lines, so a real reading always wins the pixel.
      for (var g = 1; g < segs.length; g++) {
        var a = segs[g - 1][segs[g - 1].length - 1], b = segs[g][0];
        svg.appendChild(svgEl("path", {
          d: "M" + X(a[0]).toFixed(2) + " " + Y(a[1]).toFixed(2) +
             "L" + X(b[0]).toFixed(2) + " " + Y(b[1]).toFixed(2),
          fill: "none", stroke: color, "stroke-width": 1.2,
          "stroke-dasharray": "1.5 3", "stroke-linecap": "round", "stroke-opacity": 0.55
        }));
      }
      segs.forEach(function (seg) {
        // ONE thickness for every piece of the series, 3 px (owner, 2026-09-23): a lone
        // reading is a dot exactly as wide as the line, never a bigger marker — a bigger
        // one read as a different kind of point, and it is not one
        if (seg.length === 1) {
          svg.appendChild(svgEl("circle", { cx: X(seg[0][0]), cy: Y(seg[0][1]), r: LINE_W / 2, fill: color }));
          return;
        }
        var d = "";
        seg.forEach(function (pt, i) { d += (i ? "L" : "M") + X(pt[0]).toFixed(2) + " " + Y(pt[1]).toFixed(2); });
        svg.appendChild(svgEl("path", {
          d: d + "L" + X(seg[seg.length - 1][0]).toFixed(2) + " " + Y(0).toFixed(2) +
             "L" + X(seg[0][0]).toFixed(2) + " " + Y(0).toFixed(2) + "Z",
          fill: wash, stroke: "none" }));
        svg.appendChild(svgEl("path", { d: d, fill: "none", stroke: color, "stroke-width": LINE_W,
                                        "stroke-linejoin": "round", "stroke-linecap": "round" }));
      });
    }
    draw(v.acct.segments, "var(--acct)", "var(--acct-wash)");
    draw(v.fable.segments, "var(--fable)", "var(--fable-wash)");

    // The two end numbers. They sit beside their own dot, EXCEPT when the account and
    // the pool finish within a label's height of each other (Sep 12: 88% and 87%, one
    // printed over the other): then each is pushed half the shortfall, the higher up and
    // the lower down, and a hairline in the series colour keeps the number attached to
    // the dot it belongs to. Only the labels move; the dots stay on their readings.
    var ends = [[v.acct, "var(--acct)"], [v.fable, "var(--fable)"]]
      .filter(function (s) { return s[0].last; })
      .map(function (s) {
        return { color: s[1], x: X(s[0].last[0]), y: Y(s[0].last[1]),
                 ly: Y(s[0].last[1]), txt: s[0].last[1] + "%" };
      });
    if (ends.length === 2) {
      var MINSEP = 13, d = ends[1].y - ends[0].y;
      if (Math.abs(d) < MINSEP) {
        var push = (MINSEP - Math.abs(d)) / 2, up = d >= 0 ? 0 : 1;
        ends[up].ly = ends[up].y - push;
        ends[1 - up].ly = ends[1 - up].y + push;
      }
      // never off the top or bottom of the plot, whatever the push asked for
      ends.forEach(function (e) {
        e.ly = Math.max(M.t + 4, Math.min(M.t + ih - 2, e.ly));
      });
    }
    ends.forEach(function (e) {
      svg.appendChild(svgEl("circle", { cx: e.x, cy: e.y, r: 4.5, fill: e.color,
                                        stroke: "var(--surface-1)", "stroke-width": 2 }));
      if (Math.abs(e.ly - e.y) > 0.5) {
        var lead = (e.x + 9 + e.txt.length * 7 > W) ? -1 : 1;
        svg.appendChild(svgEl("line", { x1: e.x + 4 * lead, y1: e.y, x2: e.x + 8 * lead, y2: e.ly,
                                        stroke: e.color, "stroke-width": 1,
                                        "stroke-opacity": 0.7 }));
      }
      // ~7px per character at this size: past the right edge the number reads inward
      var out = e.x + 9 + e.txt.length * 7 > W;
      var tx = svgEl("text", { x: e.x + (out ? -9 : 9), y: e.ly + 4, class: "endlab",
                               "text-anchor": out ? "end" : "start", fill: "var(--text-1)" });
      tx.textContent = e.txt;
      svg.appendChild(tx);
    });

    legKeys.forEach(function (k) {
      var col = k.key === "acct" ? "var(--acct)" : k.key === "fable" ? "var(--fable)" : "var(--cap)";
      svg.appendChild(svgEl("line", {
        x1: k.x, x2: k.x + 12, y1: k.y - 3.5, y2: k.y - 3.5, stroke: col,
        "stroke-width": 2, "stroke-dasharray": k.key === "cap" ? "4 3" : null
      }));
      var lt = svgEl("text", { x: k.x + 16, y: k.y, class: "leglab" });
      lt.textContent = k.label;
      svg.appendChild(lt);
    });

    // hover layer, rebuilt with the view so it always sits on top
    cross = svgEl("line", { x1: 0, x2: 0, y1: M.t, y2: floorY, stroke: "var(--text-3)", "stroke-width": 1, opacity: 0 });
    dotA = svgEl("circle", { r: 4.5, fill: "var(--acct)", stroke: "var(--surface-1)", "stroke-width": 2, opacity: 0 });
    dotF = svgEl("circle", { r: 4.5, fill: "var(--fable)", stroke: "var(--surface-1)", "stroke-width": 2, opacity: 0 });
    // the band gets its own dot: the crosshair crosses it too, and the 5h number in
    // the tooltip had nothing to point at down there
    dotV = svgEl("circle", { r: 4, fill: "var(--vol)", stroke: "var(--surface-1)", "stroke-width": 2, opacity: 0 });
    // the roll gets a mark of its own: it lands in the lane of the machine the tooltip
    // names, which is the whole question the roll answers — who, not how much
    dotM = svgEl("circle", { r: 3.5, fill: "var(--text-1)", stroke: "var(--surface-1)",
                             "stroke-width": 1.5, opacity: 0 });
    svg.appendChild(cross); svg.appendChild(dotA); svg.appendChild(dotF);
    svg.appendChild(dotV); svg.appendChild(dotM);
    hit = svgEl("rect", { x: M.l, y: M.t, width: iw, height: floorY - M.t, fill: "transparent",
                          style: "cursor:crosshair", tabindex: "0" });
    svg.appendChild(hit);
    hit.addEventListener("pointermove", fromEvent);
    hit.addEventListener("pointerdown", fromEvent);
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("blur", hide);
    hit.addEventListener("focus", function () { show(cursor < 0 ? view.index.length - 1 : cursor); });
    hit.addEventListener("keydown", keyNav);
    hide();
  }

  function nearest(t) {
    var idx = view.index, lo = 0, hi = idx.length - 1;
    while (lo < hi) { var m = (lo + hi) >> 1; if (idx[m][0] < t) lo = m + 1; else hi = m; }
    if (lo > 0 && Math.abs(idx[lo - 1][0] - t) < Math.abs(idx[lo][0] - t)) lo--;
    return lo;
  }
  function show(i, clientX) {
    var idx = view.index;
    if (i < 0 || i >= idx.length) return;
    cursor = i;
    var row = idx[i], x = X(row[0]);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("opacity", 1);
    [[dotA, row[1]], [dotF, row[2]]].forEach(function (p) {
      if (p[1] === null) { p[0].setAttribute("opacity", 0); return; }
      p[0].setAttribute("cx", x); p[0].setAttribute("cy", Y(p[1])); p[0].setAttribute("opacity", 1);
    });
    tip.textContent = "";
    tip.appendChild(el("div", "when", stamp(row[0])));
    [["acct", V.seriesNames[0], row[1]], ["fable", V.seriesNames[1], row[2]]].forEach(function (s) {
      var r = el("div", "row");
      r.appendChild(el("i", "key " + s[0]));
      r.appendChild(el("span", "n", s[2] === null ? "—" : s[2] + "%"));
      r.appendChild(el("span", "l", s[1]));
      tip.appendChild(r);
    });
    // row[5] is what the band draws (the running maximum), row[4] the raw reading;
    // the dot follows the drawn step, and the tooltip names the raw one when a stale
    // machine reported lower than what had already been seen
    var h5 = (row[5] === null || row[5] === undefined) ? row[4] : row[5];
    if (h5 !== null && h5 !== undefined) {
      dotV.setAttribute("cx", x); dotV.setAttribute("cy", BY(h5)); dotV.setAttribute("opacity", 1);
      var r5 = el("div", "row");
      r5.appendChild(el("i", "key vol"));
      r5.appendChild(el("span", "n", h5 + "%"));
      r5.appendChild(el("span", "l", "of the 5h window" +
        (row[4] !== null && row[4] !== undefined && row[4] < h5 ? " (stale read " + row[4] + "%)" : "")));
      tip.appendChild(r5);
    } else {
      dotV.setAttribute("opacity", 0);
    }
    var who = (row[3] === undefined) ? null : V.sourceNames[row[3]];
    if (who) {
      var sr = el("div", "row");
      sr.appendChild(el("span", "l", "seen by " + who));
      tip.appendChild(sr);
    }
    if (who && laneMid[who] !== undefined) {
      dotM.setAttribute("cx", x); dotM.setAttribute("cy", laneMid[who]);
      dotM.setAttribute("opacity", 1);
    } else {
      dotM.setAttribute("opacity", 0);
    }
    tip.classList.add("on");
    var rect = shell.getBoundingClientRect();
    var px = X(row[0]) / W * rect.width;          // the crosshair, not the pointer: the
                                                  // card tracks the reading it describes
    var tw = tip.offsetWidth || 150, th = tip.offsetHeight || 100;
    // Beside the bar, on the roomier side: right of it in the left half of the plot, left
    // of it in the right half. The card then never crosses the bar every dot sits on, so
    // occlusion stops being a thing to solve and the vertical position can stay put.
    var gap = 14;
    var rightOf = px + gap, leftOf = px - gap - tw;
    var left = (px < rect.width / 2) ? rightOf : leftOf;
    if (left < 0 || left + tw > rect.width) {     // no room that side: take the other one
      left = (left === rightOf) ? leftOf : rightOf;
    }
    tip.style.left = Math.max(0, Math.min(Math.max(0, rect.width - tw), left)) + "px";
    // never park the card on the line it is describing: parked at the top it swallowed
    // the account line from ~60% up. When the account point sits inside the card's own
    // strip, drop the card just below that point instead — and never past the top of
    // the 5-hour band, which now carries a dot of its own.
    // Constant vertically. The scored version that dodged each dot vertically worked and
    // was still wrong to use: it hopped from top to bottom as the pointer panned, and the
    // eye follows the hop instead of the line. Sitting beside the bar makes the dodge
    // unnecessary, so the card stays in the top strip and only ever changes sides.
    tip.style.top = "6px";
  }
  function hide() {
    if (!cross) return;
    cross.setAttribute("opacity", 0);
    dotA.setAttribute("opacity", 0); dotF.setAttribute("opacity", 0);
    dotV.setAttribute("opacity", 0); dotM.setAttribute("opacity", 0);
    tip.classList.remove("on");
  }
  function fromEvent(ev) {
    var rect = shell.getBoundingClientRect();
    var rel = (ev.clientX - rect.left) / rect.width * W;
    show(nearest(t0 + (rel - M.l) / iw * (t1 - t0)), ev.clientX);
  }
  function keyNav(ev) {
    var n = view.index.length, step = ev.shiftKey ? 20 : 1, next = null;
    if (ev.key === "ArrowRight") next = Math.min(n - 1, (cursor < 0 ? 0 : cursor) + step);
    if (ev.key === "ArrowLeft") next = Math.max(0, (cursor < 0 ? n - 1 : cursor) - step);
    if (ev.key === "Home") next = 0;
    if (ev.key === "End") next = n - 1;
    if (next === null) return;
    ev.preventDefault();
    show(next);
  }

  // ---------- applying a view (snapshot first, then whatever the database says) ----------
  // The tab a reader picks is kept for this visit only: a live refresh from another
  // machine must not move them, but every fresh open lands on This Week — remembered
  // across opens, yesterday's tab hid the current week (owner, 2026-09-23).
  var tabsBox = document.getElementById("tabs"), buttons = [], chosen = null;
  function select(i) {
    buttons.forEach(function (b, j) { b.setAttribute("aria-selected", j === i ? "true" : "false"); });
    render(V.views[i]);
    chosen = V.views[i].key;
  }
  function apply(nextV) {
    if (!nextV) return;
    V = nextV;
    document.getElementById("h1").textContent = V.h1;
    document.getElementById("lede").textContent = V.lede;
    document.getElementById("prov").textContent = V.prov;
    document.getElementById("note").innerHTML = V.note;
    document.getElementById("cardtitle").textContent = V.cardTitle;
    document.getElementById("chart").setAttribute("aria-label", V.alt);
    document.getElementById("srcsum").textContent = V.sources.summary;
    fillTable("srctbl", V.sources.cols, V.sources.rows);

    // the chosen tab is kept by WINDOW, not by position, so a rebuild that adds a window
    // (or a live refresh from another machine) keeps the reader where they were
    var want = chosen;
    tabsBox.textContent = ""; buttons = [];
    V.views.forEach(function (v, i) {
      var b = el("button", null, v.tab);
      b.setAttribute("role", "tab");
      b.addEventListener("click", function () { select(i); });
      tabsBox.appendChild(b);
      buttons.push(b);
    });
    tabsBox.hidden = V.views.length < 2;
    // nothing picked yet: This Week, the live window, which is the last tab; with no
    // live window, the newest one — also the last tab
    var start = V.views.length - 1;
    V.views.forEach(function (v, i) { if (v.live && want === null) start = i; });
    V.views.forEach(function (v, i) { if (v.key === want) start = i; });
    select(start);
  }

  // ---------- light / dark, the viewer's own call ----------
  var ICON = {
    light: '<svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true"><circle cx="10" cy="10" r="3.4"/><path d="M10 2.2v1.9M10 15.9v1.9M2.2 10h1.9M15.9 10h1.9M4.5 4.5l1.35 1.35M14.15 14.15l1.35 1.35M15.5 4.5l-1.35 1.35M5.85 14.15L4.5 15.5"/></svg>',
    dark: '<svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true"><path d="M15.4 11.7A6.2 6.2 0 0 1 7.6 3.9a6.4 6.4 0 1 0 7.8 7.8Z"/></svg>'
  };
  var themeBtn = document.getElementById("theme");
  function rootEl() {
    var r = document.documentElement;
    return (r && r.setAttribute) ? r : null;
  }
  function effectiveTheme() {
    var r = rootEl(), set = r && r.getAttribute ? r.getAttribute("data-theme") : null;
    if (set === "dark" || set === "light") return set;
    try {
      return (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches)
        ? "dark" : "light";
    } catch (e) { return "light"; }
  }
  function paintTheme() {
    var next = effectiveTheme() === "dark" ? "light" : "dark";
    themeBtn.innerHTML = ICON[next];                 // the icon is where you are GOING
    themeBtn.setAttribute("aria-label", "Switch to " + next + " mode");
    themeBtn.setAttribute("title", "Switch to " + next + " mode");
  }
  if (themeBtn) {
    var saved = null;
    try { saved = localStorage.getItem("usage-theme"); } catch (e) {}
    if ((saved === "dark" || saved === "light") && rootEl()) {
      rootEl().setAttribute("data-theme", saved);
    }
    paintTheme();
    themeBtn.addEventListener("click", function () {
      var next = effectiveTheme() === "dark" ? "light" : "dark";
      if (rootEl()) rootEl().setAttribute("data-theme", next);
      try { localStorage.setItem("usage-theme", next); } catch (e) {}
      paintTheme();
    });
  }

  function viewOf(docs, source) {
    return UsageView.buildView(UsageView.mergeDocs(docs), Math.floor(Date.now() / 1000),
                               { source: source });
  }

  // Two sources, unioned (skills/chart/DESIGN.md, "The store is published files"):
  //  - one published FILE per machine, data/<stem>.json, the stems listed in
  //    data/index.json. Each machine rewrites only its own file, so none collide.
  //  - this artifact's database: the archive of the flow before it, and whatever a machine
  //    still on that flow keeps sending. Read-only from here; nothing writes it any more.
  // Either may be missing. The page draws whatever arrived and says which.
  var files = {}, archive = [], asked = 0, answered = 0, indexed = false;
  function valid(d) { return d && d.m && d.w && Array.isArray(d.rows); }
  function redraw() {
    var names = Object.keys(files), docs = archive.slice(), src = [];
    names.forEach(function (m) { docs = docs.concat(files[m]); });
    if (!docs.length) return;
    if (names.length) src.push(names.length + " machine file" + (names.length > 1 ? "s" : ""));
    if (archive.length) src.push(archive.length + " archived document" + (archive.length > 1 ? "s" : ""));
    apply(viewOf(docs, src.join(" + ")));
  }
  function settle() {
    // the index and every file it named have answered, and nothing drew: say so rather
    // than leave "Loading" up
    if (indexed && answered >= asked && !V) {
      document.getElementById("prov").textContent = "No readings could be loaded yet.";
    }
  }
  function getJson(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; });
  }
  function load() {
    getJson("data/index.json").then(function (idx) {
      ((idx && Array.isArray(idx.machines)) ? idx.machines : []).forEach(function (stem) {
        if (!/^[A-Za-z0-9_.-]+$/.test(stem)) return;        // never a path we did not mint
        asked++;
        getJson("data/" + stem + ".json").then(function (j) {
          if (j && Array.isArray(j.docs)) {
            // the file's build time is each document's: it is when the machine last
            // read its whole snapshot folder, which is what the roll's coverage line needs
            files[stem] = j.docs.filter(valid).map(function (d) {
              return d.updated ? d : Object.assign({ updated: j.updated }, d);
            });
            redraw();
          }
        }).catch(function () {}).then(function () { answered++; settle(); });
      });
    }).catch(function () {}).then(function () { indexed = true; settle(); });
  }
  load();
  // a tab left open picks up other machines' pushes when the reader comes back to it
  if (document.addEventListener) {
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") load();
    });
  }

  if (window.claude && window.claude.use) {
    window.claude.use("db").then(function (db) {
      if (!db) return;
      db.collection("readings").onSnapshot(function (snap) {
        archive = snap.docs.map(function (d) { return d.data(); }).filter(valid);
        redraw();
      }, function () { /* a dead subscription leaves the last good view on screen */ });
    }).catch(function () {});
  }
})();
