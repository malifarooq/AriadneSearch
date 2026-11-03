"use strict";

const $ = (id) => document.getElementById(id);
const GRID_CELL = 24;
const GRAPH_LIMIT = 200;
const FRONTIER_SIZE = 50;
const FIT_PADDING = 60;
const NEW_PROJECT = "__new";
const STORED_PROJECT = "ariadne.project";
const STATE_NAMES = { seed: "Seed", accepted: "Accepted", read: "Read", rejected: "Rejected" };

const state = {
  projects: [],
  projectId: null,
  health: null,
  items: [],
  selectedId: null,
  listMode: "suggestions", // "added": your papers only, "recent": state of the art
  recentSince: new Date().getFullYear() - 1,
  view: "map",
  sizeBy: "percentile",
  positions: new Map(), // saved map positions
  graph: null,
  centerForce: null,
  palette: {},
  neighbours: new Map(),
  hoverId: null,
  sizeRank: new Map(),
  ticks: [],
  fitPending: false,
  fitTimer: null,
  highlight: null, // ids lit by search, a legend entry or an author; others fade
  colourBy: "state", // or "topic", "subfield"
  categorySlots: new Map(), // category -> palette slot, kept while the project is open
  legendFocus: null,
  authorOpen: null,
  searchResults: [],
  searchActive: -1,
  justAdded: [], // citing papers added from the details panel, drawn whatever their rank
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child !== null && child !== undefined));
  return node;
}

// OpenAlex titles can contain markup such as <sub>.
function plain(text) {
  return (text || "Untitled").replace(/<[^>]+>/g, "");
}

function say(message, isError = false) {
  const status = $("status");
  status.textContent = message;
  status.classList.toggle("is-error", isError);
}

async function api(path, body, method) {
  const options = { headers: { "Content-Type": "application/json" } };
  if (body !== undefined) {
    options.method = "POST";
    options.body = JSON.stringify(body);
  }
  if (method) options.method = method;
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : null;
    throw new Error(detail || `The request failed with status ${response.status}.`);
  }
  return data;
}

async function attempt(action) {
  try {
    return await action();
  } catch (error) {
    say(error.message, true);
    return undefined;
  }
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function projectPath(suffix = "") {
  return `/api/projects/${state.projectId}${suffix}`;
}

function storedProject() {
  try {
    return Number(localStorage.getItem(STORED_PROJECT)) || null;
  } catch {
    return null;
  }
}

function storeProject(id) {
  try {
    localStorage.setItem(STORED_PROJECT, String(id));
  } catch {
    // The choice just won't be remembered.
  }
}

/* Projects and the title block */

async function loadHealth() {
  state.health = await api("/api/health");
  const { usage, limits, core_rows: coreRows } = state.health;
  $("tb-openalex").textContent =
    `$${usage.openalex_cost_usd.toFixed(4)} of $${limits.openalex_daily_usd.toFixed(2)}`;
  $("tb-ieee").textContent = `${usage.ieee_calls} of ${limits.ieee_daily_calls}`;
  $("min-rank").disabled = coreRows === 0;
  $("rank-note").hidden = coreRows > 0;
}

function currentProject() {
  return state.projects.find((project) => project.id === state.projectId);
}

function renderProjects() {
  const select = $("project-select");
  select.replaceChildren(
    ...state.projects.map((project) =>
      el("option", { value: project.id }, project.name)
    ),
    el("option", { value: NEW_PROJECT }, "New project")
  );
  select.hidden = state.projects.length === 0;
  if (state.projectId) select.value = String(state.projectId);

  const project = currentProject();
  const counts = project ? project.counts : {};
  $("tb-project").textContent = project ? project.name : "None";
  $("tb-seeds").textContent = counts.seed || 0;
  $("tb-accepted").textContent = counts.accepted || 0;
  $("tb-frontier").textContent = counts.frontier || 0;

  const link = $("export-link");
  if (project) {
    link.href = projectPath("/export.bib");
    link.removeAttribute("aria-disabled");
  } else {
    link.removeAttribute("href");
    link.setAttribute("aria-disabled", "true");
  }
  const pending = project ? project.pending : 0;
  $("expand-button").textContent = pending ? `Update graph (${pending} new)` : "Update graph";
  $("expand-button").disabled = !project;
  $("update-button").disabled = !project;
}

async function loadProjects() {
  const { projects } = await api("/api/projects");
  state.projects = projects;
  if (!projects.some((project) => project.id === state.projectId)) {
    const stored = storedProject();
    const match = projects.find((project) => project.id === stored) || projects[0];
    state.projectId = match ? match.id : null;
  }
  $("project-form").hidden = projects.length > 0;
  renderProjects();
}

async function refresh() {
  await loadProjects();
  await Promise.all([loadHealth(), loadList(), loadGraph()]);
}

async function switchProject(id) {
  state.projectId = id;
  state.selectedId = null;
  state.positions.clear();
  state.categorySlots = new Map();
  state.justAdded = [];
  storeProject(id);
  renderDetailsEmpty();
  await refresh();
}

/* Suggestions */

function rankLabel(rank, withEdition = false) {
  if (rank.edition === "target") return `Target ${rank.rank}`;
  return withEdition ? `CORE ${rank.rank} (${rank.edition})` : `CORE ${rank.rank}`;
}

// OpenAlex's 2-year mean citedness, computed like an impact factor on OpenAlex data.
function impactTag(journal) {
  if (journal?.mean_citedness == null) return null;
  return el(
    "span",
    {
      class: "tag tag-rank",
      title: "OpenAlex 2-year mean citedness, computed like an impact factor. Not Clarivate's JIF.",
    },
    `Journal 2-yr impact ${journal.mean_citedness.toFixed(1)}`
  );
}

function tagsFor(item) {
  const tags = [];
  if (STATE_NAMES[item.state]) tags.push(el("span", { class: "tag" }, STATE_NAMES[item.state]));
  if (item.populated === false) tags.push(el("span", { class: "tag tag-explore" }, "Not in graph yet"));
  if (item.rank) tags.push(el("span", { class: "tag tag-rank" }, rankLabel(item.rank)));
  const impact = impactTag(item.journal);
  if (impact) tags.push(impact);
  if (item.mine.cited_by_me) {
    const year = item.mine.last_cited_year;
    tags.push(
      el("span", { class: "tag tag-cited" }, year ? `Cited by me, ${year}` : "Cited by me")
    );
  }
  if (item.mine.in_library) tags.push(el("span", { class: "tag" }, "In my library"));
  if (item.explore) tags.push(el("span", { class: "tag tag-explore" }, "Exploration pick"));
  return tags.length ? el("div", { class: "tags" }, ...tags) : null;
}

function paperRow(item) {
  const select = () => selectPaper(item.id);
  const meta = [item.label, item.venue].filter(Boolean).join(", ");
  const row = el(
    "li",
    {
      class: "paper-row",
      "data-id": item.id,
      "aria-current": item.id === state.selectedId ? "true" : "false",
      onclick: select,
    },
    el("button", { type: "button", class: "paper-title" }, plain(item.title)),
    el("p", { class: "paper-meta" }, meta),
    item.reasons.length ? el("p", { class: "paper-why" }, item.reasons[0]) : null,
    tagsFor(item),
    item.state
      ? null // added papers are changed from the details panel
      : el(
          "div",
          { class: "row-actions", onclick: (event) => event.stopPropagation() },
          el("button", { type: "button", onclick: () => decide(item.id, "accepted") }, "Accept"),
          el("button", { type: "button", onclick: () => decide(item.id, "rejected") }, "Reject")
        )
  );
  return row;
}

function loadList() {
  if (state.listMode === "added") return loadAdded();
  if (state.listMode === "authors") return loadAuthors();
  return loadFrontier();
}

async function loadAuthors() {
  state.items = [];
  const authors = state.projectId ? (await api(projectPath("/authors"))).items : [];
  const rows = authors.map((author) => {
    const open = state.authorOpen === author.name;
    const papers = open
      ? el(
          "ul",
          { class: "author-papers" },
          ...author.papers.map((paper) =>
            el(
              "li",
              {},
              el(
                "button",
                { type: "button", class: "link-button", onclick: () => selectPaper(paper.id) },
                `${paper.label}: ${plain(paper.title)}`,
                onGraph(paper.id) ? "" : " (not on the graph)"
              )
            )
          )
        )
      : null;
    return el(
      "li",
      { class: "paper-row author-row", "aria-current": String(open) },
      el(
        "button",
        { type: "button", class: "paper-title", onclick: () => focusAuthor(author) },
        author.name
      ),
      el(
        "p",
        { class: "paper-meta" },
        [author.added ? `${plural(author.added, "added paper")}` : "", author.suggested ? `${plural(author.suggested, "suggestion")}` : ""]
          .filter(Boolean)
          .join(" · ")
      ),
      papers
    );
  });
  $("frontier-list").replaceChildren(...rows);
  $("suggestion-count").textContent = authors.length ? `${authors.length} with 2 or more papers` : "";
  $("frontier-empty").textContent = state.projectId && !authors.length ? "No author appears on two papers yet." : "";
}

function focusAuthor(author) {
  state.authorOpen = state.authorOpen === author.name ? null : author.name;
  state.highlight = state.authorOpen ? new Set(author.work_ids) : null;
  redraw();
  attempt(loadAuthors);
}

async function loadAdded() {
  state.items = state.projectId ? (await api(projectPath("/added"))).items : [];
  $("frontier-list").replaceChildren(...state.items.map(paperRow));
  $("suggestion-count").textContent = state.items.length ? plural(state.items.length, "paper") : "";
  let message = "";
  if (!state.projectId) message = "Create a project to get started.";
  else if (!state.items.length) message = "No added papers yet. Add a seed or accept a suggestion.";
  $("frontier-empty").textContent = message;
}

const LIST_HEADINGS = {
  suggestions: "Suggestions",
  added: "Added papers",
  recent: "State of the art",
  authors: "Authors",
};

async function setListMode(mode) {
  state.listMode = mode;
  for (const name of Object.keys(LIST_HEADINGS)) {
    $(`list-${name}`).setAttribute("aria-pressed", String(mode === name));
  }
  $("suggestions-heading").textContent = LIST_HEADINGS[mode];
  $("filters").hidden = mode === "added" || mode === "authors";
  state.authorOpen = null;
  state.highlight = null;
  $("recent-filter").hidden = mode !== "recent";
  // Added papers mode leaves out everything still to review, counts included.
  for (const id of ["tb-frontier-cell", "legend-review"]) $(id).hidden = mode === "added";
  if (mode === "recent" && state.view !== "timeline") setView("timeline");
  await Promise.all([loadList(), loadGraph()]);
}

async function loadFrontier() {
  if (!state.projectId) {
    state.items = [];
    renderFrontier(0, 0);
    return;
  }
  const params = new URLSearchParams({ k: FRONTIER_SIZE });
  if ($("hide-cited").checked) params.set("hide_cited", "true");
  if ($("hide-library").checked) params.set("hide_library", "true");
  if ($("min-rank").value) params.set("min_rank", $("min-rank").value);
  if ($("min-impact").value) params.set("min_impact", $("min-impact").value);
  if ($("sort-by").value !== "relevance") params.set("sort", $("sort-by").value);
  if (state.listMode === "recent") params.set("min_year", state.recentSince);
  const data = await api(projectPath(`/frontier?${params}`));
  state.items = data.items;
  renderFrontier(data.total, data.matching);
}

function renderFrontier(total, matching) {
  $("frontier-list").replaceChildren(...state.items.map(paperRow));
  $("suggestion-count").textContent = total
    ? `${state.items.length} of ${matching} matching`
    : "";

  let message = "";
  if (!state.projectId) message = "Create a project to get started.";
  else if (!total) message = "Add a seed, then press Update graph to fetch connected papers.";
  else if (!matching && state.listMode === "recent") {
    message = `No suggestions from ${state.recentSince} on yet. Check new papers finds recent work.`;
  }
  else if (!matching) message = "No papers match these filters.";
  $("frontier-empty").textContent = message;
}

function markSelectedRow() {
  for (const row of document.querySelectorAll(".paper-row")) {
    row.setAttribute("aria-current", Number(row.dataset.id) === state.selectedId ? "true" : "false");
  }
}

async function decide(workId, decision) {
  const index = state.items.findIndex((item) => item.id === workId);
  const next = index >= 0 ? state.items[index + 1] || state.items[index - 1] : null;
  const done = await attempt(async () => {
    await api(projectPath("/decisions"), { work_id: workId, decision });
    return true;
  });
  if (!done) return;
  const words = { accepted: "Accepted", rejected: "Rejected", read: "Marked read" };
  const title = plain(state.items[index]?.title || "the paper");
  const later = decision === "rejected" ? "" : " Press Update graph to add its neighbours.";
  say(`${words[decision]}: ${title}.${later}`);
  // The graph keeps its shape until Update graph. Only the paper's mark changes.
  await attempt(async () => {
    await loadProjects();
    await Promise.all([loadHealth(), loadList()]);
  });
  updateNodeState(workId, decision);
  if (next) selectPaper(next.id);
  else renderDetailsEmpty();
}

/* Graph

   force-graph draws the papers on a canvas and moves them with d3-force, the
   same pairing Obsidian's graph view uses. A collision force keeps papers apart,
   labels appear as you zoom in without covering each other, and hovering a paper
   dims everything not linked to it. */

// What node size can show. Sizes follow each paper's rank among the papers on
// screen, so differences stay visible however the values are spread.
const SIZE_METRICS = {
  percentile: (node) => node.percentile, // citations against papers of its field and year
  citations: (node) => node.cited_by,
  project: (node) => node.project_citations,
  relevance: (node) => node.score ?? (KNOWN_STATES.has(node.state) ? Infinity : null),
};
const MIN_RADIUS = 3;
const RADIUS_RANGE = 10;
const KNOWN_STATES = new Set(["seed", "accepted", "read"]);
const LABEL_SIZE = 11; // screen pixels at any zoom
const LABEL_MIN_SCREEN_RADIUS = 9; // suggestions get a label once this large on screen
const TICK_MIN_SPACING = 40; // screen pixels between year labels

// The dataviz skill's categorical palette, validated against this page's light
// paper and dark blueprint surfaces. Categories past the eighth fold into Other.
const CATEGORY_COLOURS = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
};
const OTHER = "Other";

function categoryOf(node) {
  return node[state.colourBy] || OTHER;
}

// A category keeps its colour while the project is open, so reloading or
// filtering never repaints the rest.
function assignCategories(nodes) {
  if (state.colourBy === "state") return;
  const counts = new Map();
  for (const node of nodes) {
    const category = categoryOf(node);
    if (category !== OTHER) counts.set(category, (counts.get(category) || 0) + 1);
  }
  const used = new Set(state.categorySlots.values());
  const slots = CATEGORY_COLOURS.light.length;
  for (const [category] of [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))) {
    if (state.categorySlots.has(category) || used.size >= slots) continue;
    const free = [...Array(slots).keys()].find((slot) => !used.has(slot));
    state.categorySlots.set(category, free);
    used.add(free);
  }
  renderLegend(nodes);
}

function categoryColour(category) {
  const slot = state.categorySlots.get(category);
  if (slot === undefined) return state.palette.pencil;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  return CATEGORY_COLOURS[dark ? "dark" : "light"][slot];
}

function renderLegend(nodes) {
  const legend = $("graph-legend");
  if (state.colourBy === "state") {
    legend.hidden = true;
    return;
  }
  const counts = new Map();
  for (const node of nodes) {
    const category = state.categorySlots.has(categoryOf(node)) ? categoryOf(node) : OTHER;
    counts.set(category, (counts.get(category) || 0) + 1);
  }
  const rows = [...counts]
    .sort((a, b) => (a[0] === OTHER) - (b[0] === OTHER) || b[1] - a[1])
    .map(([category, count]) =>
      el(
        "li",
        {},
        el(
          "button",
          {
            type: "button",
            "aria-pressed": String(state.legendFocus === category),
            title: "Highlight these papers",
            onclick: () => focusCategory(category),
          },
          el("span", { class: "swatch", style: `background:${categoryColour(category)}` }),
          `${category} (${count})`
        )
      )
    );
  $("legend-items").replaceChildren(...rows);
  $("legend-title").textContent = state.colourBy === "topic" ? "Topics" : "Research areas";
  legend.hidden = false;
}

function focusCategory(category) {
  const nodes = state.graph?.graphData().nodes || [];
  state.legendFocus = state.legendFocus === category ? null : category;
  const inCategory = (node) =>
    (state.categorySlots.has(categoryOf(node)) ? categoryOf(node) : OTHER) === category;
  state.highlight = state.legendFocus ? new Set(nodes.filter(inCategory).map((node) => node.id)) : null;
  renderLegend(nodes);
  redraw();
}

function setColourBy(mode) {
  state.colourBy = mode;
  state.categorySlots = new Map();
  state.legendFocus = null;
  state.highlight = null;
  assignCategories(state.graph?.graphData().nodes || []);
  renderLegend(state.graph?.graphData().nodes || []);
  redraw();
}

function cssColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function readPalette() {
  state.palette = Object.fromEntries(
    ["ink", "pencil", "paper", "sheet", "red", "graphite"].map((name) => [name, cssColor(`--${name}`)])
  );
}

// Accepts #rgb, #rrggbb and rgb() colours from the CSS variables.
function withAlpha(color, alpha) {
  if (color.startsWith("#")) {
    const hex = color.length === 4 ? [...color.slice(1)].map((c) => c + c).join("") : color.slice(1);
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  return color.replace(/^rgb\((.*)\)$/, `rgba($1, ${alpha})`);
}

function rankSizes(nodes) {
  const value = SIZE_METRICS[state.sizeBy];
  const sorted = nodes.map(value).filter((item) => item != null).sort((a, b) => a - b);
  const position = (target, strict) => {
    let low = 0;
    let high = sorted.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (strict ? sorted[middle] < target : sorted[middle] <= target) low = middle + 1;
      else high = middle;
    }
    return low;
  };
  state.sizeRank = new Map(
    nodes.map((node) => {
      const item = value(node);
      if (item == null || sorted.length < 2) return [node.id, item == null ? 0 : 0.5];
      const middle = (position(item, true) + position(item, false) - 1) / 2; // ties share
      return [node.id, middle / (sorted.length - 1)];
    })
  );
}

function radius(node) {
  return MIN_RADIUS + RADIUS_RANGE * (state.sizeRank.get(node.id) ?? 0) ** 1.5;
}

function nodeId(end) {
  return typeof end === "object" ? end.id : end;
}

function isFaded(node) {
  if (state.hoverId) {
    if (node.id === state.hoverId) return false;
    return !state.neighbours.get(state.hoverId)?.has(node.id);
  }
  return state.highlight !== null && !state.highlight.has(node.id);
}

function linkIsLit(link) {
  const ends = [nodeId(link.source), nodeId(link.target)];
  return ends.includes(state.hoverId) || ends.includes(state.selectedId);
}

function redraw() {
  // force-graph pauses drawing when nothing moves. Setting a drawing property
  // asks for one more frame.
  if (state.graph) state.graph.nodeCanvasObject(drawNode);
}

function drawNode(node, ctx, scale) {
  const { ink, pencil, sheet, red } = state.palette;
  const r = radius(node);
  ctx.globalAlpha = isFaded(node) ? 0.12 : node.state === "rejected" ? 0.25 : 1;
  ctx.beginPath();
  if (node.state === "seed") {
    const d = r * 1.3;
    ctx.moveTo(node.x, node.y - d);
    ctx.lineTo(node.x + d, node.y);
    ctx.lineTo(node.x, node.y + d);
    ctx.lineTo(node.x - d, node.y);
    ctx.closePath();
  } else {
    ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
  }
  const filled = node.state === "seed" || node.state === "accepted";
  if (state.colourBy === "state") {
    ctx.fillStyle = filled ? ink : node.state === "read" ? pencil : sheet;
    ctx.fill();
  } else {
    // Your papers are solid, papers to review are a pale tint of the same colour.
    const colour = categoryColour(state.categorySlots.has(categoryOf(node)) ? categoryOf(node) : OTHER);
    ctx.fillStyle = KNOWN_STATES.has(node.state) ? colour : withAlpha(colour, 0.35);
    ctx.fill();
  }
  ctx.lineWidth = (node.cited_by_me ? 2.4 : 1.2) / Math.sqrt(scale);
  ctx.strokeStyle = node.cited_by_me ? red : KNOWN_STATES.has(node.state) ? ink : pencil;
  ctx.stroke();
  if (node.id === state.selectedId) {
    ctx.beginPath();
    ctx.arc(node.x, node.y, r * 1.3 + 3 / scale, 0, 2 * Math.PI);
    ctx.lineWidth = 2 / scale;
    ctx.strokeStyle = ink;
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}

function paintHitArea(node, color, ctx) {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(node.x, node.y, radius(node) * 1.3 + 2, 0, 2 * Math.PI);
  ctx.fill();
}

function labelPriority(node, scale) {
  if (node.id === state.selectedId) return 1000;
  if (node.id === state.hoverId) return 900;
  if ((state.hoverId || state.highlight) && !isFaded(node)) return 500 + radius(node);
  if (KNOWN_STATES.has(node.state)) return 100 + radius(node);
  return radius(node) * scale >= LABEL_MIN_SCREEN_RADIUS ? radius(node) : 0;
}

// Labels go on last, best first, and skip any that would cover one already drawn.
function drawLabels(ctx, scale) {
  if (!state.graph) return;
  const { ink, paper } = state.palette;
  const size = LABEL_SIZE / scale;
  ctx.font = `${size}px "Barlow Semi Condensed", sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  const queue = state.graph
    .graphData()
    .nodes.filter((node) => Number.isFinite(node.x))
    .map((node) => [labelPriority(node, scale), node])
    .filter(([priority]) => priority > 0)
    .sort((a, b) => b[0] - a[0]);
  const placed = [];
  for (const [, node] of queue) {
    const width = ctx.measureText(node.label).width + 4 / scale;
    const box = {
      x: node.x - width / 2,
      y: node.y + radius(node) * (node.state === "seed" ? 1.3 : 1) + 2 / scale,
      w: width,
      h: size * 1.25,
    };
    const covers = (other) =>
      box.x < other.x + other.w && other.x < box.x + box.w &&
      box.y < other.y + other.h && other.y < box.y + box.h;
    if (placed.some(covers)) continue;
    placed.push(box);
    ctx.globalAlpha = isFaded(node) ? 0.15 : 1;
    ctx.fillStyle = withAlpha(paper, 0.85);
    ctx.fillRect(box.x, box.y, box.w, box.h);
    ctx.fillStyle = ink;
    ctx.fillText(node.label, node.x, box.y + size * 0.1);
  }
  ctx.globalAlpha = 1;
  if (state.view === "timeline") drawYearTicks(ctx, scale);
}

function drawYearTicks(ctx, scale) {
  const { graphite } = state.palette;
  const bottom = state.graph.screen2GraphCoords(0, $("graph").clientHeight - 20).y;
  ctx.font = `${13 / scale}px "Barlow Semi Condensed", sans-serif`;
  ctx.fillStyle = graphite;
  ctx.textAlign = "center";
  let last = -Infinity;
  for (const tick of state.ticks) {
    if ((tick.x - last) * scale < TICK_MIN_SPACING) continue;
    ctx.fillText(String(tick.year), tick.x, bottom);
    last = tick.x;
  }
}

// Push overlapping papers apart. Fine for the few hundred shown at once.
function collideForce() {
  let nodes = [];
  function force() {
    const padding = state.view === "timeline" ? 1.5 : 3;
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        const gap = radius(a) * 1.3 + radius(b) * 1.3 + padding;
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        const distance = Math.hypot(dx, dy) || 0.01;
        if (distance >= gap) continue;
        const push = ((gap - distance) / distance) * 0.5;
        dx *= push;
        dy *= push;
        a.vx -= dx;
        a.vy -= dy;
        b.vx += dx;
        b.vy += dy;
      }
    }
  }
  force.initialize = (all) => {
    nodes = all;
  };
  return force;
}

// A weak pull to the middle keeps separate groups of papers close by.
function gravityForce(strength) {
  let nodes = [];
  function force(alpha) {
    for (const node of nodes) {
      node.vx -= node.x * strength * alpha;
      node.vy -= node.y * strength * alpha;
    }
  }
  force.initialize = (all) => {
    nodes = all;
  };
  return force;
}

// Pull each paper towards a target, such as its place on the timeline.
function targetForce(targets, strengthX, strengthY) {
  let nodes = [];
  function force(alpha) {
    for (const node of nodes) {
      const target = targets.get(node.id);
      if (!target) continue;
      node.vx += (target.x - node.x) * strengthX * alpha;
      node.vy += (target.y - node.y) * strengthY * alpha;
    }
  }
  force.initialize = (all) => {
    nodes = all;
  };
  return force;
}

// Each year gets room for the papers it holds, and empty years take little,
// so a crowded 2026 spreads out instead of being squeezed against 1960.
function timelineTargets(nodes) {
  const counts = new Map();
  for (const node of nodes) if (node.year) counts.set(node.year, (counts.get(node.year) || 0) + 1);
  const years = [...counts.keys()].sort((a, b) => a - b);
  const starts = new Map();
  const widths = new Map();
  state.ticks = [];
  let x = 0;
  let previous = null;
  for (const year of years) {
    if (previous !== null) x += Math.min(year - previous - 1, 5) * 8;
    const width = 24 + 6.5 * counts.get(year);
    starts.set(year, x);
    widths.set(year, width);
    state.ticks.push({ year, x: x + width / 2 });
    x += width;
    previous = year;
  }
  const radii = nodes.map(radius);
  const smallest = Math.min(...radii);
  const span = Math.max(...radii) - smallest || 1;
  const height = Math.max(240, x * 0.5);
  const targets = new Map();
  // Within its year, a paper sits by publication date, so the columns read as time.
  const withinYear = (node) => {
    const date = node.pub_date ? new Date(node.pub_date) : null;
    if (!date || Number.isNaN(date.getTime())) return 0.5;
    return (date - new Date(date.getFullYear(), 0, 1)) / (365.25 * 24 * 3600 * 1000);
  };
  for (const node of nodes) {
    targets.set(node.id, {
      x: starts.has(node.year) ? starts.get(node.year) + widths.get(node.year) * withinYear(node) : -40,
      y: height * (1 - (radius(node) - smallest) / span), // more important is higher
    });
  }
  return targets;
}

function configureForces() {
  const graph = state.graph;
  if (!graph) return;
  const timeline = state.view === "timeline";
  const { nodes } = graph.graphData();
  graph.d3Force("link").strength(timeline ? 0 : 0.3).distance((link) => 28 + radius(link.source) + radius(link.target));
  graph.d3Force("charge").strength(timeline ? -6 : -40).distanceMax(320);
  graph.d3Force("center", timeline ? null : state.centerForce);
  graph.d3Force("collide", collideForce());
  graph.d3Force("target", timeline ? targetForce(timelineTargets(nodes), 0.3, 0.12) : null);
  graph.d3Force("gravity", timeline ? null : gravityForce(0.04));
  state.fitPending = true;
  graph.d3ReheatSimulation();
  clearTimeout(state.fitTimer);
  state.fitTimer = setTimeout(fitGraph, 1500); // and again once it settles
}

function syncGrid({ k, x, y }) {
  const pad = document.querySelector(".pad");
  let cell = GRID_CELL * k;
  while (cell < 12) cell *= 5;
  while (cell > 60) cell /= 5;
  pad.style.setProperty("--cell", `${cell}px`);
  pad.style.setProperty("--x", `${x}px`);
  pad.style.setProperty("--y", `${y}px`);
}

function initGraph() {
  if (typeof ForceGraph === "undefined") {
    say("The graph library did not load. Reload the page.", true);
    return;
  }
  readPalette();
  const container = $("graph");
  const graph = ForceGraph()(container)
    .width(container.clientWidth)
    .height(container.clientHeight)
    .backgroundColor("rgba(0, 0, 0, 0)")
    .minZoom(0.05)
    .maxZoom(40)
    .nodeLabel((node) => `${node.label}: ${plain(node.title)}`)
    .nodeCanvasObject(drawNode)
    .nodePointerAreaPaint(paintHitArea)
    .linkColor((link) =>
      linkIsLit(link)
        ? state.palette.ink
        : withAlpha(state.palette.pencil, state.view === "timeline" ? 0.12 : 0.3)
    )
    .linkWidth((link) => (linkIsLit(link) ? 1.6 : 0.6))
    .linkDirectionalArrowLength((link) => (linkIsLit(link) ? 4 : 2))
    .linkDirectionalArrowRelPos(1)
    .linkDirectionalArrowColor((link) => (linkIsLit(link) ? state.palette.ink : withAlpha(state.palette.pencil, 0.4)))
    .onRenderFramePost(drawLabels)
    .onBackgroundClick(() => {
      if (state.highlight && !$("graph-search").value) {
        state.highlight = null; // clears a highlight from the key, an author or new papers
        redraw();
      }
    })
    .onNodeClick((node) => selectPaper(node.id))
    .onNodeHover((node) => {
      state.hoverId = node ? node.id : null;
      container.style.cursor = node ? "pointer" : "";
      redraw();
    })
    .onZoom(syncGrid)
    .cooldownTime(6000)
    .onEngineStop(() => {
      if (state.fitPending) fitGraph();
      state.fitPending = false;
      if (state.view === "map") savePositions(graph.graphData().nodes);
    });
  state.graph = graph;
  state.centerForce = graph.d3Force("center");
  container.append($("graph-legend")); // force-graph empties its container when it starts
  new ResizeObserver(() => graph.width(container.clientWidth).height(container.clientHeight)).observe(container);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    readPalette();
    renderLegend(graph.graphData().nodes);
    redraw();
  });
}

function savePositions(nodes) {
  const positions = nodes
    .filter((node) => Number.isFinite(node.x))
    .map((node) => ({ id: node.id, x: node.x, y: node.y }));
  for (const position of positions) state.positions.set(position.id, position);
  if (positions.length) attempt(() => api(projectPath("/positions"), { positions }));
}

function fitGraph() {
  if (state.graph?.graphData().nodes.length) state.graph.zoomToFit(400, FIT_PADDING);
}

function padMessage(nodes) {
  let message = "";
  if (!state.projectId) message = "Create a project, then add a paper to place it here.";
  else if (!nodes && state.listMode === "recent") message = "No papers from these years yet.";
  else if (!nodes) message = "Add a DOI, arXiv ID or exact title to place your first paper.";
  $("pad-empty").textContent = message;
}

function graphQuery() {
  const params = new URLSearchParams({ limit: GRAPH_LIMIT });
  if (state.justAdded.length) params.set("include", state.justAdded.join(","));
  if (state.listMode === "added") params.set("mode", "added");
  if (state.listMode === "recent") {
    params.set("mode", "recent");
    params.set("min_year", state.recentSince);
  }
  return params;
}

async function loadGraph() {
  const graph = state.graph;
  if (!graph) return;
  if (!state.projectId) {
    graph.graphData({ nodes: [], links: [] });
    padMessage(0);
    return;
  }
  const data = await api(projectPath(`/graph?${graphQuery()}`));
  const previous = new Map(graph.graphData().nodes.map((node) => [node.id, node]));
  state.neighbours = new Map(data.nodes.map((node) => [node.id, new Set()]));
  for (const edge of data.edges) {
    state.neighbours.get(edge.source)?.add(edge.target);
    state.neighbours.get(edge.target)?.add(edge.source);
  }

  // Papers keep where they were, on screen or as saved, and new ones start next
  // to a placed neighbour, so the map settles instead of reshuffling.
  for (const node of data.nodes) {
    const known = previous.get(node.id) || state.positions.get(node.id) || (node.x !== null ? node : null);
    if (known && Number.isFinite(known.x)) {
      node.x = known.x;
      node.y = known.y;
    }
  }
  for (const node of data.nodes) {
    if (Number.isFinite(node.x)) continue;
    const anchor = [...state.neighbours.get(node.id)]
      .map((id) => data.nodes.find((other) => other.id === id))
      .find((other) => other && Number.isFinite(other.x));
    if (anchor) {
      const angle = Math.random() * 2 * Math.PI;
      node.x = anchor.x + 30 * Math.cos(angle);
      node.y = anchor.y + 30 * Math.sin(angle);
    } else {
      delete node.x;
      delete node.y;
    }
  }
  rankSizes(data.nodes);
  state.legendFocus = null;
  assignCategories(data.nodes);
  const words = $("graph-search").value.toLowerCase().split(/\s+/).filter(Boolean);
  state.highlight = words.length
    ? new Set(data.nodes.filter((node) => matchesQuery(node, words)).map((node) => node.id))
    : null;
  graph.graphData({
    nodes: data.nodes,
    links: data.edges.map((edge) => ({ source: edge.source, target: edge.target })),
  });
  configureForces();
  padMessage(data.nodes.length);
}

function setView(view) {
  if (view === "map" && state.view !== "map" && state.graph) {
    for (const node of state.graph.graphData().nodes) {
      const saved = state.positions.get(node.id);
      if (saved) Object.assign(node, { x: saved.x, y: saved.y, vx: 0, vy: 0 });
    }
  }
  state.view = view;
  $("view-map").setAttribute("aria-pressed", String(view === "map"));
  $("view-timeline").setAttribute("aria-pressed", String(view === "timeline"));
  configureForces();
}

function setSizeBy(metric) {
  state.sizeBy = metric;
  rankSizes(state.graph?.graphData().nodes || []);
  configureForces();
  redraw();
}

function highlightSelection() {
  redraw();
}

function updateNodeState(workId, decision) {
  const node = state.graph?.graphData().nodes.find((item) => item.id === workId);
  if (node) node.state = decision;
  redraw();
}

/* Finding a paper */

const SEARCH_SHOWN = 8;
const SEARCH_DELAY_MS = 150;

function matchesQuery(node, words) {
  const text = `${node.title} ${node.label} ${(node.authors || []).join(" ")} ${node.year || ""}`.toLowerCase();
  return words.every((word) => text.includes(word));
}

// Papers on the graph that don't match fade as you type, and the list below the
// box offers matches from the whole project.
async function runSearch(query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const nodes = state.graph?.graphData().nodes || [];
  state.highlight = words.length
    ? new Set(nodes.filter((node) => matchesQuery(node, words)).map((node) => node.id))
    : null;
  redraw();
  if (!words.length || !state.projectId) {
    renderSearchResults([]);
    return;
  }
  const params = new URLSearchParams({ q: query, limit: SEARCH_SHOWN });
  const { items } = await api(projectPath(`/search?${params}`));
  if ($("graph-search").value.trim() !== query) return; // a newer query is on its way
  // The server also matches authors beyond the few each graph node carries.
  for (const item of items) if (onGraph(item.id)) state.highlight.add(item.id);
  redraw();
  renderSearchResults(items);
}

function onGraph(workId) {
  return Boolean(state.graph?.graphData().nodes.some((node) => node.id === workId));
}

function renderSearchResults(items) {
  state.searchResults = items;
  state.searchActive = items.length ? 0 : -1;
  const list = $("search-results");
  const query = $("graph-search").value.trim();
  const rows = items.map((item, index) =>
    el(
      "li",
      {
        id: `search-result-${index}`,
        role: "option",
        "aria-selected": String(index === state.searchActive),
        onmousedown: (event) => {
          event.preventDefault(); // keep focus in the box until the choice is made
          chooseSearchResult(index);
        },
      },
      el("span", { class: "result-title" }, plain(item.title)),
      el(
        "span",
        { class: "result-meta" },
        [item.label, STATE_NAMES[item.state] || "To review", onGraph(item.id) ? "on the graph" : "not on the graph"].join(" · ")
      )
    )
  );
  if (query && !items.length) rows.push(el("li", { class: "result-meta" }, "No paper in this project matches."));
  list.replaceChildren(...rows);
  list.hidden = !rows.length;
  $("graph-search").setAttribute("aria-expanded", String(!list.hidden));
  $("graph-search").setAttribute("aria-activedescendant", items.length ? "search-result-0" : "");
}

function moveSearchActive(step) {
  const count = state.searchResults.length;
  if (!count) return;
  state.searchActive = (state.searchActive + step + count) % count;
  $("search-results").querySelectorAll("li[role=option]").forEach((row, index) => {
    row.setAttribute("aria-selected", String(index === state.searchActive));
    if (index === state.searchActive) row.scrollIntoView({ block: "nearest" });
  });
  $("graph-search").setAttribute("aria-activedescendant", `search-result-${state.searchActive}`);
}

function focusNode(workId) {
  const node = state.graph?.graphData().nodes.find((item) => item.id === workId);
  if (!node || !Number.isFinite(node.x)) return false;
  state.graph.centerAt(node.x, node.y, 600);
  state.graph.zoom(Math.max(state.graph.zoom(), 3), 600);
  return true;
}

function chooseSearchResult(index) {
  const item = state.searchResults[index];
  if (!item) return;
  $("search-results").hidden = true;
  $("graph-search").setAttribute("aria-expanded", "false");
  if (!focusNode(item.id)) {
    say(`${plain(item.title)} is not on the graph in this view, so only its details are shown.`);
  }
  selectPaper(item.id);
}

function clearSearch() {
  $("graph-search").value = "";
  state.highlight = null;
  renderSearchResults([]);
  redraw();
}

function bindSearch() {
  const box = $("graph-search");
  let timer = null;
  box.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => attempt(() => runSearch(box.value.trim())), SEARCH_DELAY_MS);
  });
  box.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") moveSearchActive(1);
    else if (event.key === "ArrowUp") moveSearchActive(-1);
    else if (event.key === "Enter") chooseSearchResult(Math.max(state.searchActive, 0));
    else if (event.key === "Escape") {
      clearSearch();
      box.blur();
    } else return;
    event.preventDefault();
  });
  box.addEventListener("blur", () => {
    $("search-results").hidden = true;
    box.setAttribute("aria-expanded", "false");
  });
  box.addEventListener("focus", () => {
    if (state.searchResults.length) $("search-results").hidden = false;
  });
}

/* Paper details */

function renderDetailsEmpty() {
  state.selectedId = null;
  markSelectedRow();
  highlightSelection();
  $("paper-details").replaceChildren(
    el("p", { class: "empty" }, "Select a paper to read its abstract and see why it was suggested.")
  );
}

function detailLinks(work) {
  const links = [];
  if (work.doi) {
    links.push(el("a", { href: `https://doi.org/${work.doi}`, target: "_blank", rel: "noreferrer" }, "Publisher page"));
  }
  if (work.arxiv) {
    links.push(el("a", { href: `https://arxiv.org/abs/${work.arxiv}`, target: "_blank", rel: "noreferrer" }, "arXiv"));
  }
  return links.length ? el("div", { class: "detail-block links" }, ...links) : null;
}

function linkedList(heading, papers) {
  if (!papers || !papers.length) return null;
  return el(
    "div",
    { class: "detail-block" },
    el("h3", {}, heading),
    el("ul", {}, ...papers.map((paper) => el("li", {}, plain(paper.title))))
  );
}

function whySection(item) {
  if (!item) return null;
  return el(
    "div",
    { class: "detail-block" },
    el("h3", {}, item.explore ? "Why this paper (exploration pick)" : "Why this paper"),
    item.reasons.length
      ? el("ul", {}, ...item.reasons.map((reason) => el("li", {}, reason)))
      : el("p", { class: "muted" }, "It is connected to your papers through the wider graph.")
  );
}

function ieeeSection(work) {
  if (work.ieee) {
    const record = work.ieee;
    return el(
      "div",
      { class: "detail-block" },
      el("h3", {}, "IEEE Xplore"),
      el("p", {}, `Cited by ${plural(record.citing_paper_count ?? 0, "paper")} in IEEE Xplore.`),
      record.ieee_terms.length
        ? el("p", { class: "ieee-note" }, record.ieee_terms.join(", "))
        : null
    );
  }
  if (!work.ieee_available) return null;

  const hasKey = state.health && state.health.ieee_key;
  const limit = state.health ? state.health.limits.ieee_daily_calls : 180;
  return el(
    "div",
    { class: "detail-block" },
    el("h3", {}, "IEEE Xplore"),
    el(
      "button",
      {
        type: "button",
        ...(hasKey ? {} : { disabled: "" }),
        onclick: () => fetchIeee(work.id),
      },
      "Get IEEE details"
    ),
    el(
      "p",
      { class: "ieee-note" },
      hasKey
        ? `Uses 1 of your ${limit} daily IEEE calls.`
        : "Add IEEE_API_KEY to your .env file to use this."
    )
  );
}

async function fetchIeee(workId) {
  await attempt(async () => {
    const { calls_today: calls } = await api(`/api/works/${workId}/ieee`, {});
    say(`IEEE details fetched. ${plural(calls, "call")} used today.`);
    await loadHealth();
    await selectPaper(workId);
  });
}

async function selectPaper(workId) {
  state.selectedId = workId;
  markSelectedRow();
  highlightSelection();
  document
    .querySelector(`.paper-row[data-id="${workId}"]`)
    ?.scrollIntoView({ block: "nearest" });

  await attempt(async () => {
    const work = await api(projectPath(`/works/${workId}`));
    if (state.selectedId !== workId) return;
    const item = state.items.find((entry) => entry.id === workId);
    const venue = [work.venue, work.year].filter(Boolean).join(", ");
    const mine = work.mine.cited_in.length
      ? el("p", { class: "tag-cited detail-venue" }, `You cited this in ${work.mine.cited_in.join(", ")}.`)
      : null;
    const actions = work.state
      ? el(
          "div",
          { class: "detail-actions" },
          el("button", { type: "button", onclick: () => decide(workId, "accepted") }, "Accept"),
          el("button", { type: "button", onclick: () => decide(workId, "rejected") }, "Reject"),
          el("button", { type: "button", onclick: () => decide(workId, "read") }, "Mark read")
        )
      : null;

    const tags = [
      STATE_NAMES[work.state] ? el("span", { class: "tag" }, STATE_NAMES[work.state]) : null,
      work.rank ? el("span", { class: "tag tag-rank" }, rankLabel(work.rank, true)) : null,
      impactTag(work.journal),
    ].filter(Boolean);

    const sections = [
      el("h3", { class: "detail-title" }, plain(work.title)),
      el("p", { class: "detail-authors" }, work.authors.join(", ") || "Authors unknown"),
      venue ? el("p", { class: "detail-venue" }, venue) : null,
      tags.length ? el("div", { class: "tags" }, ...tags) : null,
      mine,
      actions,
      whySection(item),
      linkedList("Cites these project papers", item?.linked.cites),
      linkedList("Cited by these project papers", item?.linked.cited_by),
      el(
        "div",
        { class: "detail-block" },
        el("h3", {}, "Abstract"),
        work.abstract
          ? el("p", { class: "abstract" }, plain(work.abstract))
          : el("p", { class: "muted" }, "OpenAlex has no abstract for this paper.")
      ),
      el(
        "div",
        { class: "detail-block" },
        el("h3", {}, "Citations"),
        el("p", {}, `Cited by ${plural(work.cited_by ?? 0, "paper")} in OpenAlex.`),
        work.level !== "stub" && !work.n_refs
          ? el("p", { class: "muted" }, "OpenAlex has no reference list for this paper.")
          : null,
        citersControls(work)
      ),
      ieeeSection(work),
      detailLinks(work),
    ];
    $("paper-details").replaceChildren(...sections.filter(Boolean));
  });
}

/* Papers citing one paper */

const CITER_CHOICES = [["10", "best 10"], ["25", "best 25"], ["50", "best 50"], ["all", "all"]];
const CITER_ORDERS = [["newest", "of the newest"], ["most_cited", "of the most cited"]];

function citersControls(work) {
  const result = el("div", { class: "citers-result" });
  const count = el(
    "select",
    { "aria-label": "How many citing papers to add" },
    ...CITER_CHOICES.map(([value, label]) => el("option", { value }, label))
  );
  const order = el(
    "select",
    { "aria-label": "Which citing papers to fetch when there are more than 500" },
    ...CITER_ORDERS.map(([value, label]) => el("option", { value }, label))
  );
  const button = el("button", { type: "button" }, "Find citing papers");
  button.addEventListener("click", () =>
    attempt(() => findCiters(work, count.value, order.value, button, result))
  );
  return el(
    "div",
    { class: "citers" },
    el("div", { class: "citers-row" }, button, el("span", { class: "muted" }, "add"), count, order),
    el(
      "p",
      { class: "muted" },
      "Fetches up to 500 papers citing this one, the newest or the most cited, then adds the best not yet in the project to review. Asking again adds the next best."
    ),
    result
  );
}

async function findCiters(work, choice, order, button, result) {
  button.disabled = true;
  button.textContent = "Finding...";
  say(`Looking for papers citing ${plain(work.title)}...`);
  try {
    const body = { limit: choice === "all" ? null : Number(choice), order };
    const report = await api(projectPath(`/works/${work.id}/citers`), body);
    const parts = [];
    if (!report.stored) {
      parts.push("OpenAlex lists no papers citing it yet.");
    } else {
      parts.push(`Added ${plural(report.added.length, "citing paper")} to review.`);
      if (report.already) parts.push(`${report.already} were already in the project.`);
      if (report.capped) {
        parts.push(`OpenAlex lists ${report.total}, and ${report.stored} of them are stored and were checked.`);
      }
      if (report.added.length) parts.push("They are highlighted on the map. Click empty space to clear.");
    }
    parts.push(`Cost $${report.cost_usd.toFixed(4)}.`);

    state.justAdded = [...new Set([...state.justAdded, ...report.added])];
    if (state.listMode === "added" || state.listMode === "authors") await setListMode("suggestions");
    await refresh();
    state.highlight = report.added.length ? new Set([work.id, ...report.added]) : null;
    redraw();
    renderAddedCiters(report.added, result);
    say(parts.join(" "));
  } finally {
    button.disabled = false;
    button.textContent = "Find citing papers";
  }
}

function renderAddedCiters(ids, result) {
  const nodes = new Map((state.graph?.graphData().nodes || []).map((node) => [node.id, node]));
  const rows = ids.map((id) => {
    const node = nodes.get(id);
    const label = node ? `${node.label}: ${plain(node.title)}` : `Paper ${id}`;
    return el("li", {}, el("button", { type: "button", class: "link-button", onclick: () => selectPaper(id) }, label));
  });
  result.replaceChildren(
    ...(rows.length ? [el("p", {}, "Added to review:"), el("ul", { class: "author-papers" }, ...rows)] : [])
  );
}

/* Actions */

async function addSeed(identifier) {
  say("Adding seed...");
  const report = await api(projectPath("/seeds"), { identifiers: [identifier] });
  if (report.unresolved.length) {
    say(`No match found for ${identifier}. Check the DOI or arXiv ID, or paste the exact title.`, true);
    return false;
  }
  say(
    report.no_references.length
      ? "Seed added. OpenAlex has no references for it, so its suggestions will come from papers citing it."
      : "Seed added. Press Update graph to add its best neighbours."
  );
  await refresh();
  return true;
}

async function expand() {
  const button = $("expand-button");
  button.disabled = true;
  button.textContent = "Updating...";
  say("Adding the best neighbours of new papers. Many new papers can take a minute.");
  try {
    const report = await api(projectPath("/expand"), {});
    const parts = [`Added ${plural(report.frontier_added, "paper")} to review.`];
    if (report.refs_filled) {
      parts.push(`Filled missing reference lists for ${plural(report.refs_filled, "paper")} from arXiv.`);
    }
    parts.push(`Cost $${report.cost_usd.toFixed(4)}.`);
    if (report.cap_hit.length) {
      parts.push(`${plural(report.cap_hit.length, "paper")} reached the citing-paper limit.`);
    }
    say(parts.join(" "));
    await refresh();
  } finally {
    renderProjects();
  }
}

async function checkNewPapers() {
  const button = $("update-button");
  button.disabled = true;
  say("Checking for new citing papers...");
  try {
    const report = await api(projectPath("/update"), {});
    const parts = [
      `${plural(report.new_papers.length, "new paper")} since ${report.since}.`,
      `Cost $${report.cost_usd.toFixed(4)}.`,
    ];
    if (report.capped) parts.push("Stopped at the limit, so press Update graph for a full refresh.");
    say(parts.join(" "));
    await refresh();
  } finally {
    button.disabled = !state.projectId;
  }
}

function renderTargets(targets) {
  $("target-list").replaceChildren(
    ...targets.map((target) =>
      resultRow(
        `${target.rank}  ${target.title}${target.acronym ? ` (${target.acronym})` : ""}`,
        "",
        "Remove",
        () =>
          attempt(async () => {
            const params = new URLSearchParams({ title: target.title });
            renderTargets((await api(`/api/venue-targets?${params}`, undefined, "DELETE")).targets);
            await refresh();
          })
      )
    )
  );
}

async function loadTargets() {
  renderTargets((await api("/api/venue-targets")).targets);
}

async function addTarget() {
  const body = {
    title: $("target-name").value.trim(),
    acronym: $("target-acronym").value.trim() || null,
    rank: $("target-rank").value,
  };
  renderTargets((await api("/api/venue-targets", body)).targets);
  $("target-name").value = "";
  $("target-acronym").value = "";
  say(`${body.title} now counts as ${body.rank}.`);
  await refresh();
}

const WEIGHT_NAMES = {
  bc: "shared references",
  cc: "cited together",
  ppr: "graph walk",
  imp: "field importance",
  venue: "venue rank",
};

async function loadWeights() {
  if (!state.projectId) return;
  const { weights, tuned, min_seeds: minSeeds, papers } = await api(projectPath("/weights"));
  const parts = Object.entries(WEIGHT_NAMES).map(([key, name]) => `${name} ${weights[key]}`);
  const origin = tuned
    ? `Fitted to your seeds on ${tuned.at.slice(0, 10)}: they find ${Math.round(tuned.recall * 100)}% of hidden seeds, against ${Math.round(tuned.default_recall * 100)}% for the defaults.`
    : papers >= minSeeds
      ? `Default weights. This project's ${papers} seed and accepted papers are enough to fit them.`
      : `Default weights. Fitting needs ${minSeeds} seed or accepted papers, and this project has ${papers}.`;
  $("weights-text").textContent = `${parts.join(", ")}. ${origin}`;
}

async function tuneWeights() {
  const button = $("tune-button");
  button.disabled = true;
  say("Fitting ranking weights to your seed papers. This can take several minutes.");
  try {
    const result = await api(projectPath("/tune"), {});
    say(
      `Saved new weights. They find ${Math.round(result.recall * 100)}% of hidden seeds, ` +
        `against ${Math.round(result.default_recall * 100)}% for the defaults.`
    );
    await Promise.all([loadWeights(), refresh()]);
  } finally {
    button.disabled = false;
  }
}

function resultRow(primary, secondary, buttonText, onclick) {
  return el(
    "li",
    {},
    el("div", {}, el("div", {}, plain(primary)), el("div", { class: "muted" }, secondary)),
    el("button", { type: "button", onclick }, buttonText)
  );
}

async function findVenue() {
  const name = $("venue-name").value.trim();
  if (!name) {
    say("Enter a venue name or acronym.", true);
    return;
  }
  const { venues } = await api("/api/venues/search", { name });
  const since = Number($("venue-since").value);
  $("venue-results").replaceChildren(
    ...venues.map((venue) =>
      resultRow(
        venue.name,
        `${venue.type || "Venue"}, ${plural(venue.works_count ?? 0, "paper")}`,
        "Add papers",
        () => attempt(() => addVenue(venue, since))
      )
    )
  );
  say(venues.length ? "Choose the matching venue." : `No venues found for ${name}.`, !venues.length);
}

async function addVenue(venue, since) {
  const { added } = await api(projectPath("/venues"), { venue_id: venue.id, since_year: since });
  say(`Added ${plural(added, "paper")} from ${venue.name} to review.`);
  await refresh();
}

async function searchIeee() {
  const query = $("ieee-query").value.trim();
  if (!query) {
    say("Enter search terms.", true);
    return;
  }
  const { results, calls_today: calls } = await api("/api/ieee/search", { query });
  $("ieee-results").replaceChildren(
    ...results.map((record) =>
      resultRow(record.title, record.doi, "Add seed", () =>
        attempt(() => addSeed(record.doi))
      )
    )
  );
  say(`Found ${plural(results.length, "paper")}. ${plural(calls, "IEEE call")} used today.`);
  await loadHealth();
}

function moveSelection(step) {
  if (!state.items.length) return;
  const index = state.items.findIndex((item) => item.id === state.selectedId);
  const next = Math.min(Math.max(index + step, 0), state.items.length - 1);
  selectPaper(state.items[index < 0 ? 0 : next].id);
}

function handleShortcut(event) {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target.closest("input, select, textarea")) return;
  if (event.key === "/") {
    event.preventDefault();
    $("graph-search").focus();
    return;
  }
  const decisions = { a: "accepted", r: "rejected", m: "read" };
  if (event.key === "j" || event.key === "ArrowDown") {
    event.preventDefault();
    moveSelection(1);
  } else if (event.key === "k" || event.key === "ArrowUp") {
    event.preventDefault();
    moveSelection(-1);
  } else if (decisions[event.key] && state.selectedId) {
    decide(state.selectedId, decisions[event.key]);
  }
}

function bindEvents() {
  $("project-select").addEventListener("change", (event) => {
    if (event.target.value === NEW_PROJECT) {
      $("project-form").hidden = false;
      $("project-name").focus();
      event.target.value = state.projectId ? String(state.projectId) : "";
      return;
    }
    attempt(() => switchProject(Number(event.target.value)));
  });

  $("project-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const name = $("project-name").value.trim();
    if (!name) {
      say("Enter a project name.", true);
      return;
    }
    attempt(async () => {
      const project = await api("/api/projects", { name });
      $("project-name").value = "";
      say(`Created ${project.name}. Add a seed to begin.`);
      await switchProject(project.id);
    });
  });

  $("project-cancel").addEventListener("click", () => {
    if (state.projects.length) $("project-form").hidden = true;
  });

  $("seed-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("seed-input");
    const identifier = input.value.trim();
    if (!identifier) {
      say("Enter a DOI, arXiv ID or exact title.", true);
      return;
    }
    if (!state.projectId) {
      say("Create a project first.", true);
      return;
    }
    attempt(async () => {
      if (await addSeed(identifier)) input.value = "";
    });
  });

  $("expand-button").addEventListener("click", () => attempt(expand));
  $("list-suggestions").addEventListener("click", () => attempt(() => setListMode("suggestions")));
  $("list-added").addEventListener("click", () => attempt(() => setListMode("added")));
  $("list-recent").addEventListener("click", () => attempt(() => setListMode("recent")));
  $("list-authors").addEventListener("click", () => attempt(() => setListMode("authors")));
  $("colour-by").addEventListener("change", (event) => setColourBy(event.target.value));
  $("tune-button").addEventListener("click", () => attempt(tuneWeights));
  $("targets-drawer").addEventListener("toggle", (event) => {
    if (event.target.open) attempt(loadTargets);
  });
  $("target-form").addEventListener("submit", (event) => {
    event.preventDefault();
    attempt(addTarget);
  });
  $("weights-drawer").addEventListener("toggle", (event) => {
    if (event.target.open) attempt(loadWeights);
  });
  $("recent-since").addEventListener("change", (event) => {
    state.recentSince = Number(event.target.value);
    attempt(() => Promise.all([loadList(), loadGraph()]));
  });
  $("size-by").addEventListener("change", (event) => setSizeBy(event.target.value));
  bindSearch();
  $("update-button").addEventListener("click", () => attempt(checkNewPapers));
  $("venue-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!state.projectId) {
      say("Create a project first.", true);
      return;
    }
    attempt(findVenue);
  });
  $("ieee-form").addEventListener("submit", (event) => {
    event.preventDefault();
    attempt(searchIeee);
  });

  for (const id of ["hide-cited", "hide-library", "min-rank", "min-impact", "sort-by"]) {
    $(id).addEventListener("change", () => attempt(loadFrontier));
  }

  $("view-map").addEventListener("click", () => setView("map"));
  $("view-timeline").addEventListener("click", () => setView("timeline"));
  $("view-fit").addEventListener("click", fitGraph);
  document.addEventListener("keydown", handleShortcut);
}

async function start() {
  const year = new Date().getFullYear();
  $("venue-since").value = year - 5;
  $("recent-since").replaceChildren(
    ...[year - 2, year - 1, year].map((option) => el("option", { value: option }, `Since ${option}`))
  );
  $("recent-since").value = state.recentSince;
  bindEvents();
  initGraph();
  const started = await attempt(async () => {
    await refresh();
    return true;
  });
  if (started && !$("status").textContent) {
    say(
      state.health.openalex_key
        ? "Connected."
        : "Add OPENALEX_API_KEY to your .env file for the full daily allowance."
    );
  }
}

start();
