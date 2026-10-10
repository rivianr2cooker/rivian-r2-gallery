// R2 Spotter: static, client-side filtering over data/images.json.
const MIN_TAG = 0.6;    // how sure it must be about a paint/wheel/interior tag
const STRICT_TAG = 0.8; // ...with "High confidence only" checked
const PAGE = 48;

const SOURCES = { reddit: "Reddit", forum: "Forums" };

const MODES = {
  exterior: { groups: ["paint"], titles: { paint: "Paint" } },
  interior: { groups: ["interior"], titles: { interior: "Interior" } },
};

let OPTIONS, IMAGES = [], LABELS = {};
const DEFAULT_HIDE = ["forum"];  // sources unticked unless the link says otherwise
let state = { mode: "exterior", sel: {}, strict: true, hide: new Set(DEFAULT_HIDE) }, shown = 0, lists = { exact: [], partial: [] };

const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v != null) n.setAttribute(k, v);
  }
  n.append(...kids.filter((k) => k != null));
  return n;
};

// A tag counts only if the tagger was confident and didn't answer "unknown".
function known(img, group) {
  const t = img.tags?.[group];
  return t && t.value !== "unknown" && t.confidence >= (state.strict ? STRICT_TAG : MIN_TAG) ? t.value : null;
}

function inMode(img, mode) {
  if (state.hide.has(img.source) || img.view === "other" || img.render >= 0.8) return false;  // render: configurator/press/CGI (0.5-0.8 had real photos)
  if (img.pii && (img.pii.face >= 0.5 || img.pii.info >= 0.5)) return false;  // identifiable face or personal info
  if (mode === "interior") return !!known(img, "interior") && img.view !== "exterior";
  return img.view !== "interior" && !!known(img, "paint");
}

// exact: every chosen option is visible and matches.
// partial: nothing contradicts, at least one chosen option matches, the rest can't be seen.
function classify(img) {
  const chosen = MODES[state.mode].groups.filter((g) => state.sel[g]);
  if (!chosen.length) return "exact";
  let matched = 0;
  for (const g of chosen) {
    const v = known(img, g);
    if (v === null) continue;
    if (v !== state.sel[g]) return null;
    matched++;
  }
  if (matched === chosen.length) return "exact";
  return matched > 0 ? "partial" : null;
}

function readHash() {
  const [mode, q] = location.hash.slice(1).split("?");
  state.mode = MODES[mode] ? mode : "exterior";
  const params = new URLSearchParams(q || "");
  state.strict = params.get("hc") !== "0";  // on by default
  params.delete("hc");
  state.hide = params.has("hide") ? new Set(params.get("hide").split(",").filter((s) => SOURCES[s])) : new Set(DEFAULT_HIDE);
  params.delete("hide");
  state.sel = Object.fromEntries(params);
}
function writeHash() {
  const q = new URLSearchParams(Object.entries(state.sel).filter(([g, v]) => v && MODES[state.mode].groups.includes(g)));
  if (!state.strict) q.set("hc", "0");
  const hide = [...state.hide].sort().join(",");
  if (hide !== [...DEFAULT_HIDE].sort().join(",")) q.set("hide", hide);  // "hide=" means show every source
  history.replaceState(null, "", `#${state.mode}${q.toString() ? "?" + q : ""}`);
}

function renderFilters() {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.mode === state.mode));
  const box = $("#filters");
  box.replaceChildren();
  const strict = el("input", { type: "checkbox", role: "switch", onchange: (e) => { state.strict = e.target.checked; writeHash(); renderFilters(); renderResults(); } });
  strict.checked = state.strict;
  box.append(el("label", { class: "conf" + (state.strict ? " on" : "") }, strict,
    el("span", {}, el("strong", {}, `High confidence only (${STRICT_TAG * 100}%+)`),
      el("span", { class: "hint" }, "Only photos where the tagger is that sure of the color"))));
  const srcs = Object.entries(SOURCES).map(([s, label]) => {
    const cb = el("input", { type: "checkbox", onchange: (e) => {
      e.target.checked ? state.hide.delete(s) : state.hide.add(s); writeHash(); renderResults(); } });
    cb.checked = !state.hide.has(s);
    return el("label", { class: "src" }, cb, label);
  });
  box.append(el("div", { class: "group sources" }, el("h3", {}, "Source"), el("div", { class: "opts" }, ...srcs)));
  for (const g of MODES[state.mode].groups) {
    const pick = (v) => () => { state.sel[g] = v; writeHash(); renderFilters(); renderResults(); };
    const opts = [el("button", { class: "opt", "aria-pressed": !state.sel[g], onclick: pick(null) }, "Any")];
    for (const o of OPTIONS[g]) {
      opts.push(el("button", { class: "opt", "aria-pressed": state.sel[g] === o.value, onclick: pick(o.value) },
        o.swatch ? el("span", { class: "sw", style: `background:${o.swatch}` }) : null, o.label));
    }
    box.append(el("div", { class: "group" }, el("h3", {}, MODES[state.mode].titles[g]), el("div", { class: "opts" }, ...opts)));
  }
}

function card(img) {
  const tags = MODES[state.mode].groups.map((g) => {
    const v = known(img, g);
    return el("span", { class: "tag" + (v ? "" : " unk") }, v ? LABELS[g][v] : `${MODES[state.mode].titles[g]} not visible`);
  });
  const src = img.thumb || img.image_url;
  const image = el("img", { src, alt: `${img.title} (photo by ${img.author})`, loading: "lazy", referrerpolicy: "no-referrer" });
  const c = el("article", { class: "card" },
    el("a", { class: "ph", href: img.post_url, target: "_blank", rel: "noopener" }, image),
    el("div", { class: "meta" },
      el("div", { class: "title", title: img.title }, img.title || "Untitled post"),
      el("div", { class: "tags" }, ...tags),
      el("div", { class: "credit" },
        el("span", {}, "by ", img.author_url ? el("a", { href: img.author_url, target: "_blank", rel: "noopener" }, img.author) : img.author),
        el("a", { href: img.post_url, target: "_blank", rel: "noopener" }, img.source === "reddit" ? "Reddit post" : "Forum post"))));
  // Sites that block embedding: drop the card rather than show a broken image.
  image.addEventListener("error", () => c.remove());
  return c;
}

function renderResults() {
  lists = { exact: [], partial: [] };
  for (const img of IMAGES) {
    if (!inMode(img, state.mode)) continue;
    const k = classify(img);
    if (k) lists[k].push(img);
  }
  $("#exact").replaceChildren();
  $("#partial").replaceChildren();
  shown = 0;
  const any = MODES[state.mode].groups.some((g) => state.sel[g]);
  $("#summary").textContent = IMAGES.length === 0 ? "" :
    `${lists.exact.length} ${any ? "matching " : ""}photo${lists.exact.length === 1 ? "" : "s"}` +
    (lists.partial.length ? `, plus ${lists.partial.length} partial match${lists.partial.length === 1 ? "" : "es"}` : "");
  const ph = $("#partial-head");
  ph.hidden = !lists.partial.length;
  ph.textContent = "Partial matches";
  if (!lists.exact.length) {
    $("#exact").append(el("p", { class: "empty" }, IMAGES.length ? "No exact matches yet for this configuration." : "No photos have been tagged yet."));
  }
  more();
}

// Fill exact matches first, then partial, PAGE cards at a time.
function more() {
  const all = [...lists.exact.map((i) => ["exact", i]), ...lists.partial.map((i) => ["partial", i])];
  for (const [k, img] of all.slice(shown, shown + PAGE)) $("#" + k).append(card(img));
  shown = Math.min(shown + PAGE, all.length);
  $("#more").hidden = shown >= all.length;
}

async function main() {
  const [opts, data] = await Promise.all([
    fetch("data/options.json").then((r) => r.json()),
    fetch("data/images.json").then((r) => (r.ok ? r.json() : { images: [] })).catch(() => ({ images: [] })),
  ]);
  OPTIONS = opts;
  IMAGES = data.images || [];
  for (const g of ["paint", "wheels", "interior"]) LABELS[g] = Object.fromEntries(OPTIONS[g].map((o) => [o.value, o.label]));
  if (data.generated) $("#updated").textContent = `Last updated ${new Date(data.generated).toLocaleDateString()}. ${IMAGES.length} photos tagged.`;

  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
    state.mode = b.dataset.mode; writeHash(); renderFilters(); renderResults();
  }));
  $("#more").addEventListener("click", more);
  readHash();
  renderFilters();
  renderResults();
}
main();
