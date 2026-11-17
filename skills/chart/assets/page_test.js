// Runs the staged page's OWN scripts against a minimal DOM, because the chart renders
// only inside claude.ai and a runtime error there is invisible from here: a typo in the
// renderer would publish silently and show a blank card.
//
//     node <skill>/assets/page_test.js <dir>
//
// <dir> is what `usage_chart.py prepare` bundled: index.html, view.js, page.js,
// data/<stem>.json and (on a joining run) data/index.json. The paths a real viewer
// takes, each exercised through apply(), render() of every tab and the hover card,
// failing on the first thrown error:
//   1. machine files only, no database (a viewer without the capability)
//   2. files plus the database archive, which carries one machine no file has
//   3. an index naming a machine whose file is missing — the others must still draw
//   4. no index at all — the page must say so, not sit on "Loading"
// And the loader itself: small, and no inline code — it is read before every publish.
var fs = require("fs"), path = require("path");

var dir = process.argv[2];
if (!dir) { console.error("usage: node page_test.js <staged dir>"); process.exit(2); }
var html = fs.readFileSync(path.join(dir, "index.html"), "utf8");
if (Buffer.byteLength(html) > 4096) {
  throw new Error("index.html is " + Buffer.byteLength(html) + " bytes: every publish reads it, keep it under 4 KB");
}
if (/<script>/.test(html)) throw new Error("index.html carries inline code: it belongs in page.js");
if (/<style>/.test(html)) throw new Error("index.html carries inline css: it belongs in chart.css");
var INDEX = JSON.parse(html.match(/<script id="chart-index" type="application\/json">([\s\S]*?)<\/script>/)[1]);
if (!(INDEX.renderer >= 1)) throw new Error("chart-index carries no renderer version");
["view.js", "page.js"].forEach(function (f) {
  if (html.indexOf('src="' + f + '"') < 0) throw new Error("index.html does not load " + f);
});
var code = fs.readFileSync(path.join(dir, "view.js"), "utf8") + "\n" +
           fs.readFileSync(path.join(dir, "page.js"), "utf8");
var dataDir = path.join(dir, "data");
var files = {};
(fs.existsSync(dataDir) ? fs.readdirSync(dataDir) : []).forEach(function (f) {
  if (/\.json$/.test(f)) files["data/" + f] = JSON.parse(fs.readFileSync(path.join(dataDir, f), "utf8"));
});
// a bundle from a machine already in the index carries no index.json: stand one up from
// the machine files present, as the live chart would hold it
if (!files["data/index.json"]) {
  files["data/index.json"] = { v: 1, machines: Object.keys(files).map(function (k) {
    return k.replace(/^data\//, "").replace(/\.json$/, ""); }) };
}
var machineFiles = Object.keys(files).filter(function (k) { return k !== "data/index.json"; });
var firstFile = files[machineFiles[0]];
if (!firstFile || !firstFile.docs.length) throw new Error("no machine file with documents in " + dataDir);

function runPage(label, opt) {
  var made = 0, listeners = {};
  function node(tag) {
    var n = {
      tag: tag, children: [], attrs: {}, style: {},
      classList: { add: function () {}, remove: function () {} },
      className: "", innerHTML: "", hidden: false,
      offsetWidth: 160, offsetHeight: 104,
      appendChild: function (c) { n.children.push(c); made++; return c; },
      setAttribute: function (k, v) {
        if (v === undefined || (typeof v === "number" && !isFinite(v))) {
          throw new Error(tag + ": attribute " + k + " set to " + v);
        }
        n.attrs[k] = v;
      },
      getAttribute: function (k) { return n.attrs[k]; },
      addEventListener: function (ev, fn) { (listeners[ev] || (listeners[ev] = [])).push(fn); },
      getBoundingClientRect: function () { return { left: 0, top: 0, width: 1048, height: 440 }; },
      querySelector: function () { return node("stub"); }
    };
    // real textContent empties the element — the renderer clears the stats row, the
    // legend and the tab strip that way before rebuilding them, and a shim that kept
    // the children would hide a leak (it did: tiles piled up tab after tab)
    var text = "";
    Object.defineProperty(n, "textContent", {
      get: function () { return text; },
      set: function (v) { text = String(v); n.children.length = 0; }
    });
    return n;
  }
  var byId = {}, q = {}, fetched = [];
  ["chart", "tip", "shell", "stats", "tblsum", "tbl", "srctbl", "h1", "lede",
   "prov", "note", "cardtitle", "srcsum", "tabs", "chart-index", "theme"].forEach(function (id) {
    byId[id] = node(id);
  });
  byId["chart-index"].textContent = JSON.stringify(INDEX);

  global.window = opt.db ? { claude: { use: function (n) {
    return Promise.resolve(n === "db" ? opt.db : null);
  } } } : {};
  global.fetch = function (url, init) {
    fetched.push(url);
    if (!init || init.cache !== "no-store") throw new Error("fetch of " + url + " may be served stale");
    var body = (opt.serve || files)[url];
    return Promise.resolve(body ? { ok: true, json: function () { return Promise.resolve(body); } }
                                : { ok: false, json: function () { return Promise.reject(new Error("404")); } });
  };
  global.localStorage = {
    store: {}, getItem: function (k) { return this.store[k] || null; },
    setItem: function (k, v) { this.store[k] = v; }
  };
  var rootNode = node("html");
  global.document = {
    documentElement: rootNode,
    visibilityState: "visible",
    addEventListener: function (ev, fn) { (listeners["doc:" + ev] || (listeners["doc:" + ev] = [])).push(fn); },
    getElementById: function (id) {
      if (!byId[id]) throw new Error("page asked for #" + id + " which the markup does not define");
      return byId[id];
    },
    querySelector: function (sel) {
      var m = sel.match(/^#([\w-]+)/);
      if (!m || !byId[m[1]]) throw new Error("page queried " + sel + " which the markup does not define");
      return q[sel] || (q[sel] = node(sel));
    },
    createElement: node,
    createTextNode: function (t) { var n = node("#text"); n.textContent = t; return n; },
    createElementNS: function (_ns, tag) { return node(tag); }
  };
  (new Function(code))();          // one scope, as a browser gives the two <script src> tags
  return { byId: byId, q: q, listeners: listeners, fetched: fetched };
}

function machines(page) {
  return ((page.q["#srctbl tbody"] || {}).children || []).map(function (tr) {
    return tr.children.length ? tr.children[0].textContent : "";
  });
}

function exercise(label, page) {
  var L = page.listeners, byId = page.byId;
  if (!L.pointermove) throw new Error(label + ": no pointermove handler — the hover card is dead");
  [0.02, 0.3, 0.62, 0.95].forEach(function (frac) {
    L.pointermove.forEach(function (fn) { fn({ clientX: 1048 * frac }); });
  });
  L.keydown.forEach(function (fn) {
    ["ArrowRight", "ArrowLeft", "Home", "End"].forEach(function (k) {
      fn({ key: k, preventDefault: function () {} });
    });
  });
  L.pointerleave.forEach(function (fn) { fn({}); });
  var tabs = byId.tabs.children;
  if (!tabs.length) throw new Error(label + ": no tabs were built");
  // before anyone clicks: the page opens on the last tab, This Week (owner, 2026-09-23)
  var picked = tabs.map(function (b) { return b.attrs["aria-selected"]; });
  if (picked[picked.length - 1] !== "true" || picked.indexOf("true") !== picked.length - 1) {
    throw new Error(label + ": the page did not open on the last tab (" + picked.join(",") + ")");
  }
  (L.click || []).forEach(function (fn) { fn(); });          // render every tab
  if (byId.chart.children.length < 50) throw new Error(label + ": the chart drew almost nothing");
  if (byId.stats.children.length !== 4) throw new Error(label + ": expected 4 stat tiles");
  // the heading must be filled in, never a particular wording (a rename once failed this)
  if ((byId.h1.textContent || "").trim().length < 4) throw new Error(label + ": no heading");
  if (!/svg/.test(byId.theme.innerHTML || "")) throw new Error(label + ": the theme button has no icon");
  var dayRows = (page.q["#tbl tbody"] || { children: [] }).children.length;
  if (!dayRows) throw new Error(label + ": the day-by-day table is empty");
  if (!machines(page).length) throw new Error(label + ": the machines table is empty");
  // each lane's name, once at its data's end, and again at its start when the lane is long
  var names = byId.chart.children.filter(function (n) { return n.attrs["class"] === "rolllab"; });
  var copies = names.filter(function (n) { return n.attrs["aria-hidden"] === "true"; });
  var lanes = names.length - copies.length;
  if (!lanes) throw new Error(label + ": no machine names on the roll");
  if (copies.length > lanes) throw new Error(label + ": more name copies than lanes");
  copies.forEach(function (c) {
    if (c.attrs["text-anchor"] !== "start") throw new Error(label + ": a start-of-lane name that is not left-aligned");
  });
  console.log("%s\n   tabs: %s", label, tabs.map(function (b) { return b.textContent; }).join(" | "));
  console.log("   prov: %s", byId.prov.textContent);
  console.log("   machines: %s · svg nodes %d · day rows %d · names %d (+%d at lane start)",
    machines(page).join(", "), byId.chart.children.length, dayRows, lanes, copies.length);
}

function later(fn) { setTimeout(fn, 5); }

// 1. files only
var p1 = runPage("files", {});
later(function () {
  if (p1.fetched.length !== files["data/index.json"].machines.length + 1) {
    throw new Error("expected the index plus one fetch per listed machine, got " + p1.fetched.join(", "));
  }
  exercise("1. machine files only (no database)", p1);

  // 2. files + the database archive, with a machine no file carries
  var last = firstFile.docs[firstFile.docs.length - 1];
  var extra = { m: "ARCHIVEBOX", w: last.w, tz: 60, rows: [[last.rows[0][0] + 60, 99, 99, 9, last.w]] };
  var delivered = null;
  var p2 = runPage("db", { db: { collection: function (name) {
    if (name !== "readings") throw new Error("page read the wrong collection: " + name);
    return { onSnapshot: function (next) { delivered = next; return function () {}; } };
  } } });
  later(function () {
    if (!delivered) throw new Error("the page never subscribed to the archive");
    delivered({ docs: [extra].map(function (d) { return { data: function () { return d; } }; }) });
    exercise("2. files + database archive", p2);
    if (machines(p2).indexOf("ARCHIVEBOX") < 0) throw new Error("the archive did not reach the machines table");
    if (!/archived/.test(p2.byId.prov.textContent)) throw new Error("provenance does not name the archive");

    // 3. an index naming a machine whose file is not there
    var serve3 = Object.assign({}, files, { "data/index.json": {
      v: 1, machines: files["data/index.json"].machines.concat(["GONE_BOX", "../evil"]) } });
    var p3 = runPage("missing", { serve: serve3 });
    later(function () {
      exercise("3. one listed file missing", p3);
      if (p3.fetched.some(function (u) { return /evil/.test(u); })) throw new Error("the page fetched a path it did not mint");

      // 4. nothing at all
      var p4 = runPage("nothing", { serve: {} });
      later(function () {
        if (!/No readings/.test(p4.byId.prov.textContent)) {
          throw new Error("with nothing to draw the page still says: " + p4.byId.prov.textContent);
        }
        console.log("4. no index\n   prov: %s", p4.byId.prov.textContent);
        console.log("\nok — every path renders, and the loader is %d bytes", Buffer.byteLength(html));
      });
    });
  });
});
