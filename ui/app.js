/* Dicteer - window front-end.
 *
 * Talks to Python through window.pywebview.api (methods return promises) and
 * receives pushed events through window.dictate.push({type, value}). When the
 * page is opened in a plain browser for design work there is no bridge, so a
 * mock with demo data takes its place.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    settings: {},
    history: [],
    devices: [],
    stats: {},
    learning: { corrections: [], vocabulary: [], total: 0 },
    editingId: null,
    autostart: false,
    listening: true,
    current: "idle",
    hotkeyLabel: "F9",
    version: "2.0.12",
    models: [],
    cloud: { backend: "local", has_key: false, keys: {}, providers: {}, model: "", gemini_model: "", gemini_models: [], free_limits: "", keys_url: "" },
    configPath: "",
    modelDir: "",
    crashLogPath: "",
    crash: { pending: false, path: "" },
    crashDismissed: false,
    storageWarnings: [],
    last: null,
    level: 0,
    partial: "",
    recordingStarted: 0,
    capture: false,
  };

  const LANGUAGE_LABELS = {
    nl: "Nederlands",
    en: "Engels",
    de: "Duits",
    fr: "Frans",
    es: "Spaans",
    it: "Italiaans",
    pt: "Portugees",
    auto: "Automatisch herkennen",
  };

  const MODE_LABELS = { paste: "geplakt", type: "getypt", clipboard: "op het klembord" };
  const MODE_CHIPS = { paste: "Plakken", type: "Typen", clipboard: "Klembord" };

  const MODEL_NOTES = {
    tiny: "Snelst, laagste nauwkeurigheid.",
    base: "Snel, goed genoeg voor korte zinnen.",
    small: "Aanbevolen: beste balans tussen snelheid en nauwkeurigheid.",
    "large-v3-turbo": "Bijna large-v3 kwaliteit, veel sneller. Eenmalig ~1,6 GB downloaden.",
    medium: "Nauwkeuriger, maar duidelijk trager.",
    "large-v3": "Hoogste nauwkeurigheid, veel trager en zwaarder.",
  };

  const MODEL_ORDER = ["tiny", "base", "small", "large-v3-turbo", "medium", "large-v3"];

  /* ------------------------------------------------------------------ api */
  const mock = createMock();
  let api = mock;
  let bridged = false;
  let booted = false;

  function attachBridge() {
    if (bridged) return true;
    const candidate = window.pywebview && window.pywebview.api;
    if (!candidate) return false;
    bridged = true;
    api = candidate;
    return true;
  }

  function start() {
    // With a bridge, Python pushes the boot payload once the page is loaded;
    // the page must not call into Python before the loop is up.
    if (attachBridge()) return;
    let attempts = 0;
    const timer = setInterval(() => {
      attempts += 1;
      if (attachBridge()) {
        clearInterval(timer);
      } else if (attempts >= 6) {
        clearInterval(timer);
        document.body.dataset.mock = "true";
        bootMock();
      }
    }, 250);
  }

  async function bootMock() {
    if (booted) return;
    try {
      const data = await api.boot();
      applyBoot(data);
      logJs("preview mode (no bridge)");
    } catch (err) {
      toast("Kon de app niet laden: " + (err && err.message ? err.message : err), "error");
    }
  }

  window.dictate = {
    push(event) {
      if (!event || !event.type) return;
      handleEvent(event);
    },
  };

  function logJs(message) {
    if (bridged && api.log_js) api.log_js(message);
  }

  /* ---------------------------------------------------------------- boot */
  function applyBoot(data) {
    booted = true;
    state.settings = data.settings || {};
    state.history = data.history || [];
    state.devices = data.devices || [];
    state.stats = data.stats || computeStats(state.history);
    state.learning = data.learning || { corrections: [], vocabulary: [], total: 0 };
    state.cloud = data.cloud || state.cloud;
    state.autostart = Boolean(data.autostart);
    state.listening = Boolean(data.listening);
    state.current = data.state || "idle";
    state.hotkeyLabel = prettyHotkey(state.settings.hotkey || (data.hotkey_label ? `<${String(data.hotkey_label).toLowerCase()}>` : "<f9>"));
    state.version = data.version || state.version;
    state.models = data.models || [];
    state.configPath = data.config_path || "";
    state.modelDir = data.model_dir || "";
    state.crashLogPath = data.crash_log_path || "";
    state.crash = data.crash || { pending: false, path: state.crashLogPath };
    state.crashDismissed = false;
    state.storageWarnings = Array.isArray(data.storage_warnings)
      ? data.storage_warnings.map((item) => String(item || "").trim()).filter(Boolean)
      : [];
    document.body.dataset.booted = "1";
    if (state.current === "recording") state.recordingStarted = Date.now();
    renderAll();
    startClock();
  }

  function renderAll() {
    renderCrashNotice();
    renderStorageNotice();
    renderMicNotice();
    renderBrand();
    renderStatus();
    renderChips();
    renderTranscript();
    renderStats();
    renderRecent();
    renderHistory();
    renderSettings();
    renderLearning();
    renderCloud();
    renderAbout();
  }

  /* -------------------------------------------------------------- events */
  function handleEvent(event) {
    switch (event.type) {
      case "boot":
        applyBoot(event.value || {});
        logJs(`ui booted ${state.version} ${location.hash || "#dictate"}`);
        break;
      case "state":
        state.current = event.value;
        if (typeof event.listening === "boolean") state.listening = event.listening;
        if (event.value === "recording") {
          state.partial = "";
          state.recordingStarted = Date.now();
          setTranscript("");
          setLiveNote("Laat los om te plakken · de definitieve tekst komt daarna.");
        }
        if (event.value === "idle") {
          state.recordingStarted = 0;
          $("timer").hidden = true;
        }
        renderStatus();
        break;
      case "listening":
        state.listening = Boolean(event.value);
        $("listening-toggle").checked = state.listening;
        renderStatus();
        break;
      case "partial":
        state.partial = event.value || "";
        setTranscript(state.partial);
        break;
      case "level":
        state.level = Number(event.value) || 0;
        renderMeter();
        break;
      case "delivered":
        onDelivered(event.value);
        break;
      case "history":
        state.history = event.value || [];
        state.stats = computeStats(state.history);
        renderStats();
        renderRecent();
        renderHistory();
        break;
      case "settings":
        state.settings = event.value || state.settings;
        renderSettings();
        renderChips();
        renderMicNotice();
        renderCloud();
        renderAbout();
        renderStatus();
        break;
      case "cloud":
        state.cloud = event.value || state.cloud;
        renderCloud();
        renderBrand();
        renderAbout();
        break;
      case "storage_warnings":
        state.storageWarnings = Array.isArray(event.value)
          ? event.value.map((item) => String(item || "").trim()).filter(Boolean)
          : [];
        renderStorageNotice();
        break;
      case "toast":
        toast(event.value && event.value.text, event.value && event.value.kind);
        break;
      default:
        break;
    }
  }

  function onDelivered(item) {
    if (!item) return;
    state.last = item;
    if (Array.isArray(item.history)) state.history = item.history;
    state.stats = item.stats || computeStats(state.history);
    state.partial = "";
    state.current = "idle";
    state.recordingStarted = 0;
    setTranscript(item.text || "", true);
    const seconds = Number(item.duration || 0);
    const meta = [new Date().toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" })];
    if (seconds) meta.push(`${seconds.toFixed(1)} s spraak`);
    if (item.mode) meta.push(MODE_LABELS[item.mode] || item.mode);
    setLiveNote(meta.join(" · "));
    $("timer").hidden = true;
    renderStatus();
    renderStats();
    renderRecent();
    renderHistory();
    renderMeter();
    const words = String(item.text || "").trim().split(/\s+/).filter(Boolean).length;
    toast(words ? `Geplakt · ${words} woorden` : "Geplakt", "ok");
  }

  /* ------------------------------------------------------- crash notice */
  function renderCrashNotice() {
    const notice = $("crash-notice");
    const data = state.crash || {};
    const pending = Boolean(data.pending) && !state.crashDismissed;
    notice.hidden = !pending;
    if (!pending) return;
    const bits = [];
    const started = Number(data.started);
    if (started) {
      const when = new Date(started * 1000);
      if (!Number.isNaN(when.getTime())) {
        bits.push(`Vorige sessie: ${when.toLocaleString("nl-NL", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}.`);
      }
    }
    bits.push("De foutmelding staat in crash.log.");
    $("crash-notice-detail").textContent = bits.join(" ");
  }

  /* -------------------------------------------------------- storage notice */
  function renderStorageNotice() {
    const notice = $("storage-notice");
    if (!notice) return;
    const warnings = (state.storageWarnings || []).filter(Boolean);
    notice.hidden = warnings.length === 0;
    if (!warnings.length) return;
    $("storage-notice-detail").textContent = warnings.join(" ");
  }

  /* ------------------------------------------------------ microphone notice */
  function configuredMicrophone() {
    const raw = state.settings && state.settings.input_device;
    if (raw === null || raw === undefined) return "";
    const name = String(raw).trim();
    if (!name || name.toLowerCase() === "default") return "";
    return name;
  }

  function renderMicNotice() {
    const notice = $("mic-notice");
    if (!notice) return;
    const name = configuredMicrophone();
    const missing = name && !findDevice(name);
    notice.hidden = !missing;
    if (!missing) return;
    $("mic-notice-detail").textContent =
      `“${name}” is niet gevonden. Sluit hem aan of kies een andere microfoon.`;
  }

  /* -------------------------------------------------------------- brand */
  function renderBrand() {
    const language = state.settings.language || "auto";
    const label = LANGUAGE_LABELS[language] || language;
    const online = state.cloud && state.cloud.backend !== "local" && state.cloud.has_key;
    $("brand-sub").textContent = `${label} · ${online ? "online" : "offline"}`;
    document.querySelectorAll("#hotkey-kbd").forEach((el) => {
      el.textContent = state.hotkeyLabel;
    });
    $("hotkey-side").textContent = state.hotkeyLabel;
    $("listening-toggle").checked = state.listening;
  }

  /* ------------------------------------------------------------- status */
  function renderStatus() {
    const hold = state.settings.hold_to_talk !== false;
    const hotkey = state.hotkeyLabel;
    document.body.dataset.state = state.current;
    document.body.dataset.listening = String(state.listening);

    let kicker = "Klaar om te dicteren";
    let hint = hold
      ? `Houd <kbd>${hotkey}</kbd> ingedrukt terwijl je spreekt.`
      : `Druk op <kbd>${hotkey}</kbd> om te beginnen en nog eens om te stoppen.`;
    let label = "Opnemen";

    if (!state.listening) {
      kicker = "Sneltoets staat uit";
      hint = "Zet de sneltoets linksonder weer aan, of gebruik de knop.";
    }
    if (state.current === "recording") {
      kicker = "Aan het luisteren";
      hint = hold ? "Laat los om te plakken." : `Druk op <kbd>${hotkey}</kbd> om te stoppen.`;
      label = "Stop en plak";
    } else if (state.current === "transcribing") {
      kicker = "Bezig met omzetten";
      hint = "Het model werkt aan je zin.";
      label = "Bezig…";
    }

    // Only replay the entrance when the line actually changes, so the hero
    // does not twitch while a dictation is running.
    const kickerNode = $("status-kicker");
    if (kickerNode.textContent !== kicker) {
      kickerNode.textContent = kicker;
      kickerNode.classList.remove("is-flash");
      void kickerNode.offsetWidth;
      kickerNode.classList.add("is-flash");
    }
    $("status-hint").innerHTML = hint;
    $("record-label").textContent = label;
    $("record-button").disabled = state.current === "transcribing";
    $("status-label").textContent =
      state.current === "recording" ? "Opnemen" :
      state.current === "transcribing" ? "Omzetten" :
      state.listening ? "Klaar" : "Gepauzeerd";
    $("nav-live").hidden = state.current !== "recording";
    $("timer").hidden = state.current !== "recording" && state.current !== "transcribing";
    if (state.current !== "recording") {
      state.level = 0;
      renderMeter();
    }
    $("copy-live").hidden = !(state.partial || (state.last && state.current === "idle"));
  }

  function findDevice(setting) {
    const raw = String(setting ?? "").trim();
    if (!raw || raw.toLowerCase() === "default") return null;
    const lower = raw.toLowerCase();
    const devices = state.devices || [];
    const exact = devices.find((entry) => {
      const id = String(entry.id ?? entry.name ?? "").toLowerCase();
      const name = String(entry.name ?? "").toLowerCase();
      return id === lower || name === lower || String(entry.index) === raw;
    });
    if (exact) return exact;
    return devices.find((entry) => {
      const id = String(entry.id ?? entry.name ?? "").toLowerCase();
      const name = String(entry.name ?? "").toLowerCase();
      return name.includes(lower) || id.includes(lower);
    }) || null;
  }

  function deviceOptionLabel(entry) {
    const label = entry.label || entry.name || entry.id || "Microfoon";
    return entry.default ? `${label} (Windows-standaard)` : label;
  }

  function renderDeviceOptions(configured) {
    const select = $("set-device");
    const hint = $("device-hint");
    const options = [{ value: "default", label: "Systeemstandaard (Windows)" }].concat(
      (state.devices || []).map((entry) => ({
        value: String(entry.id ?? entry.name),
        label: deviceOptionLabel(entry),
      }))
    );
    const raw = configured === null || configured === undefined ? "default" : String(configured);
    const match = raw === "default" ? null : findDevice(raw);
    const selected = match ? String(match.id) : raw;
    if (!options.some((entry) => entry.value === selected)) {
      options.push({ value: selected, label: `${raw} (niet gevonden)` });
    }
    fillSelect(select, options);
    select.value = selected;

    hint.classList.remove("warn");
    if (raw === "default") {
      hint.textContent = "De microfoon die Windows als standaard kiest. Kies hier een andere om alleen Dictate die te laten gebruiken.";
    } else if (match) {
      hint.textContent = `Gekozen: ${match.label}. De keuze wordt op naam bewaard, ook als Windows de volgorde wijzigt.`;
    } else {
      hint.textContent = "Deze microfoon is niet gevonden. Kies opnieuw of klik op Vernieuwen.";
      hint.classList.add("warn");
    }
  }

  function renderChips() {
    const s = state.settings || {};
    $("chip-model").textContent = s.model || "—";
    $("chip-language").textContent = s.language || "auto";
    const device = findDevice(s.input_device);
    $("device-chip").textContent = device ? device.label : (s.input_device ? String(s.input_device) : "Standaardmicrofoon");
    $("mode-chip").textContent = MODE_CHIPS[s.mode] || "Plakken";
  }

  function renderMeter() {
    const bars = $("meter").children;
    const factors = [0.35, 0.7, 1, 0.55, 0.85, 1, 0.45, 0.8, 1, 0.6, 0.4, 0.25];
    for (let i = 0; i < bars.length; i += 1) {
      const height = 3 + state.level * 13 * factors[i];
      bars[i].style.height = `${Math.max(3, Math.min(16, height)).toFixed(1)}px`;
    }
  }

  let clockTimer = null;
  function startClock() {
    if (clockTimer) return;
    // Only the timer chip counts here: the foot line belongs to the word
    // counter, and two writers on one element made the words invisible.
    clockTimer = setInterval(() => {
      if (state.current !== "recording" || !state.recordingStarted) return;
      const seconds = (Date.now() - state.recordingStarted) / 1000;
      $("timer").textContent = `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
    }, 200);
  }

  /* --------------------------------------------------------- transcript */
  function setTranscript(text, fresh) {
    const box = $("transcript");
    const value = String(text || "");
    box.classList.remove("is-fresh");
    if (!value.trim()) {
      box.innerHTML = '<span class="placeholder">Je woorden verschijnen hier terwijl je spreekt.</span>';
      $("live-count").textContent = "0 woorden";
      $("copy-live").hidden = true;
      return;
    }
    box.textContent = value;
    box.scrollTop = box.scrollHeight;
    const words = value.trim().split(/\s+/).filter(Boolean).length;
    $("live-count").textContent = `${words} ${words === 1 ? "woord" : "woorden"}`;
    $("copy-live").hidden = false;
    if (fresh) {
      void box.offsetWidth;
      box.classList.add("is-fresh");
    }
  }

  function renderTranscript() {
    if (state.current === "recording") return;
    setTranscript(state.last ? state.last.text : "");
    if (!state.last) setLiveNote("De definitieve tekst wordt na het loslaten geplakt.");
  }

  function setLiveNote(text) {
    $("live-note").textContent = text;
  }

  function renderStats() {
    const stats = state.stats || {};
    bumpValue($("stat-count"), stats.today_count || 0);
    bumpValue($("stat-words"), stats.today_words || 0);
    bumpValue($("stat-time"), humanSeconds(stats.today_seconds || 0));
  }

  /* ------------------------------------------------------------- recent */
  function renderRecent() {
    const list = $("recent-list");
    const items = (state.history || []).slice(0, 3);
    $("recent-empty").hidden = items.length > 0;
    list.innerHTML = "";
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "recent-item";
      row.title = "Klik om te kopiëren";
      const text = document.createElement("p");
      text.textContent = item.text;
      const time = document.createElement("span");
      time.className = "recent-time";
      time.textContent = timeLabel(item.ts, true);
      const copy = document.createElement("button");
      copy.className = "icon-button";
      copy.title = "Kopiëren";
      copy.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M15 6.5V5.5A2.5 2.5 0 0 0 12.5 3h-7A2.5 2.5 0 0 0 3 5.5v7A2.5 2.5 0 0 0 5.5 15h1"/></svg>';
      copy.addEventListener("click", (event) => {
        event.stopPropagation();
        copyText(item.text);
      });
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.addEventListener("click", () => copyText(item.text));
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          copyText(item.text);
        }
      });
      row.append(text, time, copy);
      list.appendChild(row);
    });
  }

  /* ------------------------------------------------------------ history */
  function timeLabel(ts, clockOnly) {
    const date = new Date(Number(ts || 0) * 1000);
    const time = date.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
    if (clockOnly) return time;
    const now = new Date();
    if (date.toDateString() === now.toDateString()) return time;
    const yesterday = new Date(now.getTime() - 86400000);
    if (date.toDateString() === yesterday.toDateString()) return `gisteren ${time}`;
    return date.toLocaleDateString("nl-NL", { day: "numeric", month: "short" }) + " " + time;
  }

  function dayKey(ts) {
    const date = new Date(Number(ts || 0) * 1000);
    return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
  }

  function dayLabel(ts) {
    const date = new Date(Number(ts || 0) * 1000);
    const now = new Date();
    if (date.toDateString() === now.toDateString()) return "Vandaag";
    const yesterday = new Date(now.getTime() - 86400000);
    if (date.toDateString() === yesterday.toDateString()) return "Gisteren";
    return date.toLocaleDateString("nl-NL", { weekday: "long", day: "numeric", month: "long" });
  }

  function renderHistory() {
    const query = ($("history-search").value || "").trim().toLowerCase();
    const all = state.history || [];
    const items = all.filter((item) => !query || String(item.text || "").toLowerCase().includes(query));
    const list = $("history-list");
    list.innerHTML = "";

    const words = all.reduce((sum, item) => sum + String(item.text || "").trim().split(/\s+/).filter(Boolean).length, 0);
    const seconds = all.reduce((sum, item) => sum + Number(item.duration || 0), 0);
    $("history-summary").textContent = all.length
      ? `${all.length} ${all.length === 1 ? "dictaat" : "dictaten"} bewaard · ${words} woorden · ${humanSeconds(seconds)} spreektijd`
      : "Alles wat je dicteerde, lokaal bewaard.";

    $("history-count").textContent = String(all.length);
    $("history-count").hidden = all.length === 0;
    $("history-empty").hidden = items.length > 0;
    $("history-empty-text").innerHTML = all.length
      ? "Geen resultaten voor deze zoekopdracht."
      : `Nog geen dictaten. Houd <kbd>${state.hotkeyLabel}</kbd> ingedrukt om te beginnen.`;

    let currentDay = null;
    items.forEach((item) => {
      const key = dayKey(item.ts);
      if (key !== currentDay) {
        currentDay = key;
        const head = document.createElement("div");
        head.className = "group-head";
        head.textContent = dayLabel(item.ts);
        list.appendChild(head);
      }
      list.appendChild(historyItem(item));
    });
  }

  function historyItem(item) {
    if (state.editingId === item.id) return historyEditor(item);
    const node = document.createElement("article");
    node.className = "history-item";
    node.dataset.id = item.id;

    const textWrap = document.createElement("div");
    textWrap.className = "history-text";
    const paragraph = document.createElement("p");
    paragraph.textContent = item.text;
    const meta = document.createElement("div");
    meta.className = "history-meta";
    const wordCount = String(item.text || "").trim().split(/\s+/).filter(Boolean).length;
    const bits = [timeLabel(item.ts), `${wordCount} ${wordCount === 1 ? "woord" : "woorden"}`];
    if (item.duration) bits.push(`${Number(item.duration).toFixed(1)} s`);
    if (item.mode) bits.push(MODE_LABELS[item.mode] || item.mode);
    if (item.model) bits.push(item.model);
    if (item.edited) bits.push("bewerkt");
    bits.forEach((bit) => {
      const span = document.createElement("span");
      span.textContent = bit;
      meta.appendChild(span);
    });
    textWrap.append(paragraph, meta);

    const actions = document.createElement("div");
    actions.className = "history-actions";
    const edit = document.createElement("button");
    edit.className = "icon-button";
    edit.title = "Bewerken";
    edit.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4l10.5-10.5a2.1 2.1 0 0 0-3-3L5 17v3Z"/><path d="m13.5 6.5 4 4"/></svg>';
    edit.addEventListener("click", () => {
      state.editingId = item.id;
      renderHistory();
    });
    const copy = document.createElement("button");
    copy.className = "icon-button";
    copy.title = "Kopiëren";
    copy.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M15 6.5V5.5A2.5 2.5 0 0 0 12.5 3h-7A2.5 2.5 0 0 0 3 5.5v7A2.5 2.5 0 0 0 5.5 15h1"/></svg>';
    copy.addEventListener("click", async () => {
      const result = await copyText(item.text);
      if (result) {
        copy.classList.add("copied");
        setTimeout(() => copy.classList.remove("copied"), 1200);
      }
    });
    const remove = document.createElement("button");
    remove.className = "icon-button danger";
    remove.title = "Verwijderen";
    remove.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M4 7h16M9.5 7V5.2A1.7 1.7 0 0 1 11.2 3.5h1.6A1.7 1.7 0 0 1 14.5 5.2V7M6.5 7l.8 12.2A1.8 1.8 0 0 0 9.1 21h5.8a1.8 1.8 0 0 0 1.8-1.8L17.5 7"/></svg>';
    remove.addEventListener("click", () => deleteItem(item.id));
    actions.append(edit, copy, remove);

    node.append(textWrap, actions);
    return node;
  }

  function historyEditor(item) {
    const node = document.createElement("article");
    node.className = "history-item is-editing";
    node.dataset.id = item.id;

    const wrap = document.createElement("div");
    wrap.className = "history-edit";
    const area = document.createElement("textarea");
    area.className = "edit-area";
    area.rows = 3;
    area.spellcheck = false;
    area.value = item.text || "";
    area.setAttribute("aria-label", "Dictaat bewerken");

    const bar = document.createElement("div");
    bar.className = "edit-bar";
    const hint = document.createElement("span");
    hint.className = "edit-hint";
    hint.textContent = "De app leert van je correctie.";
    const cancel = document.createElement("button");
    cancel.className = "ghost-button mini";
    cancel.textContent = "Annuleren";
    const save = document.createElement("button");
    save.className = "primary-button mini";
    save.textContent = "Opslaan";
    bar.append(hint, cancel, save);
    wrap.append(area, bar);
    node.append(wrap);

    const close = () => {
      state.editingId = null;
      renderHistory();
    };
    const commit = async () => {
      const text = area.value.trim();
      if (!text) {
        hint.textContent = "Tekst mag niet leeg zijn.";
        hint.classList.add("error");
        return;
      }
      save.disabled = true;
      cancel.disabled = true;
      hint.classList.remove("error");
      hint.textContent = "Bezig…";
      try {
        const res = await api.correct_history(item.id, text);
        if (!res || !res.ok) {
          hint.textContent = (res && res.error) || "Opslaan mislukt.";
          hint.classList.add("error");
          save.disabled = false;
          cancel.disabled = false;
          return;
        }
        state.history = res.history || state.history;
        state.stats = res.stats || state.stats;
        state.learning = res.learning || state.learning;
        state.editingId = null;
        renderHistory();
        renderRecent();
        renderStats();
        renderLearning();
        const corrections = Number(res.learned && res.learned.corrections) || 0;
        const words = Number(res.learned && res.learned.words) || 0;
        if (corrections || words) {
          const bits = [];
          if (corrections) bits.push(`${corrections} ${corrections === 1 ? "correctie" : "correcties"}`);
          if (words) bits.push(`${words} ${words === 1 ? "woord" : "woorden"}`);
          toast(`Bijgewerkt · ${bits.join(" en ")} geleerd`, "ok");
        } else {
          toast("Tekst bijgewerkt", "ok");
        }
      } catch (err) {
        hint.textContent = "Opslaan mislukt: " + (err && err.message ? err.message : err);
        hint.classList.add("error");
        save.disabled = false;
        cancel.disabled = false;
      }
    };

    save.addEventListener("click", commit);
    cancel.addEventListener("click", close);
    area.addEventListener("input", () => {
      save.disabled = !area.value.trim();
    });
    area.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        commit();
      }
    });
    // Keep the scroll position: focusing normally makes Chromium scroll the
    // focused field into view and, with a tall settings page behind it, can
    // scroll the whole document instead of just the content column.
    setTimeout(() => area.focus({ preventScroll: true }), 0);
    return node;
  }

  async function deleteItem(id) {
    try {
      const res = await api.delete_history(id);
      if (res && res.history) {
        state.history = res.history;
        state.stats = res.stats || computeStats(res.history);
        renderHistory();
        renderRecent();
        renderStats();
        toast("Dictaat verwijderd", "ok");
      }
    } catch (err) {
      toast("Verwijderen mislukt", "error");
    }
  }

  async function copyText(text) {
    try {
      const res = await api.copy(text);
      if (res && res.ok === false) {
        toast("Kopiëren mislukt", "error");
        return false;
      }
      toast("Gekopieerd", "ok");
      return true;
    } catch (err) {
      toast("Kopiëren mislukt", "error");
      return false;
    }
  }

  /* ------------------------------------------------------------ learning */
  function renderLearning() {
    const data = state.learning || {};
    const corrections = Array.isArray(data.corrections) ? data.corrections : [];
    const vocabulary = Array.isArray(data.vocabulary) ? data.vocabulary : [];
    const total = corrections.length + vocabulary.length;
    const enabled = state.settings.learn_from_corrections !== false;

    $("learn-count").textContent = total
      ? `${total} geleerd${enabled ? "" : " · leren uit"}`
      : enabled ? "" : "leren uit";
    $("learn-empty").hidden = total > 0;
    $("learn-empty").textContent = enabled
      ? "Nog niets geleerd. Bewerk een dictaat in Geschiedenis om te beginnen."
      : "Leren staat uit. Bestaande kennis hieronder blijft actief tot je die vergeet.";

    const correctionsBlock = $("learn-corrections-block");
    const vocabularyBlock = $("learn-vocabulary-block");
    correctionsBlock.hidden = corrections.length === 0;
    // The block holds the add field, so it stays reachable with an empty list.
    vocabularyBlock.hidden = false;
    $("learn-clear").hidden = total === 0;

    const list = $("learn-corrections");
    list.innerHTML = "";
    corrections.forEach((row) => {
      const node = document.createElement("div");
      node.className = "learn-row";
      const pair = document.createElement("div");
      pair.className = "learn-pair";
      const heard = document.createElement("span");
      heard.className = "learn-heard";
      heard.textContent = row.heard;
      const arrow = document.createElement("span");
      arrow.className = "learn-arrow";
      arrow.textContent = "→";
      const fixed = document.createElement("span");
      fixed.className = "learn-fixed";
      fixed.textContent = row.fixed;
      pair.append(heard, arrow, fixed);
      const times = document.createElement("span");
      times.className = "learn-times";
      times.textContent = Number(row.count) > 1 ? `${row.count}×` : "";
      const forget = document.createElement("button");
      forget.className = "learn-forget";
      forget.title = "Deze correctie vergeten";
      forget.textContent = "×";
      forget.addEventListener("click", () => forgetLearned("correction", row.heard));
      node.append(pair, times, forget);
      list.appendChild(node);
    });

    const chips = $("learn-vocabulary");
    chips.innerHTML = "";
    vocabulary.forEach((row) => {
      const chip = document.createElement("span");
      chip.className = "learn-chip";
      const label = document.createElement("span");
      label.textContent = row.word;
      const forget = document.createElement("button");
      forget.title = "Dit woord vergeten";
      forget.textContent = "×";
      forget.addEventListener("click", () => forgetLearned("word", row.word));
      chip.append(label, forget);
      chips.appendChild(chip);
    });
  }

  async function addVocabulary() {
    const input = $("learn-word");
    const word = input.value.trim();
    if (!word) {
      input.focus();
      return;
    }
    try {
      const res = await api.add_vocabulary(word);
      if (!res || res.ok === false) {
        toast((res && res.error) || "Toevoegen mislukt", "error");
        return;
      }
      state.learning = res.learning || state.learning;
      input.value = "";
      renderLearning();
      input.focus();
      toast(`"${word}" toegevoegd`, "ok");
    } catch (err) {
      toast("Toevoegen mislukt", "error");
    }
  }

  async function forgetLearned(kind, value) {
    try {
      const res = await api.forget_learning(kind, value);
      if (!res || res.ok === false) {
        toast((res && res.error) || "Vergeten mislukt", "error");
        return;
      }
      state.learning = res.learning || state.learning;
      renderLearning();
      toast("Vergeten", "ok");
    } catch (err) {
      toast("Vergeten mislukt", "error");
    }
  }

  let learnClearTimer = null;
  function resetLearnClear() {
    const button = $("learn-clear");
    button.classList.remove("armed");
    button.textContent = button.dataset.original || "Alles vergeten";
  }

  async function clearLearning() {
    const button = $("learn-clear");
    if (!button.classList.contains("armed")) {
      button.classList.add("armed");
      button.dataset.original = button.textContent;
      button.textContent = "Klik nogmaals om te vergeten";
      clearTimeout(learnClearTimer);
      learnClearTimer = setTimeout(resetLearnClear, 3400);
      return;
    }
    clearTimeout(learnClearTimer);
    resetLearnClear();
    try {
      const res = await api.clear_learning();
      if (!res || res.ok === false) {
        toast((res && res.error) || "Vergeten mislukt", "error");
        return;
      }
      state.learning = res.learning || { corrections: [], vocabulary: [], total: 0 };
      renderLearning();
      toast("Alle geleerde woorden vergeten", "ok");
    } catch (err) {
      toast("Vergeten mislukt", "error");
    }
  }

  /* ----------------------------------------------------------- settings */
  let modelList = [];

  function buildModelList() {
    const names = (state.models && state.models.length)
      ? state.models.slice()
      : MODEL_ORDER.concat(["tiny", "base", "small", "medium"].map((name) => `${name}.en`));
    modelList = names;
  }

  function fillSelect(select, entries) {
    select.innerHTML = "";
    entries.forEach((entry) => {
      if (entry.group) {
        const group = document.createElement("optgroup");
        group.label = entry.group;
        (entry.options || []).forEach((option) => group.appendChild(newOption(option)));
        select.appendChild(group);
      } else {
        select.appendChild(newOption(entry));
      }
    });
  }

  function newOption({ value, label }) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }

  function modelEntries(extra) {
    const multilingual = modelList.filter((name) => !name.endsWith(".en"));
    const english = modelList.filter((name) => name.endsWith(".en"));
    const entries = [];
    if (extra) entries.push(extra);
    if (multilingual.length) entries.push({ group: "Meertalig", options: multilingual.map((name) => ({ value: name, label: name })) });
    if (english.length) entries.push({ group: "Alleen Engels", options: english.map((name) => ({ value: name, label: name })) });
    return entries;
  }

  function renderSettings() {
    buildModelList();
    const s = state.settings || {};

    fillSelect($("set-language"), Object.keys(LANGUAGE_LABELS).map((code) => ({
      value: code,
      label: LANGUAGE_LABELS[code],
    })));
    $("set-language").value = s.language || "auto";

    fillSelect($("set-model"), modelEntries());
    $("set-model").value = s.model || "";

    fillSelect($("set-preview-model"), modelEntries({ value: "", label: "Zelfde als hoofdmodel" }));
    $("set-preview-model").value = s.preview_model || "";

    renderDeviceOptions(s.input_device);

    $("set-threads").value = s.cpu_threads ?? 4;
    $("set-beam").value = s.beam_size ?? 1;
    $("set-prompt").value = s.initial_prompt || "";
    $("set-hotkey").value = s.hotkey || "<f9>";
    $("set-mode").value = s.mode || "paste";
    $("set-backend").value = s.online_backend || (state.cloud && state.cloud.backend) || "local";

    $("set-hold").checked = s.hold_to_talk !== false;
    $("set-overlay").checked = s.overlay !== false;
    $("set-stream").checked = s.stream_preview !== false;
    $("set-beeps").checked = s.beeps !== false;
    $("set-space").checked = s.post_space !== false;
    $("set-clipboard").checked = s.restore_clipboard !== false;
    $("set-learn").checked = s.learn_from_corrections !== false;
    $("set-autostart").checked = Boolean(state.autostart);
    $("set-crash-log").checked = s.crash_logging !== false;

    $("settings-error").hidden = true;
    $("save-state").textContent = "";
    $("save-state").classList.remove("error");
    $("hotkey-hint").textContent = "Klik op het veld en druk de combinatie die je wilt gebruiken.";
    $("hotkey-hint").classList.remove("warn");
    $("config-path").textContent = state.configPath ? `Instellingen: ${state.configPath}` : "";

    updateModelHints();
    renderLearning();
    renderCloud();
    renderAbout();
  }

  // Used when the bridge predates the picker; the bridge's own list wins.
  const GEMINI_FALLBACK = [
    { value: "gemini-3.5-flash-lite", label: "3.5 Flash-Lite", note: "snelst, ongeveer 1 seconde per dictaat" },
    { value: "gemini-3.6-flash", label: "3.6 Flash", note: "even nauwkeurig, maar ongeveer 3 seconden en snel aan de gratis limiet" },
  ];

  function providerMeta(provider) {
    const cloud = state.cloud || {};
    return ((cloud.providers || {})[provider]) || {};
  }

  function keyPresent(backend) {
    const cloud = state.cloud || {};
    if (cloud.keys && Object.prototype.hasOwnProperty.call(cloud.keys, backend)) {
      return Boolean(cloud.keys[backend]);
    }
    return Boolean(cloud.has_key) && cloud.backend === backend;
  }

  // A key belongs to the provider it was made for; while the app is still on
  // "local" the field keeps pointing at Groq, as it always did.
  function currentProvider() {
    const backend = $("set-backend").value || "local";
    return backend === "local" ? "groq" : backend;
  }

  function renderCloud() {
    const backend = $("set-backend").value || "local";
    const provider = currentProvider();
    const meta = providerMeta(provider);
    const online = backend !== "local";
    const name = meta.name || "online";
    const hasKey = keyPresent(provider);
    const note = $("cloud-note");
    const status = $("cloud-key-status");
    const hint = $("backend-hint");

    $("cloud-key-label").textContent = `${name} API-sleutel`;
    renderGeminiModels(backend, meta);
    $("set-cloud-key").placeholder = meta.key_hint || "API-sleutel";
    $("cloud-limits").textContent = meta.free_limits || "Zie de limieten van je provider.";
    $("cloud-keys-url").textContent = String(meta.keys_url || "").replace(/^https?:\/\//, "");
    $("cloud-privacy-text").textContent = online
      ? `Alleen de audio van je laatste dictaat gaat naar ${name}; het live voorbeeld blijft lokaal. Zonder sleutel of bij een fout valt de app terug op het lokale model.`
      : "Er gaat niets online: audio en tekst blijven op deze pc. Kies een online model als namen en jargon er beter uit moeten komen.";

    if (online) {
      note.textContent = hasKey ? `online · ${meta.model || name}` : "online · sleutel nodig";
      hint.textContent = hasKey
        ? `De eindtekst komt van ${name}; het live voorbeeld blijft lokaal. Bij een fout valt de app terug op lokaal.`
        : `Sla eerst een gratis ${name}-sleutel op; daarna kun je online kiezen.`;
      hint.classList.toggle("warn", !hasKey);
    } else {
      note.textContent = "lokaal";
      hint.textContent = "Lokaal blijft privé. Online is nauwkeuriger voor namen en jargon.";
      hint.classList.remove("warn");
    }

    status.textContent = hasKey
      ? "Sleutel opgeslagen (versleuteld met Windows DPAPI)."
      : "Geen sleutel opgeslagen.";
    status.classList.toggle("warn", online && !hasKey);
    $("clear-cloud-key").hidden = !hasKey;
  }

  function renderGeminiModels(backend, meta) {
    const field = $("gemini-model-field");
    const select = $("set-gemini-model");
    field.hidden = backend !== "gemini";
    if (field.hidden) return;

    const list = (state.cloud && state.cloud.gemini_models && state.cloud.gemini_models.length)
      ? state.cloud.gemini_models
      : GEMINI_FALLBACK;
    const signature = list.map((entry) => entry.value).join(",");
    if (select.dataset.signature !== signature) {
      fillSelect(select, list.map((entry) => ({ value: entry.value, label: entry.label })));
      select.dataset.signature = signature;
    }
    const saved = String(
      (state.settings && state.settings.gemini_model)
      || (state.cloud && state.cloud.gemini_model)
      || ""
    );
    // Only follow the stored value when it actually moved, so re-rendering on a
    // change event cannot undo the choice the user just made in the list.
    if (!select.value || select.dataset.saved !== saved) {
      select.value = saved || list[0].value;
      select.dataset.saved = saved;
    }
    const entry = list.find((item) => item.value === select.value) || {};
    $("gemini-model-hint").textContent = entry.note
      ? `${entry.note}. ${meta.model && meta.model === select.value ? "Nu actief." : ""}`.trim()
      : "Snelheid tegen nauwkeurigheid.";
  }

  function selectedModel() { return $("set-model").value; }

  function updateModelHints() {
    const language = $("set-language").value;
    const model = selectedModel();
    const base = model.replace(/\.en$/, "");
    const hint = $("model-hint");
    const note = $("model-note");
    const englishOnly = model.endsWith(".en");
    let text = MODEL_NOTES[base] || "Bepaalt de tekst die geplakt wordt.";
    if (englishOnly) text = "Alleen Engels. " + text;
    hint.textContent = text;
    hint.classList.remove("warn");
    note.textContent = model ? `gekozen: ${model}` : "";
    const preview = $("set-preview-model").value;
    if (language !== "en" && language !== "auto") {
      const off = [model, preview].filter((name) => name && name.endsWith(".en"));
      if (off.length) {
        hint.textContent = `'${off[0]}' begrijpt alleen Engels. Kies een meertalig model.`;
        hint.classList.add("warn");
      }
    }
  }

  function collectSettings() {
    return {
      language: $("set-language").value,
      model: $("set-model").value,
      preview_model: $("set-preview-model").value,
      input_device: $("set-device").value === "default" ? null : $("set-device").value,
      cpu_threads: $("set-threads").value,
      beam_size: $("set-beam").value,
      initial_prompt: $("set-prompt").value,
      hotkey: $("set-hotkey").value,
      mode: $("set-mode").value,
      online_backend: $("set-backend").value,
      gemini_model: $("set-gemini-model").value
        || (state.settings && state.settings.gemini_model)
        || "",
      hold_to_talk: $("set-hold").checked,
      overlay: $("set-overlay").checked,
      stream_preview: $("set-stream").checked,
      beeps: $("set-beeps").checked,
      post_space: $("set-space").checked,
      restore_clipboard: $("set-clipboard").checked,
      learn_from_corrections: $("set-learn").checked,
      crash_logging: $("set-crash-log").checked,
    };
  }

  async function saveSettings() {
    if (state.capture) {
      stopCapture();
      $("set-hotkey").value = state.settings.hotkey || "<f9>";
    }
    const button = $("save-settings");
    const errorBox = $("settings-error");
    const saveState = $("save-state");
    button.disabled = true;
    errorBox.hidden = true;
    saveState.textContent = "Bezig…";
    saveState.classList.remove("error");
    try {
      const check = await api.validate_hotkey(collectSettings().hotkey);
      if (!check || check.ok === false) {
        errorBox.textContent = "Sneltoets: " + ((check && check.error) || "ongeldige combinatie");
        errorBox.hidden = false;
        saveState.textContent = "Niet opgeslagen";
        saveState.classList.add("error");
        return;
      }
      const result = await api.save_settings(collectSettings());
      if (!result || !result.ok) {
        errorBox.textContent = (result && result.error) || "Opslaan mislukt.";
        errorBox.hidden = false;
        saveState.textContent = "Niet opgeslagen";
        saveState.classList.add("error");
        return;
      }
      state.settings = result.settings || state.settings;
      state.cloud = result.cloud || state.cloud;
      const wantAutostart = $("set-autostart").checked;
      if (wantAutostart !== state.autostart) {
        const auto = await api.set_autostart(wantAutostart);
        if (auto && auto.ok) {
          state.autostart = Boolean(auto.enabled);
        } else {
          errorBox.textContent = (auto && auto.error) || "Opstartinstelling kon niet worden aangepast.";
          errorBox.hidden = false;
        }
      }
      state.hotkeyLabel = prettyHotkey(state.settings.hotkey);
      renderBrand();
      renderStatus();
      renderChips();
      renderCloud();
      renderBrand();
      renderAbout();
      $("set-hotkey").value = state.settings.hotkey || $("set-hotkey").value;
      updateModelHints();
      renderLearning();
      saveState.classList.remove("error");
      saveState.textContent = "Opgeslagen";
      toast("Instellingen opgeslagen", "ok");
      setTimeout(() => {
        if (!saveState.classList.contains("error")) saveState.textContent = "";
      }, 2800);
    } catch (err) {
      errorBox.textContent = String(err);
      errorBox.hidden = false;
      saveState.textContent = "";
    } finally {
      button.disabled = false;
    }
  }

  async function saveCloudKey() {
    const provider = currentProvider();
    const name = providerMeta(provider).name || "online";
    const input = $("set-cloud-key");
    const key = input.value.trim();
    if (!key) {
      toast(`Plak eerst een ${name}-sleutel`, "error");
      input.focus();
      return;
    }
    const button = $("save-cloud-key");
    button.disabled = true;
    try {
      const result = await api.save_cloud_key(key, provider);
      if (!result || !result.ok) {
        toast((result && result.error) || "Sleutel opslaan mislukt", "error");
        return;
      }
      input.value = "";
      state.cloud = result.cloud || state.cloud;
      renderCloud();
      renderBrand();
      renderAbout();
      toast(`${name}-sleutel opgeslagen`, "ok");
    } catch (err) {
      toast("Sleutel opslaan mislukt: " + (err && err.message ? err.message : err), "error");
    } finally {
      button.disabled = false;
    }
  }

  async function clearCloudKey() {
    const provider = currentProvider();
    const name = providerMeta(provider).name || "online";
    try {
      const result = await api.clear_cloud_key(provider);
      if (!result || result.ok === false) {
        toast((result && result.error) || "Sleutel verwijderen mislukt", "error");
        return;
      }
      state.cloud = result.cloud || state.cloud;
      renderCloud();
      renderBrand();
      renderAbout();
      toast(`${name}-sleutel verwijderd`, "ok");
    } catch (err) {
      toast("Sleutel verwijderen mislukt", "error");
    }
  }

  function renderAbout() {
    const s = state.settings || {};
    $("about-version").textContent = state.version;
    $("update-current").textContent = state.version;
    const backend = (state.cloud && state.cloud.backend) || s.online_backend || "local";
    const meta = providerMeta(backend);
    const name = meta.name || "online";
    const onlineEngine = backend !== "local" && keyPresent(backend);
    const engineModel = onlineEngine ? (meta.model || name) : (s.model || "—");
    $("about-engine").textContent = `${onlineEngine ? `${name} (online)` : "Lokaal (Whisper)"} · ${engineModel}`;
    let pitch = "Een kleine, snelle dicteerhulp met Whisper op je eigen computer. Alles blijft lokaal; je stem verlaat de pc niet.";
    if (onlineEngine) {
      pitch = `Een kleine, snelle dicteerhulp. De eindtekst komt nu van ${name}; alleen de audio van je laatste dictaat gaat online. Het live voorbeeld blijft lokaal.`;
    } else if (backend !== "local") {
      pitch = `Een kleine, snelle dicteerhulp met Whisper op je eigen computer. Online is gekozen, maar er is nog geen ${name}-sleutel; daarom blijft alles lokaal.`;
    }
    $("about-pitch").textContent = pitch;
    $("about-model").textContent = s.model || "—";
    $("about-preview").textContent = s.preview_model || (s.model ? "zelfde" : "—");
    $("about-language").textContent = s.language || "auto";
    $("about-hotkey").textContent = state.hotkeyLabel;
    $("about-models").textContent = state.modelDir || "models";
    $("about-config").textContent = state.configPath || "config.json";
    $("about-crash").textContent = state.crashLogPath || "crash.log";
  }

  /* ------------------------------------------------------------ hotkey */
  function prettyHotkey(combo) {
    const raw = String(combo || "").trim();
    if (!raw) return "F9";
    return raw
      .replace(/^<|>$/g, "")
      .split("+")
      .filter(Boolean)
      .map((part) => {
        const key = part.trim().replace(/^<|>$/g, "").toLowerCase();
        const names = {
          ctrl: "Ctrl", alt: "Alt", shift: "Shift", cmd: "Win", win: "Win",
          space: "Space", enter: "Enter", tab: "Tab", backspace: "Backspace",
          delete: "Del", insert: "Ins", home: "Home", end: "End",
          page_up: "PgUp", page_down: "PgDn", up: "↑", down: "↓", left: "←", right: "→",
          esc: "Esc", escape: "Esc",
        };
        if (names[key]) return names[key];
        if (/^f\d{1,2}$/.test(key)) return key.toUpperCase();
        return key.toUpperCase();
      })
      .join(" + ");
  }

  function startCapture() {
    state.capture = true;
    const input = $("set-hotkey");
    input.classList.add("is-capturing");
    input.value = "Druk op een combinatie…";
    $("hotkey-field").classList.add("capturing");
    $("hotkey-hint").textContent = "Houd de toetsen samen ingedrukt. Esc annuleert.";
    $("hotkey-hint").classList.remove("warn");
    input.focus();
  }

  function stopCapture() {
    state.capture = false;
    $("set-hotkey").classList.remove("is-capturing");
    $("hotkey-field").classList.remove("capturing");
  }

  function comboFromEvent(event) {
    const modifiers = [];
    if (event.ctrlKey) modifiers.push("<ctrl>");
    if (event.altKey) modifiers.push("<alt>");
    if (event.shiftKey) modifiers.push("<shift>");
    if (event.metaKey) modifiers.push("<cmd>");
    const code = event.code || "";
    let key = null;
    if (/^Key[A-Z]$/.test(code)) key = code.slice(3).toLowerCase();
    else if (/^Digit[0-9]$/.test(code)) key = code.slice(5);
    else if (/^F([1-9]|1[0-9]|2[0-4])$/.test(code)) key = `<${code.toLowerCase()}>`;
    else {
      const named = {
        Space: "<space>", Enter: "<enter>", Tab: "<tab>", Backspace: "<backspace>",
        Delete: "<delete>", Insert: "<insert>", Home: "<home>", End: "<end>",
        PageUp: "<page_up>", PageDown: "<page_down>",
        ArrowUp: "<up>", ArrowDown: "<down>", ArrowLeft: "<left>", ArrowRight: "<right>",
      };
      key = named[code] || null;
    }
    if (!key) return null;
    if (!modifiers.length && !/^f\d{1,2}$/.test(key.replace(/[<>]/g, ""))) return null;
    return (modifiers.length ? modifiers.join("+") + "+" : "") + key;
  }

  async function handleCaptureKey(event) {
    if (!state.capture) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.key === "Escape") {
      stopCapture();
      renderSettings();
      return;
    }
    const combo = comboFromEvent(event);
    if (!combo) {
      const modifiers = ["Control", "Alt", "Shift", "Meta"];
      if (!modifiers.includes(event.key)) {
        $("hotkey-hint").textContent = "Gebruik een functietoets of een combinatie met Ctrl, Alt of Shift.";
        $("hotkey-hint").classList.add("warn");
      } else {
        $("set-hotkey").value = prettyHotkey(
          [
            event.ctrlKey ? "<ctrl>" : "",
            event.altKey ? "<alt>" : "",
            event.shiftKey ? "<shift>" : "",
            event.metaKey ? "<cmd>" : "",
          ].filter(Boolean).join("+") + "+<…>"
        );
      }
      return;
    }
    stopCapture();
    const check = await api.validate_hotkey(combo);
    if (check && check.ok === false) {
      $("set-hotkey").value = combo;
      $("hotkey-hint").textContent = (check.error || "Deze combinatie werkt niet.") + " Kies een andere.";
      $("hotkey-hint").classList.add("warn");
      return;
    }
    $("set-hotkey").value = combo;
    $("hotkey-hint").textContent = `Wordt gebruikt als ${prettyHotkey(combo)}. Klik Opnemen om te wijzigen.`;
    $("hotkey-hint").classList.remove("warn");
  }

  /* ------------------------------------------------------ microphone test */
  async function refreshDevices() {
    const button = $("refresh-devices");
    if (!button || button.disabled) return;
    const current = $("set-device").value;
    button.disabled = true;
    try {
      const res = await api.list_devices();
      if (!res || !Array.isArray(res.devices)) {
        throw new Error((res && res.error) || "geen apparaten ontvangen");
      }
      state.devices = res.devices;
      renderDeviceOptions(current === "default" ? "default" : current);
      renderChips();
      renderMicNotice();
      toast(`${state.devices.length} microfoon${state.devices.length === 1 ? "" : "s"} gevonden`, "ok");
    } catch (err) {
      toast("Microfoons vernieuwen mislukt: " + (err && err.message ? err.message : err), "error");
    } finally {
      button.disabled = false;
    }
  }

  async function testMicrophone() {
    const button = $("mic-test");
    const track = $("mic-track");
    const fill = $("mic-level");
    const result = $("mic-result");
    const restore = state.current;
    if (restore === "recording" || restore === "transcribing") {
      result.textContent = "Stop eerst de opname.";
      result.className = "mic-result warn";
      return;
    }
    button.disabled = true;
    track.classList.add("testing");
    fill.style.width = "100%";
    result.className = "mic-result";
    result.textContent = "Luisteren… spreek even in de microfoon.";
    try {
      const device = $("set-device").value;
      const res = await api.test_microphone(2, device);
      track.classList.remove("testing");
      if (!res || res.ok === false) {
        fill.style.width = "0%";
        result.textContent = (res && res.error) || "Microfoon niet bereikbaar.";
        result.className = "mic-result bad";
        return;
      }
      const percent = Math.max(3, Math.min(100, ((Number(res.dbfs) + 60) / 60) * 100));
      fill.style.width = `${percent.toFixed(0)}%`;
      const where = res.device ? ` via ${res.device}` : "";
      if (res.verdict === "goed") {
        result.textContent = `Goed signaal (${res.dbfs} dBFS)${where}.`;
        result.className = "mic-result good";
      } else if (res.verdict === "zacht") {
        result.textContent = `Aan de zachte kant (${res.dbfs} dBFS)${where}. Ga wat dichter bij de microfoon.`;
        result.className = "mic-result warn";
      } else {
        result.textContent = `Bijna stil (${res.dbfs} dBFS)${where}. Controleer de microfoon en het Windows-volume.`;
        result.className = "mic-result bad";
      }
    } catch (err) {
      track.classList.remove("testing");
      fill.style.width = "0%";
      result.textContent = "Test mislukt: " + (err && err.message ? err.message : err);
      result.className = "mic-result bad";
    } finally {
      button.disabled = false;
    }
  }

  /* ---------------------------------------------------------------- misc */
  // A stat that changed scales up once; an unchanged one is left alone, so a
  // re-render never makes the row flicker.
  function bumpValue(node, value) {
    if (!node) return;
    const text = String(value);
    if (node.textContent === text) return;
    node.textContent = text;
    node.classList.remove("is-bump");
    void node.offsetWidth;
    node.classList.add("is-bump");
  }

  function toast(message, kind) {
    if (!message) return;
    const node = document.createElement("div");
    node.className = "toast" + (kind === "error" ? " error" : "");
    node.textContent = message;
    $("toasts").appendChild(node);
    setTimeout(() => {
      node.classList.add("is-out");
      setTimeout(() => node.remove(), 240);
    }, 2400);
    while ($("toasts").children.length > 4) $("toasts").firstElementChild.remove();
  }

  function humanSeconds(value) {
    const total = Math.max(0, Math.round(Number(value) || 0));
    if (total < 60) return `${total} s`;
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    if (minutes < 60) return seconds ? `${minutes} min ${seconds} s` : `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    return `${hours} uur ${minutes % 60} min`;
  }

  function computeStats(items) {
    const list = items || [];
    const now = new Date();
    const todayKey = `${now.getFullYear()}-${now.getMonth()}-${now.getDate()}`;
    const stats = { today_count: 0, today_words: 0, today_seconds: 0, total_count: list.length, total_words: 0, total_seconds: 0 };
    list.forEach((item) => {
      const words = String(item.text || "").trim().split(/\s+/).filter(Boolean).length;
      const seconds = Number(item.duration || 0);
      stats.total_words += words;
      stats.total_seconds += seconds;
      const date = new Date(Number(item.ts || 0) * 1000);
      if (`${date.getFullYear()}-${date.getMonth()}-${date.getDate()}` === todayKey) {
        stats.today_count += 1;
        stats.today_words += words;
        stats.today_seconds += seconds;
      }
    });
    return stats;
  }

  function showView(name) {
    const known = ["dictate", "history", "settings", "about"];
    const view = known.includes(name) ? name : "dictate";
    document.querySelectorAll(".view").forEach((node) => {
      node.classList.toggle("is-active", node.dataset.view === view);
    });
    document.querySelectorAll(".nav-item").forEach((node) => {
      node.classList.toggle("is-active", node.dataset.view === view);
    });
    const main = document.querySelector(".main");
    if (main) main.scrollTop = 0;
    if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
    if (view === "history") renderHistory();
    if (view === "settings") {
      updateModelHints();
      renderLearning();
    }
  }

  /* --------------------------------------------------------------- bind */
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", () => showView(item.dataset.view));
  });
  $("open-settings").addEventListener("click", () => showView("settings"));
  $("open-history").addEventListener("click", () => showView("history"));

  $("record-button").addEventListener("click", async () => {
    try {
      const res = await api.toggle_record();
      if (res && res.state) {
        state.current = res.state;
        if (res.state === "recording") {
          state.recordingStarted = Date.now();
          state.partial = "";
          setTranscript("");
        }
        renderStatus();
      }
    } catch (err) {
      toast("Opnemen mislukt: " + (err && err.message ? err.message : err), "error");
    }
  });

  $("copy-live").addEventListener("click", () => {
    const text = $("transcript").textContent.trim();
    if (text) copyText(text);
  });

  $("history-search").addEventListener("input", renderHistory);
  $("history-clear").addEventListener("click", async () => {
    if (!state.history.length) {
      toast("Er is niets om te wissen", "ok");
      return;
    }
    const button = $("history-clear");
    if (!button.classList.contains("armed")) {
      button.classList.add("armed");
      button.dataset.original = button.textContent;
      button.textContent = "Klik nogmaals om te wissen";
      setTimeout(() => {
        if (button.classList.contains("armed")) {
          button.classList.remove("armed");
          button.textContent = button.dataset.original;
        }
      }, 3200);
      return;
    }
    button.classList.remove("armed");
    button.textContent = button.dataset.original || "Alles wissen";
    try {
      const res = await api.clear_history();
      state.history = (res && res.history) || [];
      state.stats = (res && res.stats) || computeStats(state.history);
      renderHistory();
      renderRecent();
      renderStats();
      toast("Geschiedenis gewist", "ok");
    } catch (err) {
      toast("Wissen mislukt", "error");
    }
  });

  $("save-settings").addEventListener("click", saveSettings);
  $("learn-clear").addEventListener("click", clearLearning);
  $("learn-word-add").addEventListener("click", addVocabulary);
  $("learn-word").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addVocabulary();
    }
  });
  $("reset-settings").addEventListener("click", () => {
    renderSettings();
    toast("Wijzigingen hersteld", "ok");
  });
  $("set-model").addEventListener("change", updateModelHints);
  $("set-language").addEventListener("change", updateModelHints);
  $("set-preview-model").addEventListener("change", updateModelHints);
  $("set-backend").addEventListener("change", renderCloud);
  $("set-gemini-model").addEventListener("change", renderCloud);
  $("save-cloud-key").addEventListener("click", saveCloudKey);
  $("clear-cloud-key").addEventListener("click", clearCloudKey);
  $("set-cloud-key").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveCloudKey();
    }
  });

  $("set-hotkey").addEventListener("click", startCapture);
  $("hotkey-record").addEventListener("click", startCapture);
  window.addEventListener("keydown", handleCaptureKey, true);
  window.addEventListener("mousedown", (event) => {
    if (!state.capture) return;
    if ($("hotkey-field").contains(event.target)) return;
    stopCapture();
    $("set-hotkey").value = state.settings.hotkey || "<f9>";
    $("hotkey-hint").textContent = "Klik op het veld en druk de combinatie die je wilt gebruiken.";
    $("hotkey-hint").classList.remove("warn");
  }, true);

  $("refresh-devices").addEventListener("click", refreshDevices);
  $("mic-test").addEventListener("click", testMicrophone);

  $("listening-toggle").addEventListener("change", async () => {
    const wanted = $("listening-toggle").checked;
    try {
      const res = await api.set_listening(wanted);
      if (!res || res.ok === false) {
        $("listening-toggle").checked = !wanted;
        toast((res && res.error) || "Sneltoets kon niet worden aangepast", "error");
        return;
      }
      state.listening = Boolean(res.listening);
      renderStatus();
      toast(state.listening ? "Sneltoets actief" : "Sneltoets uit", "ok");
    } catch (err) {
      $("listening-toggle").checked = !wanted;
      toast("Sneltoets kon niet worden aangepast", "error");
    }
  });

  const openLog = () => api.open_log();
  const openFolder = () => api.open_folder();
  const openCrashLog = async () => {
    try {
      const result = await api.open_crash_log();
      if (!result || result.ok === false) {
        toast((result && result.error) || "Nog geen crashlog gevonden", "ok");
        return false;
      }
      return true;
    } catch (err) {
      toast("Crashlog openen mislukt", "error");
      return false;
    }
  };
  $("open-log").addEventListener("click", openLog);
  $("open-log-2").addEventListener("click", openLog);
  $("open-crash-log").addEventListener("click", openCrashLog);
  $("open-crash-log-2").addEventListener("click", openCrashLog);

  /* ----------------------------------------------------------- updates */
  // Nothing happens until the button is pressed: the app makes no update
  // request at startup, so the offline promise in the README holds.
  async function checkUpdates() {
    const button = $("check-updates");
    const status = $("update-status");
    const download = $("download-update");
    button.disabled = true;
    download.hidden = true;
    delete download.dataset.url;
    status.textContent = "Bezig met controleren…";
    try {
      const result = await api.check_updates();
      if (!result || result.ok === false) {
        status.textContent = (result && result.error) || "Kon de releasedienst niet bereiken.";
        return;
      }
      if (result.available) {
        status.textContent = `Versie ${result.latest} is beschikbaar; je hebt ${result.current}.`;
        if (result.url) {
          download.dataset.url = result.url;
          download.hidden = false;
        }
        toast(`Versie ${result.latest} is beschikbaar`, "ok");
      } else {
        status.textContent = `Je hebt de nieuwste versie (${result.current}).`;
      }
    } catch (err) {
      logJs("update check failed: " + (err && err.message ? err.message : err));
      status.textContent = "Kon de releasedienst niet bereiken.";
    } finally {
      button.disabled = false;
    }
  }
  $("check-updates").addEventListener("click", checkUpdates);
  $("download-update").addEventListener("click", async () => {
    const url = $("download-update").dataset.url;
    if (!url) return;
    try {
      const result = await api.open_release(url);
      if (result && result.ok === false) {
        toast(result.error || "Downloadlink openen mislukt", "error");
      }
    } catch (err) {
      toast("Downloadlink openen mislukt", "error");
    }
  });
  $("crash-notice-open").addEventListener("click", async () => {
    if (await openCrashLog()) {
      state.crashDismissed = true;
      renderCrashNotice();
    }
  });
  $("crash-notice-dismiss").addEventListener("click", () => {
    state.crashDismissed = true;
    renderCrashNotice();
  });
  $("storage-notice-dismiss").addEventListener("click", async () => {
    state.storageWarnings = [];
    renderStorageNotice();
    try {
      await api.dismiss_storage_warnings();
    } catch (err) {
      logJs("could not dismiss storage warnings: " + (err && err.message ? err.message : err));
    }
  });
  $("mic-notice-settings").addEventListener("click", () => {
    showView("settings");
    const select = $("set-device");
    if (!select) return;
    select.focus({ preventScroll: true });
    select.scrollIntoView({ block: "center", behavior: "smooth" });
  });
  $("open-folder").addEventListener("click", openFolder);
  $("open-folder-2").addEventListener("click", openFolder);
  $("quit-button").addEventListener("click", () => api.quit_app());
  $("quit-button-2").addEventListener("click", () => api.quit_app());

  window.addEventListener("hashchange", () => showView((location.hash || "#dictate").slice(1)));
  window.addEventListener("pywebviewready", attachBridge);
  window.addEventListener("error", (event) => {
    logJs(`${event.message} @ ${event.filename}:${event.lineno}`);
  });

  showView((location.hash || "#dictate").slice(1));
  start();

  /* ------------------------------------------------------------------ mock */
  function createMock() {
    const now = Date.now() / 1000;
    const demoItems = [
      { id: "m1", ts: now - 120, text: "Voeg een donkere modus toe aan de instellingenpagina.", duration: 3.8, model: "small", language: "nl", mode: "paste" },
      { id: "m2", ts: now - 4200, text: "De WebSocket moet opnieuw verbinden met exponentiële backoff, maximaal vijf pogingen.", duration: 6.1, model: "small", language: "nl", mode: "paste" },
      { id: "m3", ts: now - 96000, text: "Maak de inlogpagina sneller en verwijder de ongebruikte CSS.", duration: 4.4, model: "small", language: "nl", mode: "paste" },
    ];
    let history = demoItems.slice();
    let learning = { corrections: [], vocabulary: [] };
    let stateName = "idle";
    let timers = [];
    const learningPayload = () => ({
      corrections: learning.corrections.slice(),
      vocabulary: learning.vocabulary.slice(),
      total: learning.corrections.length + learning.vocabulary.length,
    });
    const settings = {
      hotkey: "<f9>", hold_to_talk: true, overlay: true, stream_preview: true,
      model: "small", preview_model: "base", language: "nl", input_device: null,
      cpu_threads: 4, beam_size: 1, mode: "paste", restore_clipboard: true,
      post_space: true, initial_prompt: "Freebuff, Codebuff, React, TypeScript, useEffect, WebSocket, API, CSS, backoff",
      max_seconds: 120, min_seconds: 0.35, beeps: true, online_backend: "local",
      gemini_model: "gemini-3.5-flash-lite", crash_logging: true,
    };
    const PROVIDER_META = {
      groq: {
        name: "Groq", model: "whisper-large-v3-turbo",
        free_limits: "20 verzoeken/min · 2.000/dag · 7.200 audio-seconden/uur",
        keys_url: "https://console.groq.com/keys", key_hint: "gsk_...",
      },
      gemini: {
        name: "Google Gemini", model: "gemini-3.5-flash-lite",
        free_limits: "Gratis tier · daglimiet en snelheid verschillen per model (zie AI Studio)",
        keys_url: "https://aistudio.google.com/app/apikey", key_hint: "AIza...",
      },
    };
    const cloudKeys = { groq: false, gemini: false };
    const cloudPayload = () => {
      const backend = settings.online_backend || "local";
      const meta = PROVIDER_META[backend] || {};
      const picked = settings.gemini_model || GEMINI_FALLBACK[0].value;
      return {
        backend,
        provider: backend === "local" ? "groq" : backend,
        name: meta.name || "Lokaal (Whisper)",
        model: backend === "gemini" ? picked : (meta.model || ""),
        gemini_model: picked,
        gemini_models: GEMINI_FALLBACK,
        free_limits: meta.free_limits || "",
        keys_url: meta.keys_url || "",
        key_hint: meta.key_hint || "",
        has_key: Boolean(cloudKeys[backend]),
        keys: { ...cloudKeys },
        providers: {
          groq: { ...PROVIDER_META.groq },
          gemini: { ...PROVIDER_META.gemini, model: picked },
        },
      };
    };
    const demo = "Maak de dicteerknop duidelijker en toon de status van de microfoon.";
    const clear = () => { timers.forEach(clearTimeout); timers = []; };
    const emit = (event) => window.dictate.push(event);

    return {
      async boot() {
        return {
          state: stateName, listening: true, settings, history,
          stats: computeStats(history),
          learning: learningPayload(),
          devices: [
            { id: "Microfoon (Realtek Audio)", name: "Microfoon (Realtek Audio)", label: "Microfoon (Realtek Audio)", index: 1, default: true, api: "Windows WASAPI", rate: 44100 },
            { id: "Microfoon (USB-headset)", name: "Microfoon (USB-headset)", label: "Microfoon (USB-headset)", index: 3, default: false, api: "Windows WASAPI", rate: 48000 },
          ],
          cloud: cloudPayload(),
          autostart: false, hotkey_label: "F9", version: "2.0.12 (demo)",
          crash_log_path: "crash.log (demo)",
          models: MODEL_ORDER.concat(["tiny", "base", "small", "medium"].map((name) => `${name}.en`)),
          model_dir: "C:\\Users\\gebruiker\\AppData\\Roaming\\Dicteer\\models",
          config_path: "C:\\Users\\gebruiker\\AppData\\Roaming\\Dicteer\\config.json",
        };
      },
      async toggle_record() {
        if (stateName === "idle") {
          stateName = "recording";
          emit({ type: "state", value: stateName, listening: true });
          const words = demo.split(" ");
          words.forEach((word, index) => {
            timers.push(setTimeout(() => {
              emit({ type: "partial", value: words.slice(0, index + 1).join(" ") });
              emit({ type: "level", value: 0.2 + Math.random() * 0.7 });
            }, 190 * (index + 1)));
          });
        } else if (stateName === "recording") {
          clear();
          stateName = "transcribing";
          emit({ type: "state", value: stateName, listening: true });
          timers.push(setTimeout(() => {
            stateName = "idle";
            const item = { id: "m" + Math.random().toString(16).slice(2, 8), ts: Date.now() / 1000, text: demo, duration: 4.2, model: settings.model, language: settings.language, mode: settings.mode };
            history = [item, ...history];
            emit({ type: "delivered", value: { ...item, history, stats: computeStats(history) } });
            emit({ type: "state", value: "idle", listening: true });
          }, 1500));
        }
        return { state: stateName };
      },
      async save_settings(updates) {
        if (updates.language !== "en" && updates.language !== "auto" &&
            ((updates.model || "").endsWith(".en") || (updates.preview_model || "").endsWith(".en"))) {
          return { ok: false, error: `'${updates.model}' begrijpt alleen Engels. Kies een meertalig model zoals 'small'.` };
        }
        if (!/^<f\d+>$|^<ctrl>|^<alt>|^<shift>/i.test(updates.hotkey || "")) {
          return { ok: false, error: "Sneltoets is geen geldige combinatie." };
        }
        if (updates.online_backend !== "local" && !cloudKeys[updates.online_backend]) {
          const name = (PROVIDER_META[updates.online_backend] || {}).name || "online";
          return { ok: false, error: `Sla eerst je gratis ${name}-sleutel op voordat je online gaat.` };
        }
        if (updates.gemini_model && !GEMINI_FALLBACK.some((entry) => entry.value === updates.gemini_model)) {
          return { ok: false, error: "Kies een Gemini-model uit de lijst." };
        }
        Object.assign(settings, updates);
        return { ok: true, settings: { ...settings }, cloud: cloudPayload() };
      },
      async save_cloud_key(key, provider) {
        const clean = String(key || "").trim();
        if (clean.length < 20) return { ok: false, error: "Dit lijkt geen geldige API-sleutel.", cloud: cloudPayload() };
        const name = provider in PROVIDER_META ? provider : "groq";
        cloudKeys[name] = true;
        return { ok: true, cloud: cloudPayload() };
      },
      async clear_cloud_key(provider) {
        const name = provider in PROVIDER_META ? provider : "groq";
        const removed = cloudKeys[name];
        cloudKeys[name] = false;
        return { ok: true, removed, cloud: cloudPayload() };
      },
      async cloud_status() { return cloudPayload(); },
      async validate_hotkey(hotkey) {
        if (!hotkey || !/^</.test(hotkey)) return { ok: false, error: "Gebruik een combinatie zoals <f9>." };
        return { ok: true };
      },
      async set_listening(enabled) { return { ok: true, listening: enabled }; },
      async set_autostart(enabled) { return { ok: true, enabled }; },
      async copy() { return { ok: true }; },
      async delete_history(id) {
        history = history.filter((item) => item.id !== id);
        return { ok: true, history, stats: computeStats(history) };
      },
      async clear_history() {
        history = [];
        return { ok: true, history, stats: computeStats(history) };
      },
      async correct_history(id, text) {
        const item = history.find((entry) => entry.id === id);
        const clean = String(text || "").trim();
        if (!item) return { ok: false, error: "Dit dictaat bestaat niet meer." };
        if (!clean) return { ok: false, error: "Tekst mag niet leeg zijn." };
        const old = item.text;
        item.text = clean;
        item.edited = true;
        const learned = { corrections: 0, words: 0 };
        if (settings.learn_from_corrections !== false && old !== clean) {
          const tidy = (word) => word.replace(/[.,;:!?]+$/, "");
          const before = old.split(/\s+/);
          const after = clean.split(/\s+/);
          for (let i = 0; i < Math.min(before.length, after.length); i += 1) {
            const heard = tidy(before[i]).toLowerCase();
            const fixed = tidy(after[i]);
            if (heard !== fixed.toLowerCase() && heard.length >= 3) {
              learning.corrections = learning.corrections.filter((row) => row.heard !== heard);
              learning.corrections.unshift({ heard, fixed, count: 1 });
              learned.corrections = 1;
              break;
            }
          }
          after.forEach((word, index) => {
            const cleanWord = tidy(word);
            const previous = index > 0 ? after[index - 1] : "";
            const sentenceStart = !previous || /[.!?…:;]$/.test(previous);
            const technical = cleanWord.length >= 3 && (
              /[A-Z]/.test(cleanWord.slice(1)) || /\d/.test(cleanWord) ||
              /[._-]/.test(cleanWord) || (cleanWord === cleanWord.toUpperCase() && cleanWord.length >= 2) ||
              (/^[A-Z][a-z]+$/.test(cleanWord) && !sentenceStart)
            );
            if (technical && !learning.vocabulary.some((row) => row.word === cleanWord)) {
              learning.vocabulary.unshift({ word: cleanWord, count: 1 });
              learned.words += 1;
            }
          });
        }
        return { ok: true, item, history, stats: computeStats(history), learning: learningPayload(), learned };
      },
      async add_vocabulary(word) {
        const clean = String(word || "").trim().replace(/^[.,;:!?]+|[.,;:!?]+$/g, "");
        if (!clean || /\s/.test(clean) || clean.length > 40) {
          return { ok: false, error: "Vul één woord in zonder spaties, maximaal 40 tekens.", learning: learningPayload() };
        }
        const row = learning.vocabulary.find((entry) => entry.word === clean);
        if (row) row.count += 1;
        else learning.vocabulary.unshift({ word: clean, count: 1 });
        return { ok: true, learning: learningPayload() };
      },
      async forget_learning(kind, value) {
        if (kind === "correction") {
          learning.corrections = learning.corrections.filter((row) => row.heard !== value);
        } else if (kind === "word" || kind === "vocabulary") {
          learning.vocabulary = learning.vocabulary.filter((row) => row.word !== value);
        } else {
          return { ok: false, error: "Onbekend soort leeritem." };
        }
        return { ok: true, removed: true, learning: learningPayload() };
      },
      async clear_learning() {
        learning = { corrections: [], vocabulary: [] };
        return { ok: true, learning: learningPayload() };
      },
      async list_devices() {
        return { devices: (await this.boot()).devices };
      },
      async test_microphone(seconds, device) {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        return { ok: true, peak: 0.14, dbfs: -17.1, verdict: "goed", device: device ? "Gekozen microfoon" : "Realtek Audio", seconds: 2 };
      },
      async check_updates() {
        await new Promise((resolve) => setTimeout(resolve, 350));
        return {
          ok: true,
          current: "2.0.12",
          latest: "2.1.0",
          available: true,
          name: "Dicteer v2.1.0",
          url: "https://github.com/DrIamlegion/Dicteer/releases/tag/v2.1.0",
          page: "https://github.com/DrIamlegion/Dicteer/releases/latest",
        };
      },
      async open_release() { return { ok: true }; },
      async open_log() {
        return { ok: true };
      },
      async open_crash_log() {
        return { ok: false, error: "Nog geen crashlog in deze demo." };
      },
      async open_folder() { return { ok: true }; },
      async dismiss_storage_warnings() { return { ok: true }; },
      async quit_app() { toast("Demo: afsluiten werkt alleen in de app."); return { ok: true }; },
    };
  }
})();
