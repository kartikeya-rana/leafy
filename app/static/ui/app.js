/* Leafy web UI — vanilla JS client (dashboard-first).
 *
 * Talks to the ADK FastAPI backend (same origin):
 *   GET  /api/profile                                → location chip
 *   GET  /api/dashboard                              → today board (plants + weather + status)
 *   POST /apps/app/users/local_user/sessions         → create chat session
 *   GET  /apps/app/users/local_user/sessions/{id}    → reuse session + history
 *   POST /run_sse                                    → chat turn (SSE stream)
 */

const APP_NAME = "app";
const USER_ID = "local_user";
const SESSION_KEY = "leafySessionId";

const state = {
  sessionId: null,
  plants: [],
  selectedPlantId: null,
  pendingImage: null, // { mimeType, data (b64), url }
  busy: false,
  dashboardData: null,
  chatOpen: false,
};

const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");

/* ---------------------------------------------------------------- utils */

function icons() {
  window.lucide && lucide.createIcons();
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/* Tiny markdown-ish renderer: bold + bullet lists + paragraphs. */
function renderMarkdown(text) {
  const bold = escapeHtml(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const blocks = bold.split(/\n{2,}/);
  return blocks
    .map((block) => {
      const lines = block.split("\n");
      if (lines.every((l) => /^\s*[-*•]\s+/.test(l) || !l.trim())) {
        const items = lines
          .filter((l) => l.trim())
          .map((l) => `<li>${l.replace(/^\s*[-*•]\s+/, "")}</li>`)
          .join("");
        return `<ul>${items}</ul>`;
      }
      return `<p>${lines.join("<br>")}</p>`;
    })
    .join("");
}

function relativeWatered(iso) {
  if (!iso) return null;
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  if (days <= 0) return "watered today";
  if (days === 1) return "watered yesterday";
  return `watered ${days} days ago`;
}

function mapWmoCode(code) {
  if (code === 0) return "Sunny";
  if ([1, 2, 3].includes(code)) return "Cloudy";
  if ([45, 48].includes(code)) return "Foggy";
  if ([51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82].includes(code)) return "Rainy";
  if ([71, 73, 75, 77, 85, 86].includes(code)) return "Snowy";
  if ([95, 96, 99].includes(code)) return "Thunderstorm";
  return "Cloudy";
}

const PLANT_GLYPHS = ["leaf", "sprout", "flower-2", "tree-deciduous", "clover"];
function plantGlyph(name) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 9973;
  return PLANT_GLYPHS[h % PLANT_GLYPHS.length];
}

function scrollToBottom() {
  messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: "smooth" });
}

/* --------------------------------------------------------- weather chip */

function showWeather(weather, locationName) {
  if (!weather || !locationName) return;
  $("locationChip").classList.add("hidden");
  $("locationChip").classList.remove("flex");

  $("weatherTemp").textContent = `${Math.round(weather.temp)}°C`;
  $("weatherCondition").textContent = weather.condition;
  $("weatherHighLow").textContent = `H: ${Math.round(weather.high)}° L: ${Math.round(weather.low)}°`;
  $("locationNameWidget").textContent = locationName;

  const cond = weather.condition.toLowerCase();
  let icon = "cloud";
  if (cond === "sunny") icon = "sun";
  else if (cond === "rainy") icon = "cloud-rain";
  else if (cond === "snowy") icon = "snowflake";
  else if (cond === "thunderstorm") icon = "cloud-lightning";
  else if (cond === "foggy") icon = "cloud-fog";

  $("weatherIconContainer").innerHTML = `<i data-lucide="${icon}" class="w-4 h-4 text-leaf"></i>`;
  $("weatherWidget").classList.remove("hidden");
  $("weatherWidget").classList.add("flex");
  icons();
}

async function loadProfile() {
  try {
    const r = await fetch(`/api/profile?t=${Date.now()}`, { cache: "no-store" });
    const p = await r.json();
    const name = p.resolved_name || p.location_text;
    if (name) {
      $("locationName").textContent = name;
      $("locationChip").classList.remove("hidden");
      $("locationChip").classList.add("flex");
    }
  } catch (_) { /* chip stays hidden */ }
}

/* ----------------------------------------------------------- dashboard */

function renderSummaryStrip(summary, locationName) {
  const strip = $("summaryStrip");
  if (!summary) { strip.innerHTML = ""; return; }

  const weatherHtml = summary.weather
    ? (() => {
        const w = summary.weather;
        const cond = w.condition.toLowerCase();
        let wIcon = "cloud";
        if (cond === "sunny") wIcon = "sun";
        else if (cond === "rainy") wIcon = "cloud-rain";
        else if (cond === "snowy") wIcon = "snowflake";
        else if (cond === "thunderstorm") wIcon = "cloud-lightning";
        else if (cond === "foggy") wIcon = "cloud-fog";
        return `
          <div class="stat-card bg-paper border border-linen rounded-xl2 p-4 flex items-center gap-3">
            <div class="w-10 h-10 rounded-full bg-sage-tint flex items-center justify-center shrink-0">
              <i data-lucide="${wIcon}" class="w-5 h-5 text-leaf"></i>
            </div>
            <div class="min-w-0">
              <div class="text-[11px] text-ink-faint uppercase font-bold tracking-wider">Weather</div>
              <div class="font-semibold text-ink text-lg leading-tight">${Math.round(w.temp)}°C</div>
              <div class="text-xs text-ink-muted">${escapeHtml(w.condition)}${locationName ? ` · ${escapeHtml(locationName)}` : ""}</div>
            </div>
          </div>`;
      })()
    : `
      <div class="stat-card bg-paper border border-linen rounded-xl2 p-4 flex items-center gap-3">
        <div class="w-10 h-10 rounded-full bg-sage-tint flex items-center justify-center shrink-0">
          <i data-lucide="cloud-off" class="w-5 h-5 text-ink-faint"></i>
        </div>
        <div class="min-w-0">
          <div class="text-[11px] text-ink-faint uppercase font-bold tracking-wider">Weather</div>
          <div class="text-sm text-ink-muted">Set your location to see weather</div>
        </div>
      </div>`;

  const totalIcon = summary.total_plants > 0 ? "sprout" : "plus";
  const waterColor = summary.water_due_count > 0 ? "text-amber" : "text-leaf";
  const waterBg = summary.water_due_count > 0 ? "bg-amber-tint" : "bg-leaf-soft";
  const moveColor = summary.move_count > 0 ? "text-amber" : "text-leaf";
  const moveBg = summary.move_count > 0 ? "bg-amber-tint" : "bg-leaf-soft";

  strip.innerHTML = `
    ${weatherHtml}

    <div class="stat-card bg-paper border border-linen rounded-xl2 p-4 flex items-center gap-3">
      <div class="w-10 h-10 rounded-full bg-leaf-soft flex items-center justify-center shrink-0">
        <i data-lucide="${totalIcon}" class="w-5 h-5 text-leaf"></i>
      </div>
      <div class="min-w-0">
        <div class="text-[11px] text-ink-faint uppercase font-bold tracking-wider">Plants</div>
        <div class="font-semibold text-ink text-lg leading-tight">${summary.total_plants}</div>
        <div class="text-xs text-ink-muted">${summary.total_plants === 1 ? "plant" : "plants"} tracked</div>
      </div>
    </div>

    <div class="stat-card bg-paper border border-linen rounded-xl2 p-4 flex items-center gap-3">
      <div class="w-10 h-10 rounded-full ${waterBg} flex items-center justify-center shrink-0">
        <i data-lucide="droplet" class="w-5 h-5 ${waterColor}"></i>
      </div>
      <div class="min-w-0">
        <div class="text-[11px] text-ink-faint uppercase font-bold tracking-wider">Watering</div>
        <div class="font-semibold text-ink text-lg leading-tight">${summary.water_due_count}</div>
        <div class="text-xs text-ink-muted">${summary.water_due_count === 1 ? "needs" : "need"} water</div>
      </div>
    </div>

    <div class="stat-card bg-paper border border-linen rounded-xl2 p-4 flex items-center gap-3">
      <div class="w-10 h-10 rounded-full ${moveBg} flex items-center justify-center shrink-0">
        <i data-lucide="arrow-right-left" class="w-5 h-5 ${moveColor}"></i>
      </div>
      <div class="min-w-0">
        <div class="text-[11px] text-ink-faint uppercase font-bold tracking-wider">Shelter</div>
        <div class="font-semibold text-ink text-lg leading-tight">${summary.move_count}</div>
        <div class="text-xs text-ink-muted">to move</div>
      </div>
    </div>
  `;
  icons();
}

function skeletonCards() {
  $("plantList").innerHTML = Array.from({ length: 3 })
    .map(
      () => `
    <div class="rounded-xl2 border border-linen p-4 flex gap-3 items-center">
      <div class="shimmer w-11 h-11 rounded-full shrink-0"></div>
      <div class="flex-1 flex flex-col gap-2">
        <div class="shimmer h-3.5 w-2/5 rounded-full"></div>
        <div class="shimmer h-3 w-4/5 rounded-full"></div>
      </div>
    </div>`
    )
    .join("");
}

function skeletonSummary() {
  $("summaryStrip").innerHTML = Array.from({ length: 4 })
    .map(() => `
      <div class="rounded-xl2 border border-linen p-4 flex gap-3 items-center">
        <div class="shimmer w-10 h-10 rounded-full shrink-0"></div>
        <div class="flex-1 flex flex-col gap-2">
          <div class="shimmer h-2.5 w-1/3 rounded-full"></div>
          <div class="shimmer h-4 w-1/4 rounded-full"></div>
          <div class="shimmer h-2.5 w-2/3 rounded-full"></div>
        </div>
      </div>
    `).join("");
}

async function loadDashboard() {
  try {
    const r = await fetch(`/api/dashboard?t=${Date.now()}`, { cache: "no-store" });
    const data = await r.json();
    state.dashboardData = data;
    state.plants = data.plants || [];

    // Show weather in header
    if (data.summary && data.summary.weather && data.location) {
      showWeather(data.summary.weather, data.location);
    } else if (data.location) {
      $("locationName").textContent = data.location;
      $("locationChip").classList.remove("hidden");
      $("locationChip").classList.add("flex");
    }

    // Render summary strip
    renderSummaryStrip(data.summary, data.location);

    // Render plant cards
    renderDashboardPlants();
  } catch (_) {
    $("plantList").innerHTML =
      `<div class="text-sm text-ink-muted px-2 py-4 col-span-full">Couldn't load your plants right now.</div>`;
  }
}

function renderDashboardPlants() {
  const list = $("plantList");
  const count = $("plantCount");
  const plants = state.plants;

  if (!plants.length) {
    count.classList.add("hidden");
    list.innerHTML = `
      <div class="col-span-full flex flex-col items-center justify-center text-center px-6 py-16 gap-3">
        <div class="relative">
          <div class="w-24 h-24 rounded-full bg-sage-tint border border-linen flex items-center justify-center">
            <i data-lucide="sprout" class="w-10 h-10 text-sage-deep"></i>
          </div>
          <div class="absolute -right-1 -bottom-1 w-9 h-9 rounded-full bg-amber-tint border border-linen flex items-center justify-center">
            <i data-lucide="sun" class="w-5 h-5 text-amber"></i>
          </div>
        </div>
        <div class="font-display font-semibold text-lg text-leaf-dark mt-1">No plants yet</div>
        <p class="text-sm text-ink-muted leading-relaxed max-w-xs">Your little indoor jungle starts here. Add your first plant and Leafy will keep an eye on it.</p>
        <button id="emptyAddBtn" class="mt-1 inline-flex items-center gap-1.5 bg-leaf hover:bg-leaf-dark text-cream text-sm font-medium rounded-full px-4 py-2 transition active:scale-95 shadow-soft">
          <i data-lucide="plus" class="w-4 h-4"></i> Add your first plant
        </button>
      </div>`;
    icons();
    const btn = $("emptyAddBtn");
    if (btn) btn.addEventListener("click", () => { openChat(); quickAction("add"); });
    return;
  }

  count.textContent = plants.length;
  count.classList.remove("hidden");

  // Water status chip styles
  const waterStyles = {
    due:     { bg: "bg-amber-tint", text: "text-amber", icon: "droplet", border: "border-amber/20" },
    soon:    { bg: "bg-amber-tint", text: "text-amber", icon: "droplet", border: "border-amber/20" },
    ok:      { bg: "bg-leaf-soft", text: "text-leaf-dark", icon: "droplet", border: "border-leaf/20" },
    unknown: { bg: "bg-cream", text: "text-ink-muted", icon: "help-circle", border: "border-linen" },
  };

  // Shelter action chip styles
  const shelterStyles = {
    move_indoors:  { bg: "bg-amber-tint", text: "text-amber", icon: "home", border: "border-amber/20" },
    move_outdoors: { bg: "bg-leaf-soft", text: "text-leaf-dark", icon: "tree-pine", border: "border-leaf/20" },
    keep_as_is:    { bg: "bg-sage-tint", text: "text-sage-deep", icon: "check", border: "border-sage/30" },
  };

  list.innerHTML = plants
    .map((p) => {
      const displayName = p.nickname || p.species;
      const sci = (p.care && p.care.scientific_name) || "";
      const placeIcon = p.placement === "outdoor" ? "tree-pine" : "home";

      // Water chip
      const ws = p.water || { status: "unknown", label: "Unknown" };
      const wst = waterStyles[ws.status] || waterStyles.unknown;

      // Shelter chip
      const sh = p.shelter;
      const shStyle = sh ? (shelterStyles[sh.action] || shelterStyles.keep_as_is) : null;

      return `
      <div data-plant-id="${p.id}"
        class="plant-card card-transition text-left rounded-xl2 border border-linen bg-paper hover:shadow-lift hover:-translate-y-0.5 p-4 flex flex-col gap-3 relative group cursor-pointer"
        id="plant_card_${p.id}">

        <!-- Trash button (visible on hover) -->
        <button data-action="delete" data-plant-id="${p.id}" data-plant-name="${escapeHtml(displayName)}"
                class="plant-delete-btn absolute top-3.5 right-3.5 w-7 h-7 rounded-full bg-paper border border-linen/80 text-ink-muted hover:bg-amber-tint hover:border-amber/40 hover:text-amber flex items-center justify-center transition opacity-0 group-hover:opacity-100 z-10"
                title="Delete plant">
          <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
        </button>

        <!-- Plant info header -->
        <div class="flex gap-3 items-start w-full">
          <div class="w-11 h-11 rounded-full bg-sage-tint border border-linen flex items-center justify-center shrink-0">
            <i data-lucide="${plantGlyph(p.species)}" class="w-5 h-5 text-leaf"></i>
          </div>
          <div class="min-w-0 flex-1">
            <div class="font-semibold text-[15px] leading-tight truncate pr-2">${escapeHtml(displayName)}</div>
            ${sci ? `<div class="text-xs italic text-ink-faint truncate mt-0.5">${escapeHtml(sci)}</div>` : ""}
            <div class="flex flex-wrap gap-1.5 mt-1.5">
              <span class="pill"><i data-lucide="${placeIcon}" class="w-3 h-3"></i>${p.placement}</span>
            </div>
          </div>
        </div>

        <!-- Status chips -->
        <div class="flex flex-wrap gap-2">
          ${ws.status !== "unknown" ? `
            <span class="status-chip ${wst.bg} ${wst.text} border ${wst.border}">
              <i data-lucide="${wst.icon}" class="w-3.5 h-3.5"></i>
              ${escapeHtml(ws.label)}
            </span>
          ` : ""}
          ${sh ? `
            <span class="status-chip ${shStyle.bg} ${shStyle.text} border ${shStyle.border}" title="${escapeHtml(sh.reason || "")}">
              <i data-lucide="${shStyle.icon}" class="w-3.5 h-3.5"></i>
              ${escapeHtml(sh.label)}
            </span>
          ` : ""}
        </div>

        <!-- Action buttons -->
        <div class="flex items-center gap-1.5 pt-2 border-t border-linen/60">
          <button data-action="water" data-plant-name="${escapeHtml(displayName)}"
                  class="plant-action-btn px-2.5 py-1 text-[11px] font-semibold bg-leaf-soft text-leaf-dark rounded-full hover:bg-leaf hover:text-cream transition">
            <i data-lucide="droplet" class="w-3 h-3 inline-block -mt-px"></i> Water
          </button>
          <button data-action="move" data-plant-name="${escapeHtml(displayName)}"
                  class="plant-action-btn px-2.5 py-1 text-[11px] font-semibold bg-cream text-ink-muted rounded-full border border-linen/60 hover:bg-sage-tint hover:text-leaf-dark transition">
            <i data-lucide="arrow-right-left" class="w-3 h-3 inline-block -mt-px"></i> Move?
          </button>
          <button data-action="where" data-plant-name="${escapeHtml(displayName)}"
                  class="plant-action-btn px-2.5 py-1 text-[11px] font-semibold bg-cream text-ink-muted rounded-full border border-linen/60 hover:bg-sage-tint hover:text-leaf-dark transition">
            Best spot?
          </button>
        </div>
      </div>`;
    })
    .join("");

  // Wire action buttons to open chat and send a question
  list.querySelectorAll(".plant-action-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const action = btn.dataset.action;
      const plantName = btn.dataset.plantName;
      openChat();
      if (action === "water") {
        submitText(`When should I water my ${plantName}?`);
      } else if (action === "where") {
        submitText(`Where should my ${plantName} go?`);
      } else if (action === "move") {
        submitText(`Should I move my ${plantName} indoors or outdoors today?`);
      }
    });
  });

  // Wire delete buttons
  let plantIdToDelete = null;
  list.querySelectorAll(".plant-delete-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = btn.dataset.plantId;
      const name = btn.dataset.plantName;
      plantIdToDelete = id;
      
      $("deleteConfirmText").textContent = `Delete ${name}? This can't be undone.`;
      const dialog = $("deleteConfirmDialog");
      dialog.classList.remove("hidden");
      dialog.classList.add("flex");
    });
  });

  $("deleteCancelBtn").onclick = () => {
    const dialog = $("deleteConfirmDialog");
    dialog.classList.add("hidden");
    dialog.classList.remove("flex");
    plantIdToDelete = null;
  };

  $("deleteConfirmBtn").onclick = async () => {
    if (!plantIdToDelete) return;
    const dialog = $("deleteConfirmDialog");
    dialog.classList.add("hidden");
    dialog.classList.remove("flex");
    
    try {
      const response = await fetch(`/api/plants/${plantIdToDelete}`, {
        method: "DELETE"
      });
      if (response.ok) {
        // Refresh dashboard
        await loadDashboard();
      } else {
        alert("Failed to delete plant");
      }
    } catch (err) {
      console.error(err);
      alert("Error deleting plant");
    } finally {
      plantIdToDelete = null;
    }
  };

  icons();
}

function selectedPlant() {
  return state.plants.find((p) => p.id === state.selectedPlantId) || null;
}

/* ------------------------------------------------------------- session */

async function ensureSession() {
  const saved = sessionStorage.getItem(SESSION_KEY);
  if (saved) {
    try {
      const r = await fetch(`/apps/${APP_NAME}/users/${USER_ID}/sessions/${saved}`);
      if (r.ok) {
        const session = await r.json();
        state.sessionId = saved;
        renderHistory(session.events || []);
        return;
      }
    } catch (_) { /* fall through to create */ }
  }
  const r = await fetch(`/apps/${APP_NAME}/users/${USER_ID}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  const session = await r.json();
  state.sessionId = session.id;
  sessionStorage.setItem(SESSION_KEY, session.id);
}

function renderHistory(events) {
  let any = false;
  for (const ev of events) {
    const parts = (ev.content && ev.content.parts) || [];
    const text = parts.filter((p) => p.text).map((p) => p.text).join("\n");
    if (!text) continue;
    if (ev.content.role === "user" || ev.author === "user") {
      // Function responses ride on role=user events; only render typed text.
      if (parts.some((p) => p.functionResponse)) continue;
      addUserBubble(text, null, false);
    } else {
      addLeafyBubble(renderMarkdown(text), false);
    }
    any = true;
  }
  if (any) scrollToBottom();
}

/* ------------------------------------------------------------ messages */

function addLeafyBubble(html, animate = true) {
  const wrap = document.createElement("div");
  wrap.className = `flex items-end gap-2.5 max-w-[92%] sm:max-w-[80%] ${animate ? "msg-enter" : ""}`;
  wrap.innerHTML = `
    <div class="w-7 h-7 rounded-full bg-leaf-soft border border-linen flex items-center justify-center shrink-0">
      <i data-lucide="leaf" class="w-3.5 h-3.5 text-leaf"></i>
    </div>
    <div class="bubble-md bg-sage-tint border border-linen rounded-2xl rounded-bl-md px-4 py-3 text-[15px] leading-relaxed"></div>`;

  const bubble = wrap.querySelector(".bubble-md");
  bubble.innerHTML = html;

  if (typeof parseShelterTextAndRender === "function") {
    parseShelterTextAndRender(bubble);
  }

  messagesEl.appendChild(wrap);
  icons();
  return bubble;
}

function addUserBubble(text, imageUrl, animate = true) {
  const wrap = document.createElement("div");
  wrap.className = `self-end flex flex-col items-end gap-1.5 max-w-[92%] sm:max-w-[80%] ${animate ? "msg-enter" : ""}`;
  let inner = "";
  if (imageUrl) {
    inner += `<img src="${imageUrl}" class="rounded-xl2 max-h-44 border border-leaf-dark/20 shadow-soft" alt="your photo" />`;
  }
  if (text) {
    inner += `<div class="bg-leaf text-cream rounded-2xl rounded-br-md px-4 py-3 text-[15px] leading-relaxed whitespace-pre-wrap">${escapeHtml(text)}</div>`;
  }
  wrap.innerHTML = inner;
  messagesEl.appendChild(wrap);
}

function addErrorBubble(msg) {
  const el = addLeafyBubble(
    `<p>Hmm, something went wrong on my end. ${escapeHtml(msg || "Mind trying again in a moment?")}</p>`
  );
  el.classList.add("!bg-amber-tint");
}

/* Tool activity chips */
const TOOL_LABELS = {
  get_weather: "Checking the weather…",
  list_plants: "Looking up your catalog…",
  get_plant: "Looking up your catalog…",
  add_plant: "Saving to your catalog…",
  update_plant_state: "Updating your plant…",
  get_or_create_profile: "Checking your profile…",
  update_location: "Saving your location…",
  geocode: "Finding your location…",
  plant_kb_lookup: "Reading plant care notes…",
  estimate_weather_tolerance: "Estimating hardiness…",
  watering_reasoner: "Working out a watering plan…",
  shelter_advisor: "Checking the forecast for your plants…",
  estimate_spot_light: "Measuring the light in that spot…",
  recommend_plants_for_light: "Matching plants to the light…",
};

const activeChips = new Map(); // tool name -> element
let currentTurnChips = [];

function addToolChip(name) {
  if (name === "adk_request_input") return;
  if (activeChips.has(name)) return; // stream can emit the same call twice
  const label = TOOL_LABELS[name] || "Working on it…";
  const chip = document.createElement("div");
  chip.className = "msg-enter self-start ml-10 inline-flex items-center gap-2 bg-cream border border-linen rounded-full px-3 py-1.5 text-xs font-medium text-ink-muted";
  chip.innerHTML = `<i data-lucide="loader-circle" class="chip-spin w-3.5 h-3.5 text-leaf"></i><span>${label}</span>`;
  messagesEl.appendChild(chip);
  activeChips.set(name, chip);
  currentTurnChips.push(chip);
  icons();
  scrollToBottom();
}

function resolveToolChip(name) {
  const chip = activeChips.get(name);
  if (!chip) return;
  activeChips.delete(name);
  const spinner = chip.querySelector(".chip-spin");
  if (spinner) {
    spinner.outerHTML = `<i data-lucide="check" class="w-3.5 h-3.5 text-leaf"></i>`;
    icons();
  }
  setTimeout(() => {
    chip.style.transition = "opacity .4s ease";
    chip.style.opacity = "0.55";
  }, 600);
}

function setTyping(on) {
  $("typing").classList.toggle("hidden", !on);
  if (on) messagesEl.parentElement && scrollToBottom();
}

/* ---------------------------------------------------------- chat turn & structured rendering */

function formatTraceStep(t) {
  const name = t.name;

  switch (name) {
    case "geocode":
      return "Found your location";
    case "get_or_create_profile":
      return "Checked your saved location";
    case "update_location":
      return "Saved your location";
    case "get_weather":
      return "Checked weather forecast";
    case "watering_reasoner":
      return "Calculated watering window";
    case "shelter_advisor":
      return "Evaluated weather suitability & shelter needs for all plants";
    case "estimate_spot_light":
      return "Measured light at spot";
    case "list_plants":
    case "get_plant":
      return "Looked at your plants";
    case "update_plant_state":
      return "Updated your plant";
    case "plant_kb_lookup":
      return "Looked up care info";
    case "estimate_weather_tolerance":
      return "Estimated care needs";
    case "recommend_plants_for_light":
      return "Matched plants to light";
    case "add_plant":
      return "Saved new plant to database";
    default:
      return "Working…";
  }
}

function parseShelterTextAndRender(bubble) {
  const text = bubble.innerText || "";
  if (text.includes("Shelter Advisor for")) {
    const lines = text.split("\n");
    const assessments = [];
    let titleText = "Shelter recommendations";
    for (const line of lines) {
      if (line.startsWith("Shelter Advisor for")) {
        titleText = line;
      }
      const match = line.match(/^-\s+\*\*(.+?)\*\*\s+\((.+?),\s+currently\s+(.+?)\):\s*([^,]+)(?:,\s*(.+))?$/i);
      if (match) {
        assessments.push({
          name: match[1],
          species: match[2],
          placement: match[3],
          action: match[4],
          reason: match[5] || ""
        });
      }
    }

    if (assessments.length > 0) {
      const badgeStyle = (action) => {
        const act = action.toLowerCase();
        if (act.includes("indoor")) return "bg-amber-tint text-amber border-amber/20";
        if (act.includes("outdoor")) return "bg-leaf-soft text-leaf-dark border-leaf/20";
        return "bg-cream text-ink-muted border-linen";
      };

      const plantsListHtml = assessments.map(item => `
        <div class="flex items-start justify-between gap-3 border-b border-linen last:border-0 pb-2.5 last:pb-0 text-ink">
          <div class="min-w-0">
            <div class="font-semibold text-sm">${escapeHtml(item.name)} <span class="text-xs font-normal text-ink-muted">(${escapeHtml(item.species)})</span></div>
            <div class="text-xs text-ink-muted mt-0.5">${escapeHtml(item.reason)}</div>
          </div>
          <span class="shrink-0 text-xs px-2.5 py-1 rounded-full font-semibold border ${badgeStyle(item.action)}">
            ${escapeHtml(item.action)}
          </span>
        </div>
      `).join("");

      bubble.innerHTML = `
        <div class="flex items-center gap-3 mb-3 text-ink">
          <div class="w-10 h-10 rounded-full bg-sage-tint flex items-center justify-center shrink-0">
            <i data-lucide="cloud-sun" class="w-5.5 h-5.5 text-leaf"></i>
          </div>
          <div>
            <div class="text-[11px] text-ink-muted uppercase font-bold tracking-wider">Shelter Advisor</div>
            <div class="font-semibold text-sm">${escapeHtml(titleText)}</div>
          </div>
        </div>
        <div class="flex flex-col gap-2.5 bg-paper border border-linen rounded-xl p-3.5 shadow-soft">
          ${plantsListHtml}
        </div>
        <div class="bg-cream border-l-4 border-leaf-ring rounded-r-lg p-3 text-xs text-ink-muted mt-3">
          <div class="font-semibold text-leaf mb-1 flex items-center gap-1">
            <i data-lucide="info" class="w-3.5 h-3.5"></i> Check it yourself
          </div>
          Always double-check actual conditions before moving plants.
        </div>
      `;
    }
  }
}

function processStructuredOutputs() {
  if (!streamEl) {
    if (state.currentTurnTools && state.currentTurnTools.length > 0) {
      streamEl = addLeafyBubble("");
    } else {
      return;
    }
  }

  const bubble = streamEl;

  // 1. Check for watering_reasoner response
  const waterTool = state.currentTurnTools && state.currentTurnTools.find(t => t.name === "watering_reasoner");
  if (waterTool && waterTool.response) {
    let r = waterTool.response;
    if (typeof r === "string") {
      try { r = JSON.parse(r); } catch (_) {}
    }
    if (r && r.next_watering_window) {
      bubble.innerHTML = `
        <div class="flex items-center gap-3 mb-3 text-ink">
          <div class="w-10 h-10 rounded-full bg-leaf-soft flex items-center justify-center shrink-0">
            <i data-lucide="droplet" class="w-5.5 h-5.5 text-leaf"></i>
          </div>
          <div>
            <div class="text-[11px] text-ink-muted uppercase font-bold tracking-wider">Watering Recommendation</div>
            <div class="font-semibold text-base text-leaf-dark">${escapeHtml(r.next_watering_window)}</div>
          </div>
        </div>
        <p class="text-sm text-ink-muted mb-3">${escapeHtml(r.reason)}</p>
        <div class="bg-cream border-l-4 border-leaf-ring rounded-r-lg p-3 text-xs text-ink-muted">
          <div class="font-semibold text-leaf mb-1 flex items-center gap-1">
            <i data-lucide="info" class="w-3.5 h-3.5"></i> Moisture Check Tip
          </div>
          ${escapeHtml(r.moisture_check)}
        </div>
      `;
    }
  }

  // 2. Check for shelter_advisor response (from text in the bubble)
  parseShelterTextAndRender(bubble);

  // 3. Check for spot check verify note in the text and convert to callout style
  const verifyNoteRegex = /<p>(?:<strong>)?Verify\s+note:?(?:<\/strong>)?:?\s*([\s\S]+?)<\/p>/i;
  const vMatch = bubble.innerHTML.match(verifyNoteRegex);
  if (vMatch) {
    const noteText = vMatch[1].trim();
    const calloutHtml = `
      <div class="bg-cream border-l-4 border-leaf-ring rounded-r-lg p-3 text-xs text-ink-muted mt-3">
        <div class="font-semibold text-leaf mb-1 flex items-center gap-1">
          <i data-lucide="info" class="w-3.5 h-3.5"></i> Check it yourself
        </div>
        ${noteText}
      </div>
    `;
    bubble.innerHTML = bubble.innerHTML.replace(vMatch[0], calloutHtml);
  }

  // 3. Render "How I worked this out" collapsible trace
  if (state.currentTurnTools && state.currentTurnTools.length > 0) {
    const steps = state.currentTurnTools.map(formatTraceStep).filter(Boolean);
    if (steps.length > 0) {
      const traceWrap = document.createElement("div");
      traceWrap.className = "mt-3 pt-3 border-t border-linen/60";

      const uniqueId = "trace_" + Math.random().toString(36).substr(2, 9);

      traceWrap.innerHTML = `
        <button id="${uniqueId}_btn" class="trace-toggle text-[11px] font-semibold text-leaf hover:text-leaf-dark flex items-center gap-1 focus:outline-none transition">
          <i id="${uniqueId}_icon" data-lucide="chevron-right" class="w-3 h-3 transition-transform duration-200"></i>
          <span>How I worked this out</span>
        </button>
        <div id="${uniqueId}_content" class="hidden mt-2 pl-3 border-l-2 border-sage text-[11px] text-ink-muted leading-relaxed flex flex-col gap-2">
          ${steps.map(step => `
            <div class="flex items-center gap-1.5">
              <span class="w-1.5 h-1.5 rounded-full bg-sage-deep shrink-0"></span>
              <span>${escapeHtml(step)}</span>
            </div>
          `).join("")}
        </div>
      `;
      bubble.appendChild(traceWrap);

      setTimeout(() => {
        const btn = document.getElementById(`${uniqueId}_btn`);
        const content = document.getElementById(`${uniqueId}_content`);
        const icon = document.getElementById(`${uniqueId}_icon`);

        if (btn && content && icon) {
          btn.addEventListener("click", () => {
            content.classList.toggle("hidden");
            icon.classList.toggle("rotate-90");
          });
        }
      }, 0);
    }
  }

  icons();
}

function cleanseLightTiers(text) {
  if (!text) return "";
  const mappings = {
    "0": "low light/shade",
    "1": "medium indirect light",
    "2": "bright indirect light",
    "3": "bright direct light"
  };
  return text.replace(/(?:light\s+)?tier\s+(?:of\s+)?([0-3])/gi, (match, num) => {
    return mappings[num] || match;
  });
}

function cleanseWeatherDetails(text) {
  if (!text) return "";
  const catMap = {
    "0": "sunny",
    "1": "cloudy",
    "2": "rainy",
    "3": "thunderstorm",
    "4": "snow"
  };
  
  // Remove category name followed by category number in parentheses (e.g., "snow (category 4)" -> "snow")
  let result = text.replace(/\b(sunny|cloudy|rain|rainy|thunderstorm|storm|snow|snowy)\s+\(category\s+[0-4]\)/gi, "$1");
  
  // Replace "(category X)" or "category X" (case-insensitive)
  result = result.replace(/(\(?)\bcategory\s+([0-4])(\)?)/gi, (match, openParen, num, closeParen) => {
    const word = catMap[num] || "unknown";
    return openParen === "(" ? `(${word})` : word;
  });

  // Replace "down to X°C" or "down to -X°C" (case-insensitive)
  result = result.replace(/\bdown\s+to\s+(-?\d+(?:\.\d+)?)\s*°C/gi, (match, tempStr) => {
    const tempVal = parseFloat(tempStr);
    if (tempVal < 0) {
      return "down to freezing conditions";
    } else if (tempVal <= 10) {
      return "down to cool temperatures";
    } else if (tempVal <= 15) {
      return "down to mild temperatures";
    } else {
      return "down to warm temperatures";
    }
  });

  return result;
}

let streamEl = null;
let streamText = "";
let turnErrorShown = false;

function handleEvent(evt) {
  if (evt.error || evt.errorCode) {
    setTyping(false);
    if (!turnErrorShown) {
      turnErrorShown = true;
      addErrorBubble(evt.errorMessage || evt.error || "");
    }
    return;
  }
  const parts = (evt.content && evt.content.parts) || [];
  for (const part of parts) {
    if (part.functionCall) {
      setTyping(false);
      addToolChip(part.functionCall.name);
      if (!state.currentTurnTools) state.currentTurnTools = [];
      const id = part.functionCall.id || part.functionCall.callId || part.functionCall.name;
      const existing = state.currentTurnTools.find(t => t.id === id);
      if (existing) {
        existing.args = part.functionCall.args;
      } else {
        state.currentTurnTools.push({
          id: id,
          name: part.functionCall.name,
          args: part.functionCall.args,
          response: null
        });
      }
    } else if (part.functionResponse) {
      resolveToolChip(part.functionResponse.name);
      setTyping(true);
      if (state.currentTurnTools) {
        const id = part.functionResponse.id || part.functionResponse.callId || part.functionResponse.name;
        const existing = state.currentTurnTools.find(t => t.id === id);
        if (existing) {
          existing.response = part.functionResponse.response;
        } else {
          const byName = state.currentTurnTools.findLast(t => t.name === part.functionResponse.name && t.response === null);
          if (byName) {
            byName.response = part.functionResponse.response;
          } else {
            state.currentTurnTools.push({
              id: id,
              name: part.functionResponse.name,
              args: null,
              response: part.functionResponse.response
            });
          }
        }
      }
    } else if (part.text && !part.thought) {
      setTyping(false);
      if (evt.partial) {
        streamText += part.text;
        if (!streamEl) streamEl = addLeafyBubble("");
        streamEl.innerHTML = renderMarkdown(cleanseWeatherDetails(cleanseLightTiers(streamText)));
      } else {
        if (!streamEl) streamEl = addLeafyBubble("");
        streamEl.innerHTML = renderMarkdown(cleanseWeatherDetails(cleanseLightTiers(part.text)));
        streamText = "";
      }
      scrollToBottom();
    }
  }
}

async function sendTurn(text, image) {
  if (state.busy || (!text && !image)) return;
  state.busy = true;
  turnErrorShown = false;
  $("sendBtn").disabled = true;

  state.currentTurnTools = [];
  currentTurnChips = [];

  addUserBubble(text, image ? image.url : null);
  scrollToBottom();
  setTyping(true);

  const parts = [];
  if (image) parts.push({ inlineData: { mimeType: image.mimeType, data: image.data } });
  if (text) parts.push({ text });

  try {
    const res = await fetch("/run_sse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        appName: APP_NAME,
        userId: USER_ID,
        sessionId: state.sessionId,
        newMessage: { role: "user", parts },
        streaming: true,
      }),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        for (const line of frame.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          try {
            handleEvent(JSON.parse(line.slice(6)));
          } catch (_) { /* skip malformed frame */ }
        }
      }
    }
  } catch (err) {
    addErrorBubble(String(err.message || err));
  } finally {
    setTyping(false);
    for (const name of [...activeChips.keys()]) resolveToolChip(name);

    // Remove tool chips from DOM once the response has arrived
    for (const chip of currentTurnChips) {
      if (chip.parentNode) {
        chip.parentNode.removeChild(chip);
      }
    }
    currentTurnChips = [];
    activeChips.clear();

    processStructuredOutputs();

    streamEl = null;
    streamText = "";
    state.busy = false;
    $("sendBtn").disabled = false;
    scrollToBottom();
    loadDashboard(); // dashboard may have changed during the turn
  }
}

/* -------------------------------------------------------- quick actions */

function quickAction(kind) {
  const plant = selectedPlant();
  const name = plant ? plant.nickname || plant.species : null;
  switch (kind) {
    case "add":
      submitText("I'd like to add a new plant to my catalog.");
      break;
    case "water":
      submitText(name ? `When should I water my ${name}?` : "When should I water my plants?");
      break;
    case "move":
      submitText(
        name
          ? `Should I move my ${name} indoors or outdoors today?`
          : "Should I move any of my plants indoors or outdoors today?"
      );
      break;
    case "spot":
      if (state.pendingImage) {
        submitText("What could I grow in this spot?");
      } else {
        $("fileInput").dataset.spotcheck = "1";
        $("fileInput").click();
      }
      break;
  }
}

function submitText(text) {
  const image = state.pendingImage;
  clearImage();
  sendTurn(text, image);
}

/* ------------------------------------------------------------- images */

function setImage(file) {
  if (!file || !file.type.startsWith("image/")) return;
  const reader = new FileReader();
  reader.onload = () => {
    const url = reader.result;
    const data = String(url).split(",", 2)[1];
    state.pendingImage = { mimeType: file.type, data, url };
    $("previewImg").src = url;
    $("imagePreview").classList.remove("hidden");
    if ($("fileInput").dataset.spotcheck === "1") {
      delete $("fileInput").dataset.spotcheck;
      $("input").value = "What could I grow in this spot?";
    }
    $("input").focus();
  };
  reader.readAsDataURL(file);
}

function clearImage() {
  state.pendingImage = null;
  $("imagePreview").classList.add("hidden");
  $("previewImg").src = "";
  $("fileInput").value = "";
}

/* ---------------------------------------------------- chat panel toggle */

function openChat() {
  const panel = $("chatPanel");
  const backdrop = $("chatBackdrop");
  state.chatOpen = true;

  // Desktop: show inline
  if (window.innerWidth >= 1024) {
    panel.classList.remove("hidden");
    panel.classList.add("flex", "chat-panel-enter");
    setTimeout(() => panel.classList.remove("chat-panel-enter"), 300);
  } else {
    // Mobile: slide-out drawer
    panel.classList.remove("hidden");
    panel.classList.add(
      "flex", "fixed", "inset-y-0", "right-0", "z-40", "w-[90%]", "max-w-lg", "shadow-lift", "chat-panel-enter"
    );
    backdrop.classList.remove("hidden");
  }

  $("chatToggle").innerHTML = `<i data-lucide="x" class="w-4 h-4"></i><span class="hidden sm:inline">Close</span>`;
  icons();
  scrollToBottom();
}

function closeChat() {
  const panel = $("chatPanel");
  const backdrop = $("chatBackdrop");
  state.chatOpen = false;

  backdrop.classList.add("hidden");
  panel.classList.add("hidden");
  panel.classList.remove(
    "flex", "fixed", "inset-y-0", "right-0", "z-40", "w-[90%]", "max-w-lg", "shadow-lift", "chat-panel-enter"
  );

  $("chatToggle").innerHTML = `<i data-lucide="message-circle" class="w-4 h-4"></i><span class="hidden sm:inline">Ask Leafy</span>`;
  icons();
}

function toggleChat() {
  state.chatOpen ? closeChat() : openChat();
}

/* --------------------------------------------------------------- wiring */

function wireComposer() {
  const input = $("input");
  const form = $("composer");

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text && !state.pendingImage) return;
    input.value = "";
    input.style.height = "auto";
    submitText(text);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 144) + "px";
  });

  $("attachBtn").addEventListener("click", () => $("fileInput").click());
  $("fileInput").addEventListener("change", (e) => setImage(e.target.files[0]));
  $("removeImage").addEventListener("click", clearImage);

  $("quickActions").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-qa]");
    if (btn) quickAction(btn.dataset.qa);
  });

  // drag & drop over the chat panel
  const chatEl = $("chatPanel");
  const overlay = $("dropOverlay");
  let dragDepth = 0;
  chatEl.addEventListener("dragenter", (e) => {
    e.preventDefault();
    dragDepth++;
    overlay.classList.remove("hidden");
    overlay.classList.add("flex");
  });
  chatEl.addEventListener("dragover", (e) => e.preventDefault());
  chatEl.addEventListener("dragleave", () => {
    if (--dragDepth <= 0) {
      dragDepth = 0;
      overlay.classList.add("hidden");
      overlay.classList.remove("flex");
    }
  });
  chatEl.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    overlay.classList.add("hidden");
    overlay.classList.remove("flex");
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) setImage(file);
  });
}

function wireChatToggle() {
  $("chatToggle").addEventListener("click", toggleChat);

  const closeBtn = $("chatClose");
  if (closeBtn) closeBtn.addEventListener("click", closeChat);

  $("chatBackdrop").addEventListener("click", closeChat);

  window.matchMedia("(min-width: 1024px)").addEventListener("change", (m) => {
    if (m.matches && state.chatOpen) {
      // Switch from mobile drawer to inline
      const panel = $("chatPanel");
      $("chatBackdrop").classList.add("hidden");
      panel.classList.remove("fixed", "inset-y-0", "right-0", "z-40", "w-[90%]", "max-w-lg", "shadow-lift");
    }
  });
}

/* ----------------------------------------------------------------- init */

async function init() {
  icons();
  skeletonSummary();
  skeletonCards();
  wireComposer();
  wireChatToggle();
  closeChat(); // Initialize closed state

  await Promise.all([loadDashboard(), ensureSession()]);

  if (!messagesEl.children.length) {
    addLeafyBubble(
      renderMarkdown(
        "Hi, I'm **Leafy** 🌿 I can keep track of your plants, tell you when to water them, warn you when one should come indoors, and even judge a spot from a photo.\n\nWhat would you like to do?"
      ),
      false
    );
  }
  scrollToBottom();
}

init();
