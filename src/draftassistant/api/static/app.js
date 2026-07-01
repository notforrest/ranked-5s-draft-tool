"use strict";

/* ============================================================
   Tiny fetch helper
   ============================================================ */
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try {
    data = await res.json();
  } catch (_e) {
    /* empty body is fine */
  }
  if (!res.ok) {
    const detail = (data && data.detail) || res.statusText || "Request failed";
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

const LOCAL_STORAGE_KEY = "draftAssistant.draftSessionId";

const VALID_ROLES = ["TOP", "JUNGLE", "MID", "BOTTOM", "SUPPORT"];

/* ============================================================
   Global app state
   ============================================================ */
const state = {
  champions: [],          // list of {champion_id, champion_key, name, tags, icon_path, ...}
  champById: new Map(),   // champion_id -> champion
  roster: [],             // active players from GET /api/roster
  selectedRoles: new Map(), // player_id -> role ("" = selected but unassigned)
  ourSide: "BLUE",
  refreshOutcomeSeen: false,

  draftSessionId: null,
  draft: null,            // last full payload from create/get/enter/amend
  amendTargetSlot: null,  // slot currently open in the amend popover, or null
};

/* ============================================================
   Champion detail hover panel -- controller state
   ============================================================ */
const CHAMPION_DETAIL_HOVER_DELAY_MS = 350;  // long enough that a fast mouse pass across many
                                               // suggestion rows fires zero fetches
const CHAMPION_DETAIL_CLOSE_GRACE_MS = 150;   // lets the mouse travel row -> panel without a flicker-close

const hoverController = {
  openTimer: null,         // pending "should I open" timer
  closeTimer: null,        // pending "should I close" grace-period timer
  requestToken: 0,         // incremented on every new hover/close; a fetch whose token no
                            // longer matches when it resolves is discarded (stale response
                            // from a previously-hovered row must never overwrite the panel)
  cache: new Map(),        // "sessionId:championId:slot" -> detail response object
};

/* ============================================================
   Bootstrapping
   ============================================================ */
document.addEventListener("DOMContentLoaded", init);

async function init() {
  wireStaticHandlers();

  const savedId = localStorage.getItem(LOCAL_STORAGE_KEY);
  if (savedId) {
    try {
      const draftData = await api("GET", `/api/draft/${savedId}`);
      state.draftSessionId = Number(savedId);
      state.draft = draftData;
      // Champion data is needed to render the board/grid -- load it before switching views.
      await loadChampions();
      showDraftView();
      renderDraft();
      return;
    } catch (e) {
      // Stale/invalid id (e.g. server restarted with a fresh DB) -- fall through to setup.
      console.warn("Could not resume saved draft, starting fresh:", e);
      localStorage.removeItem(LOCAL_STORAGE_KEY);
    }
  }

  showSetupView();
  try {
    await Promise.all([loadRoster(), loadChampions(), loadLastRefresh(), loadOracleElixirStatus()]);
  } catch (e) {
    // A single failed fetch (e.g. server not fully up yet) shouldn't leave the page silently
    // half-broken with no explanation -- surface it and let the user reload.
    console.error("Failed to load initial setup data:", e);
    const banner = document.createElement("div");
    banner.className = "refresh-status error";
    banner.textContent = `Could not load initial data (${e.message}). Try reloading the page.`;
    document.querySelector(".setup-header").after(banner);
  }
}

function showSetupView() {
  document.getElementById("setup-view").hidden = false;
  document.getElementById("draft-view").hidden = true;
}

function showDraftView() {
  document.getElementById("setup-view").hidden = true;
  document.getElementById("draft-view").hidden = false;
}

/* ============================================================
   SETUP VIEW
   ============================================================ */
function wireStaticHandlers() {
  document.getElementById("refresh-btn").addEventListener("click", onRefreshClicked);
  document.getElementById("start-draft-btn").addEventListener("click", onStartDraftClicked);
  document.getElementById("new-draft-btn").addEventListener("click", onNewDraftClicked);
  document.getElementById("fetch-tierlist-btn").addEventListener("click", onFetchTierListClicked);
  document.getElementById("fetch-oe-btn").addEventListener("click", onFetchOracleElixirClicked);
  document.getElementById("oe-upload-input").addEventListener("change", onOracleElixirFileSelected);

  document.querySelectorAll(".side-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.ourSide = btn.dataset.side;
      document.querySelectorAll(".side-btn").forEach((b) => b.classList.toggle("active", b === btn));
    });
  });

  document.getElementById("champ-search").addEventListener("input", (e) => {
    renderChampGrid(e.target.value);
  });

  document.getElementById("amend-search").addEventListener("input", (e) => {
    renderAmendGrid(e.target.value);
  });
  document.getElementById("amend-popover-close").addEventListener("click", closeAmendPopover);

  // Hovering INTO the detail panel itself (e.g. to read a long section) must not immediately
  // flicker-close it -- same close-grace-period mechanism as leaving a suggestion row.
  const detailPanel = document.getElementById("champion-detail-panel");
  detailPanel.addEventListener("mouseenter", () => clearTimeout(hoverController.closeTimer));
  detailPanel.addEventListener("mouseleave", onSuggestionRowMouseLeave);
}

async function loadRoster() {
  const el = document.getElementById("roster-list");
  try {
    state.roster = await api("GET", "/api/roster");
  } catch (e) {
    el.textContent = `Failed to load roster: ${e.message}`;
    return;
  }
  if (state.roster.length === 0) {
    el.textContent = "No active players yet. Add players to the roster first.";
    return;
  }
  renderRosterList();
}

function renderRosterList() {
  const el = document.getElementById("roster-list");
  el.innerHTML = "";
  for (const player of state.roster) {
    const row = document.createElement("div");
    row.className = "roster-row";
    row.dataset.playerId = player.player_id;

    const checked = state.selectedRoles.has(player.player_id);
    row.classList.toggle("selected", checked);

    const label = document.createElement("label");

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = checked;
    checkbox.addEventListener("change", () => onPlayerToggled(player.player_id, checkbox.checked));

    const nameSpan = document.createElement("span");
    nameSpan.className = "player-name";
    nameSpan.textContent = player.display_name;

    const metaSpan = document.createElement("span");
    metaSpan.className = "player-meta";
    metaSpan.textContent = `${player.riot_game_name}#${player.riot_tag_line}`;

    label.appendChild(checkbox);
    label.appendChild(nameSpan);
    label.appendChild(metaSpan);

    const roleSelect = document.createElement("select");
    roleSelect.disabled = !checked;
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "role…";
    roleSelect.appendChild(blank);
    for (const role of VALID_ROLES) {
      const opt = document.createElement("option");
      opt.value = role;
      opt.textContent = role;
      roleSelect.appendChild(opt);
    }
    roleSelect.value = state.selectedRoles.get(player.player_id) || "";
    roleSelect.addEventListener("change", () => {
      state.selectedRoles.set(player.player_id, roleSelect.value);
      validateRoles();
    });

    row.appendChild(label);
    row.appendChild(roleSelect);
    el.appendChild(row);
  }
  validateRoles();
}

function onPlayerToggled(playerId, isChecked) {
  if (isChecked) {
    if (state.selectedRoles.size >= 5) {
      // Already at 5 -- revert this checkbox visually via a full re-render since we never added it.
      renderRosterList();
      return;
    }
    state.selectedRoles.set(playerId, "");
  } else {
    state.selectedRoles.delete(playerId);
  }
  renderRosterList();
}

function validateRoles() {
  const msgEl = document.getElementById("role-validation");
  const startBtn = document.getElementById("start-draft-btn");

  const count = state.selectedRoles.size;
  if (count !== 5) {
    msgEl.textContent = `Select exactly 5 players (currently ${count}).`;
    msgEl.classList.remove("ok");
    startBtn.disabled = true;
    return;
  }

  const roles = Array.from(state.selectedRoles.values());
  if (roles.some((r) => !r)) {
    msgEl.textContent = "Assign a role to every selected player.";
    msgEl.classList.remove("ok");
    startBtn.disabled = true;
    return;
  }

  const sortedRoles = [...roles].sort();
  const sortedValid = [...VALID_ROLES].sort();
  const coversAllExactlyOnce = JSON.stringify(sortedRoles) === JSON.stringify(sortedValid);
  if (!coversAllExactlyOnce) {
    msgEl.textContent = `Each role (${VALID_ROLES.join(", ")}) must be assigned exactly once.`;
    msgEl.classList.remove("ok");
    startBtn.disabled = true;
    return;
  }

  msgEl.textContent = "Lineup looks good.";
  msgEl.classList.add("ok");
  startBtn.disabled = false;
}

async function loadLastRefresh() {
  const el = document.getElementById("refresh-last");
  try {
    const run = await api("GET", "/api/refresh/latest");
    if (!run) {
      el.textContent = "Last refreshed: never";
      return;
    }
    const when = run.finished_at || run.started_at || "unknown time";
    el.textContent = `Last refreshed: ${when} (${run.status})`;
  } catch (e) {
    el.textContent = "Last refreshed: unknown";
  }
}

async function onRefreshClicked() {
  const btn = document.getElementById("refresh-btn");
  const statusEl = document.getElementById("refresh-status");

  btn.disabled = true;
  statusEl.hidden = false;
  statusEl.className = "refresh-status loading";
  statusEl.textContent = "Refreshing from Riot's API… this can take a little while.";

  try {
    const summary = await api("POST", "/api/refresh");
    renderRefreshOutcome(summary);
  } catch (e) {
    // Even a hard failure (network error, unexpected exception) must never block draft setup --
    // cached data from a previous refresh is still useful.
    statusEl.className = "refresh-status error";
    statusEl.innerHTML = "";
    statusEl.appendChild(document.createTextNode(`Refresh failed: ${e.message}`));
    appendContinueAnywayLink(statusEl);
  } finally {
    btn.disabled = false;
    loadLastRefresh();
  }
}

function renderRefreshOutcome(summary) {
  const statusEl = document.getElementById("refresh-status");
  statusEl.innerHTML = "";

  if (summary.status === "success") {
    statusEl.className = "refresh-status success";
    const okCount = (summary.players || []).filter((p) => p.status === "ok").length;
    statusEl.appendChild(
      document.createTextNode(`Refreshed successfully (${okCount} player(s) updated).`)
    );
    return;
  }

  if (summary.status === "failed") {
    statusEl.className = "refresh-status error";
    const msg = summary.fatal_auth_error || "Refresh failed (likely an expired/invalid API key).";
    statusEl.appendChild(document.createTextNode(msg));
    appendContinueAnywayLink(statusEl);
    return;
  }

  // partial
  statusEl.className = "refresh-status partial";
  const lines = document.createElement("div");
  lines.appendChild(document.createTextNode("Refresh completed with some issues:"));
  const list = document.createElement("ul");
  list.style.margin = "6px 0 0 18px";
  for (const p of summary.players || []) {
    if (p.status !== "ok") {
      const li = document.createElement("li");
      li.textContent = `${p.display_name}: ${p.error || p.status}`;
      list.appendChild(li);
    }
  }
  lines.appendChild(list);
  statusEl.appendChild(lines);
  appendContinueAnywayLink(statusEl);
}

function appendContinueAnywayLink(container) {
  const btn = document.createElement("button");
  btn.className = "btn btn-secondary btn-small continue-anyway";
  btn.textContent = "Continue anyway with cached data";
  btn.addEventListener("click", () => {
    container.hidden = true;
  });
  container.appendChild(btn);
}

/* ---------------- Meta data (tier list + Oracle's Elixir) ---------------- */

async function loadOracleElixirStatus() {
  const btn = document.getElementById("fetch-oe-btn");
  try {
    const status = await api("GET", "/api/refresh/oracles-elixir/status");
    btn.hidden = !status.configured_url_set;
  } catch (e) {
    btn.hidden = true;
  }
}

async function onFetchTierListClicked() {
  const btn = document.getElementById("fetch-tierlist-btn");
  const statusEl = document.getElementById("tierlist-status");

  btn.disabled = true;
  statusEl.hidden = false;
  statusEl.className = "refresh-status loading";
  statusEl.textContent = "Fetching from op.gg…";

  try {
    const summary = await api("POST", "/api/refresh/tier-list", { mode: "ranked" });
    statusEl.className = "refresh-status success";
    statusEl.textContent = `Imported ${summary.entries_imported} role-entries `
      + `(patch ${summary.patch}, ${summary.champions_processed} champions seen).`;
  } catch (e) {
    statusEl.className = "refresh-status error";
    statusEl.textContent = `Fetch failed: ${e.message}. The manual tier_list.yaml path still `
      + `works independently -- see the README.`;
  } finally {
    btn.disabled = false;
  }
}

async function onFetchOracleElixirClicked() {
  const btn = document.getElementById("fetch-oe-btn");
  const statusEl = document.getElementById("oe-status");

  btn.disabled = true;
  statusEl.hidden = false;
  statusEl.className = "refresh-status loading";
  statusEl.textContent = "Downloading from the configured URL…";

  try {
    const summary = await api("POST", "/api/refresh/oracles-elixir/fetch");
    renderOracleElixirOutcome(statusEl, summary);
  } catch (e) {
    statusEl.className = "refresh-status error";
    statusEl.textContent = `Fetch failed: ${e.message}. Try the Upload option instead, or `
      + `re-check the URL in .env against oracleselixir.com/tools/downloads.`;
  } finally {
    btn.disabled = false;
  }
}

async function onOracleElixirFileSelected(e) {
  const file = e.target.files[0];
  if (!file) return;
  const statusEl = document.getElementById("oe-status");

  statusEl.hidden = false;
  statusEl.className = "refresh-status loading";
  statusEl.textContent = `Importing ${file.name}…`;

  try {
    const formData = new FormData();
    formData.append("file", file);
    const res = await fetch("/api/refresh/oracles-elixir/upload", { method: "POST", body: formData });
    const summary = await res.json();
    if (!res.ok) throw new Error(summary.detail || "Upload failed");
    renderOracleElixirOutcome(statusEl, summary);
  } catch (err) {
    statusEl.className = "refresh-status error";
    statusEl.textContent = `Import failed: ${err.message}`;
  } finally {
    e.target.value = "";
  }
}

function renderOracleElixirOutcome(statusEl, summary) {
  statusEl.className = "refresh-status success";
  statusEl.textContent = `Imported ${summary.rows_upserted} rows across ${summary.games_count} `
    + `games. ${summary.rows_unresolved} champion name(s) unresolved.`;
}

async function onStartDraftClicked() {
  const errEl = document.getElementById("start-draft-error");
  errEl.hidden = true;

  const lineup = Array.from(state.selectedRoles.entries()).map(([player_id, assigned_role]) => ({
    player_id,
    assigned_role,
  }));

  try {
    const sessionRes = await api("POST", "/api/roster/sessions", {
      our_side: state.ourSide,
      lineup,
    });
    const draftRes = await api("POST", "/api/draft", {
      session_id: sessionRes.session_id,
      our_side: state.ourSide,
    });

    state.draftSessionId = draftRes.draft_session_id;
    state.draft = draftRes;
    localStorage.setItem(LOCAL_STORAGE_KEY, String(draftRes.draft_session_id));

    showDraftView();
    renderDraft();
  } catch (e) {
    errEl.textContent = `Could not start draft: ${e.message}`;
    errEl.hidden = false;
  }
}

/* ============================================================
   CHAMPIONS (shared by setup grid preload + draft view)
   ============================================================ */
async function loadChampions() {
  try {
    state.champions = await api("GET", "/api/champions");
    state.champById = new Map(state.champions.map((c) => [c.champion_id, c]));
  } catch (e) {
    console.error("Failed to load champions", e);
    state.champions = [];
  }
}

function champName(championId) {
  const c = state.champById.get(championId);
  return c ? c.name : `#${championId}`;
}

function champIcon(championId) {
  const c = state.champById.get(championId);
  return c ? c.icon_path : "";
}

/* ============================================================
   DRAFT VIEW
   ============================================================ */
function onNewDraftClicked() {
  localStorage.removeItem(LOCAL_STORAGE_KEY);
  state.draftSessionId = null;
  state.draft = null;
  state.selectedRoles = new Map();
  hoverController.cache.clear();
  showSetupView();
  loadRoster();
  loadLastRefresh();
}

function renderDraft() {
  // A full state replace can destroy the DOM node the detail panel is anchored to (e.g. the
  // hovered champion just got picked/banned and is no longer in the suggestions list) --
  // always close it here rather than risk a panel left pointing at nothing.
  closeChampionDetailPanel();
  renderStepBanner();
  renderInvalidatedBanner();
  renderBoard();
  renderChampGrid(document.getElementById("champ-search").value);
  renderSuggestions();
}

function renderStepBanner() {
  const banner = document.getElementById("step-banner");
  const textEl = document.getElementById("step-banner-text");
  const info = state.draft.current_slot_info;

  banner.className = "step-banner";

  if (!info || info.is_draft_complete) {
    banner.classList.add("complete");
    textEl.textContent = "Draft complete.";
    return;
  }

  const actionClass = info.action === "BAN" ? "action-ban" : "action-pick";
  banner.classList.add(actionClass);
  if (info.is_our_turn) banner.classList.add("our-turn");

  const stepLabel = `STEP ${info.wiki_step} · ${info.side} ${info.action}S`;
  textEl.innerHTML = "";
  textEl.appendChild(document.createTextNode(stepLabel));

  const tag = document.createElement("span");
  tag.className = "turn-tag";
  tag.textContent = info.is_our_turn ? "OUR TURN" : "THEIR TURN";
  textEl.appendChild(tag);

  if (info.action === "PICK" && info.opp_pick_gap_after !== null && info.opp_pick_gap_after !== undefined) {
    const gapNote = document.createElement("span");
    gapNote.style.marginLeft = "12px";
    gapNote.style.fontSize = "13px";
    gapNote.style.fontWeight = "400";
    gapNote.style.color = "inherit";
    gapNote.style.opacity = "0.75";
    if (info.opp_pick_gap_after === 2) {
      gapNote.textContent = "(high exposure -- opponent picks twice before you again)";
    } else if (info.opp_pick_gap_after === 1) {
      gapNote.textContent = "(opponent gets one pick before you again)";
    } else {
      gapNote.textContent = "(no opponent pick before you again)";
    }
    textEl.appendChild(gapNote);
  }
}

function renderInvalidatedBanner() {
  const el = document.getElementById("invalidated-banner");
  const invalidated = state.draft.newly_invalidated_slots;
  if (!invalidated || invalidated.length === 0) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.innerHTML = "";
  const msg = document.createElement("span");
  const slotWord = invalidated.length === 1 ? "slot" : "slots";
  msg.textContent =
    `This change also invalidated your entry at ${slotWord} ${invalidated.join(", ")} ` +
    "-- please review and fix.";
  const dismiss = document.createElement("button");
  dismiss.className = "btn btn-secondary btn-small";
  dismiss.textContent = "Dismiss";
  dismiss.addEventListener("click", () => {
    el.hidden = true;
  });
  el.appendChild(msg);
  el.appendChild(dismiss);
}

function renderBoard() {
  const entries = state.draft.entries || [];
  const currentSlot = state.draft.current_slot;

  const cols = {
    BLUE: { bans: [], picks: [] },
    RED: { bans: [], picks: [] },
  };
  for (const entry of entries) {
    if (!entry) continue;
    const bucket = entry.action === "BAN" ? cols[entry.side].bans : cols[entry.side].picks;
    bucket.push(entry);
  }

  renderBoardRow("blue-bans", cols.BLUE.bans, currentSlot);
  renderBoardRow("blue-picks", cols.BLUE.picks, currentSlot);
  renderBoardRow("red-bans", cols.RED.bans, currentSlot);
  renderBoardRow("red-picks", cols.RED.picks, currentSlot);
}

function renderBoardRow(containerId, slotEntries, currentSlot) {
  const el = document.getElementById(containerId);
  el.innerHTML = "";
  for (const entry of slotEntries) {
    const cell = document.createElement("div");
    cell.className = "slot-cell";
    if (entry.champion_id !== null && entry.champion_id !== undefined) {
      cell.classList.add("filled");
      const img = document.createElement("img");
      img.src = champIcon(entry.champion_id);
      img.alt = champName(entry.champion_id);
      cell.appendChild(img);
      const nameTag = document.createElement("div");
      nameTag.className = "slot-champ-name";
      nameTag.textContent = champName(entry.champion_id);
      cell.appendChild(nameTag);
      cell.addEventListener("click", (evt) => {
        // The role-badge select handles its own clicks; don't also open the amend popover
        // when the click originated there.
        if (evt.target.closest(".role-badge-select")) return;
        openAmendPopover(entry.slot, evt.currentTarget);
      });
      if (entry.action === "PICK" && state.draft.our_side && entry.side === state.draft.our_side) {
        cell.appendChild(buildRoleBadge(entry));
      }
    } else {
      cell.classList.add("empty");
    }
    if (entry.slot === currentSlot) cell.classList.add("current-slot");
    if (entry.invalidated) cell.classList.add("invalidated");
    el.appendChild(cell);
  }
}

function buildRoleBadge(entry) {
  const select = document.createElement("select");
  select.className = "role-badge-select";
  select.classList.add(entry.resolved_role ? (entry.is_role_override ? "role-badge-override" : "role-badge-set") : "role-badge-unknown");
  select.title = entry.is_role_override ? "Manually set -- click to change or clear" : "Auto-detected role -- click to override";

  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "Unknown";
  select.appendChild(blank);
  for (const role of VALID_ROLES) {
    const opt = document.createElement("option");
    opt.value = role;
    opt.textContent = role;
    select.appendChild(opt);
  }
  select.value = entry.resolved_role || "";

  select.addEventListener("click", (evt) => evt.stopPropagation());
  select.addEventListener("change", () => onRoleBadgeChanged(entry.slot, select.value));

  return select;
}

async function onRoleBadgeChanged(slot, role) {
  try {
    const result = role
      ? await api("POST", `/api/draft/${state.draftSessionId}/role-override`, { slot, role })
      : await api("DELETE", `/api/draft/${state.draftSessionId}/role-override/${slot}`);
    state.draft = result;
    renderDraft();
  } catch (e) {
    console.error("Could not update role override:", e);
    renderBoard(); // revert the select's stale optimistic value back to the last-known-good state
  }
}

function usedChampionIds() {
  const used = new Set();
  for (const entry of state.draft.entries || []) {
    if (entry && entry.champion_id !== null && entry.champion_id !== undefined) {
      used.add(entry.champion_id);
    }
  }
  return used;
}

function renderChampGrid(filterText) {
  const el = document.getElementById("champ-grid");
  el.innerHTML = "";
  const used = usedChampionIds();
  const draftComplete = state.draft.current_slot_info && state.draft.current_slot_info.is_draft_complete;

  const needle = (filterText || "").trim().toLowerCase();
  const list = state.champions.filter((c) => !needle || c.name.toLowerCase().includes(needle));

  for (const champ of list) {
    const cell = document.createElement("div");
    cell.className = "champ-cell";
    const isUsed = used.has(champ.champion_id);
    if (isUsed || draftComplete) cell.classList.add("disabled");

    const img = document.createElement("img");
    img.src = champ.icon_path;
    img.alt = champ.name;
    img.loading = "lazy";
    cell.appendChild(img);

    const nameTag = document.createElement("div");
    nameTag.className = "champ-name";
    nameTag.textContent = champ.name;
    cell.appendChild(nameTag);

    if (!isUsed && !draftComplete) {
      cell.addEventListener("click", () => onChampionClicked(champ.champion_id));
    }
    el.appendChild(cell);
  }

  if (list.length === 0) {
    el.textContent = "No champions match your search.";
  }
}

async function onChampionClicked(championId) {
  const errBanner = document.getElementById("invalidated-banner");
  try {
    const result = await api("POST", `/api/draft/${state.draftSessionId}/enter`, {
      champion_id: championId,
    });
    state.draft = result;
    renderDraft();
  } catch (e) {
    errBanner.hidden = false;
    errBanner.innerHTML = "";
    errBanner.appendChild(document.createTextNode(`Could not enter pick/ban: ${e.message}`));
  }
}

/* ---------------- Suggestions ---------------- */
const LOW_CONFIDENCE_THRESHOLD = 0.4;

// Every signal is 0-1, higher = better, but picking the top-3 AVAILABLE signals for a
// candidate (see buildWhyText) can surface a merely-average value if it happens to be the
// least-thin data point that candidate has -- e.g. an exactly-neutral 0.5 personal win rate
// (a 1-1 record) can outrank a 0 mastery signal without itself being genuinely "strong."
// Three tiers keep the wording honest about actual magnitude, not just relative rank.
const BREAKDOWN_LABELS_STRONG = {
  global_win_rate: "strong globally",
  global_pick_rate: "commonly picked",
  personal_mastery: "high personal mastery",
  personal_win_rate: "strong personal win rate",
  pro_synergy: "synergizes with our picks (pro data)",
  roster_synergy: "synergizes with our picks (our own games)",
  role_need: "fills an open role",
  pick_safety: "safe pick for this slot",
  enemy_role_flex_risk: "denies enemy role flexibility",
  counters_our_comfort_pool: "threatens our comfort picks",
  enemy_draft_trajectory: "fits enemy's draft direction",
};

const BREAKDOWN_LABELS_NEUTRAL = {
  global_win_rate: "average globally",
  global_pick_rate: "moderately picked",
  personal_mastery: "some personal experience",
  personal_win_rate: "even personal record",
  pro_synergy: "neutral synergy with our picks (pro data)",
  roster_synergy: "neutral synergy with our picks (our own games)",
  role_need: "partially fills a role",
  pick_safety: "moderately safe for this slot",
  enemy_role_flex_risk: "some enemy role flexibility risk",
  counters_our_comfort_pool: "mild threat to our comfort picks",
  enemy_draft_trajectory: "loosely fits enemy's draft direction",
};

const BREAKDOWN_LABELS_WEAK = {
  global_win_rate: "weak globally",
  global_pick_rate: "rarely picked",
  personal_mastery: "little personal experience",
  personal_win_rate: "weak personal record",
  pro_synergy: "poor synergy with our picks (pro data)",
  roster_synergy: "poor synergy with our picks (our own games)",
  role_need: "doesn't fill a role we need",
  pick_safety: "risky pick for this slot",
  enemy_role_flex_risk: "little enemy role flexibility denied",
  counters_our_comfort_pool: "little threat to our comfort picks",
  enemy_draft_trajectory: "against enemy's likely direction",
};

function renderSuggestions() {
  const titleEl = document.getElementById("suggestions-title");
  const listEl = document.getElementById("suggestions-list");
  listEl.innerHTML = "";

  const info = state.draft.current_slot_info;
  if (state.draft.draft_complete || !info || info.is_draft_complete) {
    titleEl.textContent = "Suggestions";
    const msg = document.createElement("div");
    msg.className = "draft-complete-msg";
    msg.textContent = "Draft complete -- no further suggestions.";
    listEl.appendChild(msg);
    return;
  }

  // Suggestions always reflect OUR side's needs/data, even when it's the opponent's turn --
  // there's nothing for our driver to act on right now, so label it as a preview of our next
  // move rather than implying it's advice for whoever is currently acting.
  const actionWord = info.action === "BAN" ? "Ban" : "Pick";
  titleEl.textContent = info.is_our_turn ? `${actionWord} Suggestions` : `Our Next ${actionWord} (preview)`;

  const suggestions = state.draft.suggestions || [];
  if (suggestions.length === 0) {
    const msg = document.createElement("div");
    msg.className = "draft-complete-msg";
    msg.textContent = "No suggestions available (no data yet, or no eligible candidates).";
    listEl.appendChild(msg);
    return;
  }

  for (const s of suggestions) {
    listEl.appendChild(buildSuggestionRow(s));
  }
}

function buildSuggestionRow(suggestion) {
  const row = document.createElement("div");
  row.className = "suggestion-row";
  const lowConfidence = suggestion.confidence !== null && suggestion.confidence !== undefined
    && suggestion.confidence < LOW_CONFIDENCE_THRESHOLD;
  if (lowConfidence) row.classList.add("low-confidence");

  const img = document.createElement("img");
  img.src = champIcon(suggestion.champion_id);
  img.alt = champName(suggestion.champion_id);
  row.appendChild(img);

  const main = document.createElement("div");
  main.className = "suggestion-main";

  const nameRow = document.createElement("div");
  nameRow.className = "suggestion-name-row";

  const name = document.createElement("span");
  name.className = "suggestion-name";
  name.textContent = champName(suggestion.champion_id);
  nameRow.appendChild(name);

  const score = document.createElement("span");
  score.className = "suggestion-score";
  score.textContent = formatPercent(suggestion.score);
  nameRow.appendChild(score);

  if (lowConfidence) {
    const badge = document.createElement("span");
    badge.className = "confidence-badge";
    badge.textContent = "LOW CONFIDENCE";
    nameRow.appendChild(badge);
  }

  main.appendChild(nameRow);

  const why = document.createElement("div");
  why.className = "suggestion-why";
  why.textContent = buildWhyText(suggestion);
  main.appendChild(why);

  row.appendChild(main);

  row.addEventListener("click", () => onChampionClicked(suggestion.champion_id));
  row.addEventListener("mouseenter", () => onSuggestionRowMouseEnter(suggestion, row));
  row.addEventListener("mouseleave", onSuggestionRowMouseLeave);

  return row;
}

function labelForSignal(key, value) {
  if (value >= 0.6) return BREAKDOWN_LABELS_STRONG[key] || key;
  if (value >= 0.35) return BREAKDOWN_LABELS_NEUTRAL[key] || key;
  return BREAKDOWN_LABELS_WEAK[key] || key;
}

function buildWhyText(suggestion) {
  const breakdown = suggestion.breakdown || {};
  const entries = Object.entries(breakdown)
    .filter(([, value]) => value !== null && value !== undefined)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([key, value]) => labelForSignal(key, value));

  const confidencePct = suggestion.confidence !== null && suggestion.confidence !== undefined
    ? ` · confidence ${formatPercent(suggestion.confidence)}`
    : "";

  if (entries.length === 0) {
    return `Limited data available${confidencePct}`;
  }
  return `${entries.join(", ")}${confidencePct}`;
}

function formatPercent(value) {
  if (value === null || value === undefined) return "--";
  return `${Math.round(value * 100)}%`;
}

function formatRealPercent(value) {
  if (value === null || value === undefined) return "--";
  // One decimal place -- distinguishes this from formatPercent() (used for normalized 0-100
  // SCORES, rounded to whole numbers); real win rates like "53.2%" carry meaningful precision
  // a driver comparing two close options during a live draft would want.
  return `${(value * 100).toFixed(1)}%`;
}

/* ============================================================
   Champion detail hover panel
   ============================================================ */
function cacheKey(draftSessionId, championId, currentSlot) {
  return `${draftSessionId}:${championId}:${currentSlot}`;
}

function onSuggestionRowMouseEnter(suggestion, rowEl) {
  clearTimeout(hoverController.closeTimer);
  clearTimeout(hoverController.openTimer);
  hoverController.openTimer = setTimeout(() => {
    openChampionDetailPanel(suggestion.champion_id, rowEl);
  }, CHAMPION_DETAIL_HOVER_DELAY_MS);
}

function onSuggestionRowMouseLeave() {
  clearTimeout(hoverController.openTimer);
  hoverController.closeTimer = setTimeout(closeChampionDetailPanel, CHAMPION_DETAIL_CLOSE_GRACE_MS);
}

async function openChampionDetailPanel(championId, anchorEl) {
  const myToken = ++hoverController.requestToken;

  const panel = document.getElementById("champion-detail-panel");
  positionChampionDetailPanel(anchorEl);
  panel.hidden = false;

  const key = cacheKey(state.draftSessionId, championId, state.draft.current_slot);
  if (hoverController.cache.has(key)) {
    renderChampionDetailPanel(hoverController.cache.get(key));
    return;
  }

  renderChampionDetailPanelLoading(championId);
  try {
    const detail = await api("GET", `/api/draft/${state.draftSessionId}/champion-detail/${championId}`);
    if (myToken !== hoverController.requestToken) return; // superseded by a later hover; discard
    hoverController.cache.set(key, detail);
    renderChampionDetailPanel(detail);
  } catch (e) {
    if (myToken !== hoverController.requestToken) return;
    renderChampionDetailPanelError(e);
  }
}

function closeChampionDetailPanel() {
  document.getElementById("champion-detail-panel").hidden = true;
  hoverController.requestToken++; // invalidate any still-in-flight fetch immediately
}

function positionChampionDetailPanel(anchorEl) {
  const panel = document.getElementById("champion-detail-panel");
  const rect = anchorEl.getBoundingClientRect();
  const panelWidth = 340;
  let left = rect.right + 12; // open to the RIGHT of the suggestion row by default --
                                // the suggestions list scrolls, so (unlike the amend popover,
                                // which always opens below a small fixed-position board cell)
                                // a below-anchored panel near the bottom of the viewport would
                                // risk running off-screen; side-anchoring avoids that.
  if (left + panelWidth > window.innerWidth - 10) {
    left = rect.left - panelWidth - 12; // flip to the LEFT if that would overflow
  }
  if (left < 10) left = Math.max(10, rect.left);
  panel.style.left = `${left}px`;

  const maxTop = window.innerHeight + window.scrollY - 480 - 10; // 480 = panel max-height
  const top = rect.top + window.scrollY;
  panel.style.top = `${Math.max(10, Math.min(top, maxTop))}px`;
}

function renderChampionDetailPanelLoading(championId) {
  document.getElementById("cdp-body").hidden = true;
  document.getElementById("cdp-error").hidden = true;
  document.getElementById("cdp-loading").hidden = false;
  document.getElementById("cdp-name").textContent = champName(championId);
  document.getElementById("cdp-icon").src = champIcon(championId);
  document.getElementById("cdp-role").textContent = "";
  document.getElementById("cdp-tier-badge").hidden = true;
}

function renderChampionDetailPanelError(err) {
  document.getElementById("cdp-body").hidden = true;
  document.getElementById("cdp-loading").hidden = true;
  const errEl = document.getElementById("cdp-error");
  errEl.hidden = false;
  errEl.textContent = `Could not load detail: ${err.message}`;
}

function renderChampionDetailPanel(detail) {
  document.getElementById("cdp-loading").hidden = true;
  document.getElementById("cdp-error").hidden = true;
  document.getElementById("cdp-body").hidden = false;

  document.getElementById("cdp-icon").src = champIcon(detail.champion_id);
  document.getElementById("cdp-icon").alt = detail.champion_name;
  document.getElementById("cdp-name").textContent = detail.champion_name;
  document.getElementById("cdp-role").textContent = detail.role ? `as ${detail.role}` : "role unclear";

  const tierBadge = document.getElementById("cdp-tier-badge");
  if (detail.global.has_data && detail.global.tier) {
    tierBadge.hidden = false;
    tierBadge.textContent = detail.global.tier;
    tierBadge.className = `cdp-tier-badge tier-${String(detail.global.tier).toLowerCase()}`;
  } else {
    tierBadge.hidden = true;
  }

  renderCdpGlobalStats(detail.global);
  renderCdpRoster(detail.roster);

  const synergySection = document.getElementById("cdp-synergy-section");
  const threatSection = document.getElementById("cdp-threat-section");
  if (detail.action_context === "PICK") {
    synergySection.hidden = false;
    threatSection.hidden = true;
    renderCdpSynergy(detail.synergy_with_picks);
  } else {
    synergySection.hidden = true;
    threatSection.hidden = false;
    renderCdpThreat(detail.ban_threat);
  }
}

function renderCdpGlobalStats(global) {
  const wr = document.getElementById("cdp-winrate");
  const pr = document.getElementById("cdp-pickrate");
  const br = document.getElementById("cdp-banrate");
  const sample = document.getElementById("cdp-sample");

  if (!global.has_data) {
    wr.textContent = pr.textContent = br.textContent = "--";
    sample.textContent = "No tier data for this role/patch yet.";
    return;
  }
  wr.textContent = formatRealPercent(global.win_rate);
  pr.textContent = formatRealPercent(global.pick_rate);
  br.textContent = formatRealPercent(global.ban_rate);
  sample.textContent = global.sample_size
    ? `Based on ${global.sample_size.toLocaleString()} games`
    : "Sample size unknown";
}

function renderCdpRoster(roster) {
  const el = document.getElementById("cdp-roster-list");
  el.innerHTML = "";
  for (const r of roster) {
    const row = document.createElement("div");
    row.className = "cdp-roster-row";
    if (r.is_assigned_to_role) row.classList.add("assigned-role");
    if (!r.mastery && !r.personal) row.classList.add("no-data");

    const name = document.createElement("span");
    name.className = "cdp-roster-name";
    name.textContent = r.display_name;
    row.appendChild(name);

    const roleTag = document.createElement("span");
    roleTag.className = "cdp-roster-role-tag";
    roleTag.textContent = r.assigned_role;
    row.appendChild(roleTag);

    const stats = document.createElement("span");
    stats.className = "cdp-roster-stats";
    if (!r.mastery && !r.personal) {
      stats.textContent = "No history";
    } else {
      const parts = [];
      if (r.mastery) parts.push(`M${r.mastery.level} · ${r.mastery.points.toLocaleString()} pts`);
      if (r.personal) parts.push(`${r.personal.games}g, ${formatRealPercent(r.personal.win_rate)} WR`);
      stats.textContent = parts.join(" · ");
    }
    row.appendChild(stats);
    el.appendChild(row);
  }
}

function renderCdpSynergy(synergyRows) {
  const el = document.getElementById("cdp-synergy-list");
  el.innerHTML = "";
  if (!synergyRows || synergyRows.length === 0) {
    const msg = document.createElement("div");
    msg.className = "cdp-no-data";
    msg.textContent = "No synergy data with your current picks yet.";
    el.appendChild(msg);
    return;
  }
  for (const s of synergyRows) {
    const row = document.createElement("div");
    row.className = "cdp-synergy-row";
    const left = document.createElement("span");
    left.textContent = `with ${s.ally_champion_name}`;
    const right = document.createElement("span");
    right.textContent = `${formatRealPercent(s.win_rate_together)} (${s.games_together}g${s.source === "pro" ? ", pro" : ""})`;
    row.appendChild(left);
    row.appendChild(right);
    el.appendChild(row);
  }
}

function renderCdpThreat(threat) {
  const el = document.getElementById("cdp-threat-list");
  el.innerHTML = "";
  if (!threat.counters_comfort_pool || threat.counters_comfort_pool.length === 0) {
    const msg = document.createElement("div");
    msg.className = "cdp-no-data";
    msg.textContent = "No known matchup data against your comfort picks.";
    el.appendChild(msg);
  } else {
    for (const t of threat.counters_comfort_pool) {
      const row = document.createElement("div");
      row.className = "cdp-threat-row";
      const isHighThreat = t.win_rate_against >= 0.55;
      const left = document.createElement("span");
      left.textContent = `vs ${t.display_name}'s ${t.comfort_champion_name}`;
      const right = document.createElement("span");
      right.className = `cdp-threat-winrate${isHighThreat ? " high" : ""}`;
      right.textContent = `${formatRealPercent(t.win_rate_against)} (${t.games}g)`;
      row.appendChild(left);
      row.appendChild(right);
      el.appendChild(row);
    }
  }

  const fitsEl = document.getElementById("cdp-enemy-fits");
  if (threat.enemy_fits_role) {
    fitsEl.hidden = false;
    fitsEl.textContent = `Also fills their open ${threat.enemy_fits_role}.`;
  } else {
    fitsEl.hidden = true;
  }
}

/* ============================================================
   "Fix a previous slot" (amend) popover
   ============================================================ */
function openAmendPopover(slot, anchorEl) {
  state.amendTargetSlot = slot;

  const popover = document.getElementById("amend-popover");
  const titleEl = document.getElementById("amend-popover-title");
  const entry = (state.draft.entries || []).find((e) => e && e.slot === slot);
  const currentName = entry && entry.champion_id !== null ? champName(entry.champion_id) : "(empty)";
  titleEl.textContent = `Fix slot ${slot} (currently ${currentName})`;

  document.getElementById("amend-search").value = "";
  renderAmendGrid("");

  popover.hidden = false;
  const rect = anchorEl.getBoundingClientRect();
  const popoverWidth = 320;
  let left = rect.left;
  if (left + popoverWidth > window.innerWidth - 10) {
    left = window.innerWidth - popoverWidth - 10;
  }
  popover.style.left = `${Math.max(10, left)}px`;
  popover.style.top = `${rect.bottom + 8 + window.scrollY}px`;
}

function closeAmendPopover() {
  state.amendTargetSlot = null;
  document.getElementById("amend-popover").hidden = true;
}

function championIdsUsedBeforeSlot(slot) {
  // Only slots STRICTLY BEFORE `slot` -- matches the backend's amend() rule (state.py):
  // colliding with an earlier-filled slot is a hard reject, but colliding with a LATER-filled
  // slot is allowed (the later slot gets flagged `invalidated` instead) so that fixing a
  // two-slot swap doesn't deadlock -- you fix the earlier slot first, which frees the later one
  // up to be corrected next. If this popover disabled every already-used champion regardless of
  // slot position, that later-slot path would be unreachable from the UI even though the
  // backend fully supports it.
  const used = new Set();
  for (const entry of state.draft.entries || []) {
    if (entry && entry.slot < slot && entry.champion_id !== null && entry.champion_id !== undefined) {
      used.add(entry.champion_id);
    }
  }
  return used;
}

function renderAmendGrid(filterText) {
  const el = document.getElementById("amend-grid");
  el.innerHTML = "";
  if (state.amendTargetSlot === null) return;

  const usedBefore = championIdsUsedBeforeSlot(state.amendTargetSlot);

  const needle = (filterText || "").trim().toLowerCase();
  const list = state.champions.filter((c) => !needle || c.name.toLowerCase().includes(needle));

  for (const champ of list) {
    // Disabled only if used at an EARLIER slot -- a champion used at a later slot (or unused,
    // or the slot's own current value) is selectable; see championIdsUsedBeforeSlot above.
    const isUsedElsewhere = usedBefore.has(champ.champion_id);

    const cell = document.createElement("div");
    cell.className = "champ-cell";
    if (isUsedElsewhere) cell.classList.add("disabled");

    const img = document.createElement("img");
    img.src = champ.icon_path;
    img.alt = champ.name;
    img.loading = "lazy";
    cell.appendChild(img);

    const nameTag = document.createElement("div");
    nameTag.className = "champ-name";
    nameTag.textContent = champ.name;
    cell.appendChild(nameTag);

    if (!isUsedElsewhere) {
      cell.addEventListener("click", () => onAmendChampionClicked(champ.champion_id));
    }
    el.appendChild(cell);
  }
}

async function onAmendChampionClicked(championId) {
  const slot = state.amendTargetSlot;
  if (slot === null) return;

  try {
    const result = await api("POST", `/api/draft/${state.draftSessionId}/amend`, {
      slot,
      champion_id: championId,
    });
    state.draft = result;
    // Unlike enter(), amend() doesn't advance current_slot -- the detail cache's key includes
    // current_slot for cheap free invalidation on every real pick/ban, but an amend to an
    // EARLIER slot can change our_picks_so_far()/their_picks_so_far() (affecting synergy/threat
    // data) without that key changing. Amends are rare and deliberate, so a full cache-bust
    // here is cheap insurance rather than building a more surgical partial-invalidation scheme.
    hoverController.cache.clear();
    closeAmendPopover();
    renderDraft();
  } catch (e) {
    const titleEl = document.getElementById("amend-popover-title");
    titleEl.textContent = `Error: ${e.message}`;
  }
}
