/* The viewer's behaviour: build the tree, keep the keyboard honest, never treat data as markup.
 *
 * Two rules that a reader should be able to check by searching this file:
 *
 * 1. **every value from the capsule is written with `textContent`.** `innerHTML` appears nowhere: a capsule is
 *    untrusted input (it came from somewhere), and a prompt that contains `<img onerror=…>` must render as those exact
 *    characters. `tests/test_ui_viewer.py` feeds the page an XSS corpus and asserts the escaping, and greps this file
 *    for `innerHTML` so the rule cannot rot.
 * 2. **no network, ever.** No `fetch`, no `XMLHttpRequest`, no `WebSocket`, no external URL. The data is the JSON
 *    block in the page; everything else is computed here.
 *
 * The tree is a flat list of rows in the DOM with `role="treeitem"`, because a nested `role="group"` tree is hard to
 * navigate with a screen reader when the groups are rebuilt on every filter. Selection is therefore one index into a
 * visible-row array, and the arrow keys move that index — the same model the terminal `show` uses.
 */
(function () {
  "use strict";

  var data = JSON.parse(document.getElementById("bbx-data").textContent);
  var $ = function (id) { return document.getElementById(id); };
  var tree = $("tree");
  var filterInput = $("filter");
  var rows = [];          // {element, kindOfRow, node, event, depth}
  var selected = -1;
  var collapsed = Object.create(null);   // node slot → true when collapsed
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ── small helpers ───────────────────────────────────────────────────────────────────────────────────────────────
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;                          // textContent, never innerHTML: see the header
  }

  function fmtMs(ms) {
    if (ms === null || ms === undefined) { return "—"; }
    if (ms < 1) { return ms.toFixed(2) + " ms"; }
    if (ms < 1000) { return ms.toFixed(1) + " ms"; }
    return (ms / 1000).toFixed(2) + " s";
  }

  function fmtBytes(n) {
    if (!n) { return "0 B"; }
    var units = ["B", "KiB", "MiB", "GiB"], i = 0, v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return (i === 0 ? v : v.toFixed(1)) + " " + units[i];
  }

  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  // ── header, banner, tapes, links ───────────────────────────────────────────────────────────────────────────────
  function renderHeader() {
    var run = data.run || {};
    $("run-id").textContent = run.run_id || "run";
    document.title = "BlackBox · " + (run.run_id || "run");
    var status = $("run-status");
    status.textContent = run.status || "unknown";
    status.className = "pill " + (run.status === "recovered" ? "pill-ok" : "pill-warn");
    if (run.status_cause) { status.title = run.status_cause; }
    var coverage = $("run-coverage");
    var capture = data.capture || {};
    coverage.textContent = (capture.coverage || "unknown") + " capture";
    if (data.viewer && data.viewer.root_ok === false) {
      coverage.textContent += " · integrity FAILED";
      coverage.className = "pill pill-bad";
    }
  }

  function renderBanner() {
    var banner = $("evidence-banner"), title = $("banner-title"), text = $("banner-text");
    var reasons = [];
    var run = data.run || {};
    if (run.truncated_tail) {
      reasons.push("this tape's tail was torn and was not recorded; everything after seq " +
        (run.lost_from_seq === null || run.lost_from_seq === undefined ? "?" : run.lost_from_seq) + " is missing");
    }
    var gaps = data.gaps || [];
    if (gaps.length) {
      var first = gaps[0];
      reasons.push(gaps.length + " gap" + (gaps.length === 1 ? "" : "s") +
        " declared in the tape (first: " + (first.reason || "reason unstated") + ")");
    }
    if (data.viewer && data.viewer.root_ok === false) {
      reasons.push("the capsule's own integrity root does not match its entries — do not rely on these bytes");
    }
    if (!reasons.length) { banner.hidden = true; return; }
    banner.hidden = false;
    banner.className = "banner" + (data.viewer && data.viewer.root_ok === false ? " bad" : "");
    title.textContent = "Evidence boundary";
    text.textContent = reasons.join(" · ") + ".";
  }

  function renderTapes() {
    var list = $("tapes");
    clear(list);
    var tapes = data.tapes || [{
      capsule_path: (data.viewer || {}).capsule_path, role: (data.viewer || {}).role,
      writer: (data.viewer || {}).writer, events: (data.counts || {}).events,
      nodes: (data.counts || {}).nodes, status: (data.run || {}).status
    }];
    tapes.forEach(function (tape, index) {
      var li = el("li", "tape");
      if (index === 0) { li.setAttribute("aria-current", "true"); }
      var name = el("div", "tname");
      name.appendChild(el("span", null, tape.role || "tape"));
      name.appendChild(el("span", "mono", (tape.events === undefined ? "?" : tape.events) + " ev"));
      li.appendChild(name);
      li.appendChild(el("div", "tmeta", tape.capsule_path || ""));
      if (tape.writer) { li.appendChild(el("div", "tmeta", "writer " + tape.writer.slice(0, 12))); }
      list.appendChild(li);
    });
  }

  function renderLinks() {
    var list = $("links");
    clear(list);
    var links = ((data.links || {}).links) || [];
    var summary = (data.links || {}).summary || {};
    if (!links.length) {
      list.appendChild(el("li", "link-row muted",
        "No link to another tape. " + (summary.exact === 0 ? "These tapes were looked at and nothing matched." : "")));
      return;
    }
    links.forEach(function (link) {
      var li = el("li", "link-row");
      li.appendChild(el("span", "cls", link["class"] || "link"));
      li.appendChild(document.createTextNode(" · confidence " + link.confidence));
      li.appendChild(el("div", "tmeta", (link.left_writer || "").slice(0, 8) + " → " + (link.right_writer || "").slice(0, 8)));
      if (link.evidence && link.evidence.rule) { li.appendChild(el("div", "tmeta", link.evidence.rule)); }
      list.appendChild(li);
    });
  }

  function renderCost() {
    var list = $("cost");
    clear(list);
    var cost = data.cost || {}, counts = data.counts || {};
    var pairs = [
      ["events", counts.events === undefined ? (data.events || []).length : counts.events],
      ["nodes", counts.nodes === undefined ? (data.nodes || []).length : counts.nodes],
      ["model calls", cost.model_calls], ["tool calls", cost.tool_calls],
      ["chunks", cost.chunks], ["stream", fmtBytes(cost.stream_bytes)],
      ["interrupted", counts.nodes_interrupted === undefined ? 0 : counts.nodes_interrupted]
    ];
    pairs.forEach(function (pair) {
      list.appendChild(el("dt", null, pair[0]));
      list.appendChild(el("dd", null, pair[1] === undefined ? "0" : pair[1]));
    });
    var redaction = data.redaction || {};
    var boundary = [];
    boundary.push("content " + (redaction.content_mode || "unknown"));
    if (redaction.masks !== undefined) { boundary.push(redaction.masks + " masks"); }
    boundary.push("root " + String((data.viewer || {}).root || "").slice(0, 10) + "…");
    boundary.push("offline: this page fetched nothing");
    $("boundary").textContent = boundary.join(" · ");
  }

  // ── the tree ──────────────────────────────────────────────────────────────────────────────────────────────────
  function kindClass(kind) {
    if (kind >= 32 && kind <= 35) { return "badge-model"; }
    if (kind === 48 || kind === 49) { return "badge-tool"; }
    if (kind === 112) { return "badge-mark"; }
    return "badge-mark";
  }

  function matches(filter, event, node) {
    if (!filter) { return true; }
    var haystack = [event.kind_name, event.summary, node ? node.name : "", node ? node.kind : ""].join(" ").toLowerCase();
    return haystack.indexOf(filter) !== -1;
  }

  function buildRows(filter) {
    var nodes = data.nodes || [];
    var events = data.events || [];
    var bySlot = Object.create(null);
    nodes.forEach(function (node) { bySlot[node.slot] = node; });
    var out = [];
    var ungrouped = [];
    events.forEach(function (event) {
      var node = bySlot[event.node_slot] || null;
      if (event.node_slot && node) { node._events = node._events || []; node._events.push(event); }
      else { ungrouped.push(event); }
    });
    nodes.forEach(function (node) {
      var children = (node._events || []).filter(function (event) { return matches(filter, event, node); });
      var selfMatches = matches(filter, {kind_name: "node", summary: node.name}, node);
      if (filter && !children.length && !selfMatches) { return; }
      out.push({ type: "node", node: node, depth: 0, children: children });
      if (!collapsed[node.slot]) {
        children.forEach(function (event) { out.push({ type: "event", event: event, node: node, depth: 1 }); });
      }
    });
    ungrouped.filter(function (event) { return matches(filter, event, null); }).forEach(function (event) {
      out.push({ type: "event", event: event, node: null, depth: 0 });
    });
    return out;
  }

  function renderTree() {
    var filter = filterInput.value.trim().toLowerCase();
    var model = buildRows(filter);
    clear(tree);
    rows = [];
    tree.setAttribute("aria-busy", "false");
    if (!model.length) { $("empty").hidden = false; return; }
    $("empty").hidden = true;
    model.forEach(function (item, index) {
      var row = el("div", "row");
      row.setAttribute("role", "treeitem");
      row.setAttribute("aria-level", String(item.depth + 1));
      row.setAttribute("aria-selected", "false");
      row.tabIndex = -1;
      if (item.type === "node") {
        var node = item.node;
        var hasChildren = (node._events || []).length > 0;
        row.className = "row is-node";
        var twisty = el("span", "twisty" + (hasChildren ? "" : " is-empty"), collapsed[node.slot] ? "▸" : "▾");
        row.appendChild(twisty);
        row.appendChild(el("span", "when", "+" + fmtMs(node.opened_rel_ms)));
        var summary = el("span", "summary", node.name);
        summary.appendChild(document.createTextNode("  ·  " + node.kind + (node.duration_ms === null ? "" : "  ·  " + fmtMs(node.duration_ms))));
        row.appendChild(summary);
        var badge = el("span", "badge " + (node.closed ? "badge-model" : "badge-open"), node.closed ? "closed" : "open");
        row.appendChild(badge);
        row.setAttribute("aria-expanded", hasChildren ? String(!collapsed[node.slot]) : "false");
        row.setAttribute("aria-label", node.name + ", " + node.kind + ", " + node.status);
      } else {
        var event = item.event;
        row.className = "row" + (item.depth ? " is-child" : "");
        row.appendChild(el("span", "twisty is-empty", "·"));
        row.appendChild(el("span", "when", "+" + fmtMs(event.rel_ms)));
        var line = el("span", "summary");
        line.appendChild(el("span", "kind", event.kind_name + " "));
        line.appendChild(document.createTextNode(event.summary || ""));
        row.appendChild(line);
        row.appendChild(el("span", "badge " + kindClass(event.kind), "seq " + event.seq));
        row.setAttribute("aria-label", event.kind_name + " at " + fmtMs(event.rel_ms) + ": " + (event.summary || ""));
      }
      // Mouse selection never steals the keyboard: clicking moves the same selection the arrows move.
      row.addEventListener("mousedown", function (evt) { evt.preventDefault(); select(index, true); });
      row.addEventListener("click", function () { select(index, true); });
      tree.appendChild(row);
      rows.push({ element: row, item: item });
    });
    if (selected >= rows.length) { selected = rows.length - 1; }
    if (selected < 0 && rows.length) { select(0, false); } else { paintSelection(); }
  }

  function paintSelection() {
    rows.forEach(function (row, index) {
      var isSel = index === selected;
      row.element.classList.toggle("is-selected", isSel);
      row.element.setAttribute("aria-selected", String(isSel));
    });
    var active = rows[selected];
    if (active) {
      active.element.tabIndex = 0;
      renderInspector(active.item);
    }
  }

  function select(index, focus) {
    if (index < 0 || index >= rows.length) { return; }
    selected = index;
    rows.forEach(function (row, i) { if (i !== index) { row.element.tabIndex = -1; } });
    paintSelection();
    if (focus) { rows[index].element.focus({ preventScroll: false }); }
    rows[index].element.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
  }

  function toggleNode(index) {
    var row = rows[index];
    if (!row || row.item.type !== "node") { return; }
    var slot = row.item.node.slot;
    collapsed[slot] = !collapsed[slot];
    var keep = index;
    renderTree();
    // Keep the reader where they were: the same node is at or above the old index once children are gone.
    var target = Math.min(keep, rows.length - 1);
    if (target >= 0) { select(target, true); }
  }

  // ── inspector ────────────────────────────────────────────────────────────────────────────────────────────────
  function renderInspector(item) {
    var box = $("inspector");
    clear(box);
    if (!item) { return; }
    if (item.type === "node") {
      var node = item.node;
      box.appendChild(el("h3", null, node.name));
      box.appendChild(el("p", "sub", "node slot " + node.slot + " · " + node.kind));
      var kv = el("dl", "kv");
      [["status", node.status],
       ["opened", "+" + fmtMs(node.opened_rel_ms)],
       ["closed", node.closed ? "+" + fmtMs(node.closed_rel_ms) : "never (the process ended first)"],
       ["duration", node.duration_ms === null ? "unknown — no close frame" : fmtMs(node.duration_ms)],
       ["model calls", node.model_calls], ["chunks", node.chunks],
       ["tools", node.tools.length ? node.tools.join(", ") : "none"]].forEach(function (pair) {
        kv.appendChild(el("dt", null, pair[0]));
        kv.appendChild(el("dd", null, pair[1]));
      });
      box.appendChild(kv);
      if (!node.closed) {
        box.appendChild(el("p", "note",
          "This node has no close frame. What happened inside it is in the frames below it — the viewer will not " +
          "claim an outcome the tape does not contain."));
      }
      return;
    }
    var event = item.event;
    box.appendChild(el("h3", null, event.kind_name));
    box.appendChild(el("p", "sub", "seq " + event.seq + " · node slot " + (event.node_slot || 0) + " · +" + fmtMs(event.rel_ms)));
    box.appendChild(el("p", null, event.summary || "(no payload summary)"));
    var meta = el("dl", "kv");
    meta.appendChild(el("dt", null, "wall"));
    meta.appendChild(el("dd", "mono", new Date(event.wall_ns / 1e6).toISOString()));
    meta.appendChild(el("dt", null, "kind"));
    meta.appendChild(el("dd", "mono", String(event.kind)));
    box.appendChild(meta);
    box.appendChild(el("p", "note",
      "Payloads are summarised, never rendered as markup: a prompt or a tool argument appears here as the characters " +
      "that were recorded."));
  }

  // ── wiring ───────────────────────────────────────────────────────────────────────────────────────────────────
  function onKey(event) {
    var tag = (event.target && event.target.tagName) || "";
    var typing = tag === "INPUT" || tag === "TEXTAREA";
    if (event.key === "?" && !typing) { openHelp(); event.preventDefault(); return; }
    if (event.key === "/" && !typing) { filterInput.focus(); event.preventDefault(); return; }
    if (event.key === "Escape") {
      if (document.getElementById("help").open) { document.getElementById("help").close(); }
      else if (typing) { filterInput.value = ""; renderTree(); }
      return;
    }
    if (typing) { return; }
    if (event.key === "ArrowDown") { select(Math.min(selected + 1, rows.length - 1), true); event.preventDefault(); }
    else if (event.key === "ArrowUp") { select(Math.max(selected - 1, 0), true); event.preventDefault(); }
    else if (event.key === "Home") { select(0, true); event.preventDefault(); }
    else if (event.key === "End") { select(rows.length - 1, true); event.preventDefault(); }
    else if (event.key === "ArrowRight" || event.key === "Enter") {
      var row = rows[selected];
      if (row && row.item.type === "node") { toggleNode(selected); }
      event.preventDefault();
    } else if (event.key === "ArrowLeft") {
      var current = rows[selected];
      if (current && current.item.type === "node" && !collapsed[current.item.node.slot]) { toggleNode(selected); }
      event.preventDefault();
    }
  }

  function openHelp() {
    var dialog = document.getElementById("help");
    if (typeof dialog.showModal === "function") { dialog.showModal(); }
    else { dialog.setAttribute("open", "open"); }
  }

  renderHeader();
  renderBanner();
  renderTapes();
  renderLinks();
  renderCost();
  renderTree();
  filterInput.addEventListener("input", function () { selected = -1; renderTree(); });
  $("btn-help").addEventListener("click", openHelp);
  document.addEventListener("keydown", onKey);
  tree.addEventListener("keydown", onKey);
})();
