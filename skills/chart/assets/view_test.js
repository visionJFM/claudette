// Headless check of the view builder — the page's whole brain runs here, on the real
// documents, so a broken renderer is caught before it is published (there is no other
// way to see it: the chart only renders inside claude.ai).
//
//     node <skill>/assets/view_test.js <dir>
//
// <dir> holds machine files ({"docs": [...]}, the staged `data/`) or single documents
// (the database archive as ArtifactData's out_dir saved it) — both, unioned, as the page does.
//
// Asserts the invariants that matter, prints the view's own summary lines, exits 1 on
// the first failure.
var fs = require("fs"), path = require("path");
var UsageView = require(path.join(__dirname, "view.js"));

var dir = process.argv[2];
if (!dir) { console.error("usage: node view_test.js <dir>"); process.exit(2); }

var docs = [];
fs.readdirSync(dir).filter(function (f) { return /\.json$/.test(f); }).forEach(function (f) {
  var j = JSON.parse(fs.readFileSync(path.join(dir, f), "utf8"));
  // data/index.json names machines and holds no readings: not a document
  docs = docs.concat(Array.isArray(j.docs) ? j.docs : Array.isArray(j.rows) ? [j] : []);
});

var fails = [];
function ok(cond, what) { if (!cond) fails.push(what); }

var store = UsageView.mergeDocs(docs);
var rowsIn = docs.reduce(function (s, d) { return s + d.rows.length; }, 0);
var ptsOut = store.windows.reduce(function (s, w) { return s + w.points.length; }, 0);
console.log("documents %d  rows in %d  merged points %d  machines %s  windows %d",
  docs.length, rowsIn, ptsOut, store.machines.length, store.windows.length);

ok(store.windows.length > 0, "no windows merged");
ok(store.machines.length === new Set(docs.map(function (d) { return d.m; })).size,
   "machine list does not match the documents");
ok(ptsOut <= rowsIn, "merge invented points");
ok(store.machines.reduce(function (s, m) { return s + m.points; }, 0) === ptsOut,
   "per-machine counts do not sum to the merged points");
store.windows.forEach(function (w) {
  var ts = w.points.map(function (p) { return p[0]; });
  ok(ts.every(function (t, i) { return !i || t >= ts[i - 1]; }), "window " + w.reset + " not sorted");
  ok(w.points.every(function (p) { return p[0] > w.reset - 7 * 86400 - 3601 && p[0] < w.reset + 3601; }),
     "window " + w.reset + " holds a point outside its own span");
  ok(w.points.every(function (p) { return p[3] >= 0 && p[3] < store.machines.length; }),
     "window " + w.reset + " holds an unknown machine index");
});

var now = Math.floor(Date.now() / 1000);
var V = UsageView.buildView(store, now, { source: "test" });
ok(V, "buildView returned nothing");
if (V) {
  console.log("prov: %s", V.prov);
  ok(V.legend.map(function (l) { return l.key; }).join(",") === "cap,acct,fable",
     "the legend is not throttle, account, pool: " +
     V.legend.map(function (l) { return l.key; }).join(","));
  ok(V.views.length === Math.min(UsageView.KEEP, store.windows.length),
     "expected " + Math.min(UsageView.KEEP, store.windows.length) + " tabs, got " + V.views.length);
  var keys = V.views.map(function (v) { return +v.key; });
  ok(keys.every(function (k, i) { return !i || k > keys[i - 1]; }), "tabs are not oldest-first");
  ok(V.views.every(function (v, i) { return !v.live || i === V.views.length - 1; }),
     "the live window is not the last tab");
  ok(V.views.filter(function (v) { return v.live; }).length <= 1, "more than one live window");
  V.views.forEach(function (v) {
    console.log("   %s | %s | %s | %s | lanes: %s", v.tab, v.title, v.volLabel, v.rollLabel,
      v.lanes.map(function (l) { return l.id + "(" + l.notes.length + ")"; }).join(" "));
    ok(v.span[1] - v.span[0] === 7 * 86400, v.tab + ": span is not 7 days");
    ok(v.index.length > 0, v.tab + ": empty index");
    ok(v.stats.length === 4, v.tab + ": expected 4 stat tiles");
    ok(v.cap.length > 100 && v.cap[0][1] < v.cap[v.cap.length - 1][1],
       v.tab + ": throttle curve does not rise");
    ok(v.rules.length >= 6 && v.rules.length <= 8, v.tab + ": " + v.rules.length + " day rules");
    ok(v.xticks.length === 8, v.tab + ": expected 8 x ticks");
    // the promise the owner asked for: a 5-hour step never descends
    v.ramps.forEach(function (r) {
      var vals = r.points.map(function (p) { return p[1]; });
      ok(vals.every(function (x, i) { return !i || x >= vals[i - 1]; }),
         v.tab + ": ramp at " + r.t0 + " descends");
      ok(r.peak === Math.max.apply(null, vals), v.tab + ": ramp peak is not its maximum");
      ok(r.points.every(function (p) { return p[0] >= r.t0 - 1 && p[0] <= r.t1 + 3600; }),
         v.tab + ": ramp holds a reading outside its 5-hour window");
    });
    // every segment is a real run of readings, never spanning a gap
    v.acct.segments.concat(v.fable.segments).forEach(function (seg) {
      ok(seg.every(function (p, i) { return !i || p[0] - seg[i - 1][0] <= UsageView.GAP; }),
         v.tab + ": a segment spans a gap");
    });
    // the hover dot must land on the drawn step, not on a stale reading
    var drawn = {};
    v.ramps.forEach(function (r) { r.points.forEach(function (p) { drawn[p[0]] = p[1]; }); });
    ok(v.index.every(function (row) {
      return row[5] === null || row[5] === drawn[row[0]];
    }), v.tab + ": index disagrees with the drawn ramp");
    ok(v.index.every(function (row) { return row[5] === null || row[4] === null || row[5] >= row[4]; }),
       v.tab + ": drawn value below the raw reading");
    ok(v.table.rows.length >= 1 && v.table.rows.every(function (r) { return r.length === 6; }),
       v.tab + ": day table is malformed");
    // the per-machine roll: real machines, real runs, and never fewer readings than the
    // merged line holds — the lanes are what a bucket shared by two machines hides
    var known = store.machines.map(function (m) { return m.id; });
    ok(v.lanes.length >= 1 && v.lanes.length <= known.length,
       v.tab + ": " + v.lanes.length + " lanes against " + known.length + " machines");
    ok(v.lanes.every(function (l) { return known.indexOf(l.id) >= 0; }),
       v.tab + ": a lane names a machine the store does not know");
    ok(v.lanes.every(function (l, i) { return !i || l.id > v.lanes[i - 1].id; }),
       v.tab + ": lanes are not in alphabetical order");
    ok(v.lanes.reduce(function (s2, l) { return s2 + l.n; }, 0) >= v.index.length,
       v.tab + ": the lanes hold fewer readings than the merged line");
    v.lanes.forEach(function (l) {
      ok(l.notes.length > 0 || l.known, v.tab + "/" + l.id + ": a lane with no note and no coverage");
      ok(!l.known || (l.known[0] < l.known[1] && l.known[0] >= v.span[0] && l.known[1] <= v.span[1]),
         v.tab + "/" + l.id + ": coverage outside the window, or empty");
      ok(!l.known || l.notes.every(function (n) {
        return n[0] >= l.known[0] - 3600 && n[1] <= l.known[1] + 3600;
      }), v.tab + "/" + l.id + ": a note outside the machine's own coverage");
      ok(l.notes.every(function (n) { return n[1] >= n[0]; }),
         v.tab + "/" + l.id + ": a note ends before it starts");
      ok(l.notes.every(function (n, i) {
        return !i || n[0] - l.notes[i - 1][1] > UsageView.NOTE_GAP;
      }),
         v.tab + "/" + l.id + ": two notes that should be one");
      ok(l.notes.every(function (n) {
        return n[0] >= v.span[0] - 3600 && n[1] <= v.span[1] + 3600;
      }), v.tab + "/" + l.id + ": a note outside the window");
      ok(l.notes.reduce(function (s2, n) { return s2 + n[2]; }, 0) === l.n,
         v.tab + "/" + l.id + ": the notes do not account for every reading");
      ok(!l.notes.length || l.last === l.notes[l.notes.length - 1][1] && l.first === l.notes[0][0],
         v.tab + "/" + l.id + ": first/last disagree with the notes");
    });
  });
}

if (fails.length) {
  console.error("\nFAIL (%d):", fails.length);
  fails.forEach(function (f) { console.error("  - " + f); });
  process.exit(1);
}
console.log("\nok — %d assertions' worth of invariants hold", 23);
