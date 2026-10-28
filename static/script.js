/* ============================================================
   Campus Assistant — front-end
   - Chat (streaming + fallback) talks to the existing Flask API:
       POST /api/chat-stream   POST /api/chat   GET /api/live-updates
   - Conversations are saved in the browser (localStorage) because the
     backend has no user accounts: each visitor keeps their own private
     history, and it survives refreshes / reopening the page.
   ============================================================ */
(() => {
  "use strict";

  // ------------------------------------------------------------ constants
  const STORE_KEY = "campus_chats_v1";
  const ACTIVE_KEY = "campus_active_v1";
  const THEME_KEY = "campus_theme";
  const SB_KEY = "campus_sb_collapsed";
  const MAX_CONVS = 200;
  const MAX_TITLE = 60;
  const MOBILE_MQ = window.matchMedia("(max-width: 860px)");

  // ------------------------------------------------------------ elements
  const $ = (id) => document.getElementById(id);
  const appEl = $("app"), sidebar = $("sidebar"), scrim = $("scrim");
  const chatWindow = $("chat-window"), messagesEl = $("messages"), viewChat = $("view-chat");
  const viewHistory = $("view-history");
  const chatForm = $("chat-form"), chatInput = $("chat-input"), sendBtn = $("send-btn"), charCount = $("char-count");
  const newBtn = $("new-btn"), navHistory = $("nav-history"), historyCount = $("history-count");
  const recentList = $("recent-list"), recentEmpty = $("recent-empty");
  const historyList = $("history-list"), historyEmpty = $("history-empty"), historySearch = $("history-search");
  const historySub = $("history-sub");
  const topTitle = $("top-title"), baseTitle = topTitle.textContent;
  const confirmDialog = $("confirm-dialog"), toastEl = $("toast");

  // ------------------------------------------------------------ state
  let convs = [];          // saved conversations (only ones with >= 1 message), newest first
  let current = null;      // the conversation shown in the chat view (may be an unsaved empty draft)
  let view = "chat";       // "chat" | "history"
  let live = null;         // {conv, msg, bubble, col, controller} while an answer is streaming
  let busy = false;
  let searchTerm = "";

  // ------------------------------------------------------------ helpers
  const uid = () => "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  const now = () => Date.now();

  function toast(text, ms = 2600) {
    toastEl.textContent = text;
    toastEl.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { toastEl.hidden = true; }, ms);
  }

  function makeTitle(text) {
    const t = text.replace(/\s+/g, " ").trim();
    return t.length > MAX_TITLE ? t.slice(0, MAX_TITLE - 1).trimEnd() + "…" : t || "New chat";
  }

  const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const dateFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
  function formatWhen(ts) {
    const d = new Date(ts), n = new Date();
    const mins = Math.floor((n - d) / 60000);
    if (mins < 1) return "Just now";
    if (mins < 60) return `${mins} min ago`;
    const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const days = Math.round((startOf(n) - startOf(d)) / 86400000);
    if (days === 0) return `Today, ${timeFmt.format(d)}`;
    if (days === 1) return `Yesterday, ${timeFmt.format(d)}`;
    return `${dateFmt.format(d)}, ${timeFmt.format(d)}`;
  }

  function svg(path, size = 16) {
    return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
  }
  const ICON_EDIT = svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>');
  const ICON_TRASH = svg('<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/>');

  function iconButton(cls, label, html, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "icon-btn " + cls;
    b.setAttribute("aria-label", label);
    b.title = label;
    b.innerHTML = html;
    b.addEventListener("click", (e) => { e.stopPropagation(); onClick(b); });
    return b;
  }

  function highlight(el, text, term) {
    el.textContent = "";
    if (!term) { el.textContent = text; return; }
    const i = text.toLowerCase().indexOf(term.toLowerCase());
    if (i < 0) { el.textContent = text; return; }
    el.append(text.slice(0, i));
    const m = document.createElement("mark");
    m.textContent = text.slice(i, i + term.length);
    el.append(m, text.slice(i + term.length));
  }

  // ------------------------------------------------------------ photos (campus galleries)
  function httpsUrl(u) { try { return new URL(u).protocol === "https:"; } catch (e) { return false; } }
  function safePhotos(list) {
    return (Array.isArray(list) ? list : [])
      .filter((p) => p && httpsUrl(p.url) && httpsUrl(p.source_page) && typeof p.caption === "string")
      .map((p) => ({ url: p.url, caption: p.caption.slice(0, 200), source_page: p.source_page, campus: String(p.campus || "") }));
  }
  function safeLinks(list) {
    return (Array.isArray(list) ? list : [])
      .filter((l) => l && httpsUrl(l.url) && typeof l.title === "string")
      .map((l) => ({ title: l.title.slice(0, 120), url: l.url }));
  }
  function openLink(href, label) {
    const a = document.createElement("a");
    a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = label;
    return a;
  }
  function renderMedia(col, m) {
    const photos = safePhotos(m.photos), links = safeLinks(m.links);
    if (photos.length) {
      const grid = document.createElement("div");
      grid.className = "photo-grid";
      for (const p of photos) {
        const fig = document.createElement("figure");
        fig.className = "photo-card";
        fig.dataset.campus = p.campus;
        const img = document.createElement("img");
        img.src = p.url; img.alt = p.caption; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
        img.addEventListener("error", () => {          // never leave a broken-image icon
          img.remove();
          const miss = document.createElement("div");
          miss.className = "photo-missing";
          miss.textContent = "Image could not be loaded.";
          fig.insertBefore(miss, fig.firstChild);
        });
        const cap = document.createElement("figcaption");
        cap.append(document.createTextNode(p.caption + " \u00b7 "), openLink(p.source_page, "Source"));
        fig.append(img, cap);
        grid.appendChild(fig);
      }
      col.appendChild(grid);
    }
    if (links.length) {
      const box = document.createElement("div");
      box.className = "media-links";
      for (const l of links) box.appendChild(openLink(l.url, l.title + " \u2197"));
      col.appendChild(box);
    }
  }

  // ------------------------------------------------------------ storage
  function sanitizeConv(c) {
    if (!c || typeof c !== "object" || typeof c.id !== "string" || !Array.isArray(c.messages)) return null;
    const messages = c.messages
      .filter((m) => m && (m.role === "user" || m.role === "bot") && typeof m.text === "string")
      .filter((m) => !(m.pending && !m.text))                         // answer that never started
      .map((m) => ({ role: m.role, text: m.text, err: !!m.err, photos: safePhotos(m.photos), links: safeLinks(m.links) }));
    if (!messages.length) return null;
    return {
      id: c.id,
      title: typeof c.title === "string" && c.title.trim() ? c.title.trim().slice(0, 120) : makeTitle(messages[0].text),
      renamed: !!c.renamed,
      created: Number(c.created) || now(),
      updated: Number(c.updated) || Number(c.created) || now(),
      messages,
    };
  }

  function loadConvs() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (!raw) return [];
      const data = JSON.parse(raw);
      if (!Array.isArray(data)) return [];
      const seen = new Set();
      return data.map(sanitizeConv).filter((c) => c && !seen.has(c.id) && seen.add(c.id))
        .sort((a, b) => b.updated - a.updated);
    } catch (e) {
      console.warn("Could not read saved chats:", e);
      return [];
    }
  }

  function saveConvs() {
    // strip live (unfinished) answers before writing; the finished text is saved when streaming ends
    try {
      const out = convs.slice(0, MAX_CONVS).map((c) => ({
        ...c,
        messages: c.messages.filter((m) => !m.pending).map(({ role, text, err, photos, links }) => ({ role, text, err, photos: safePhotos(photos), links: safeLinks(links) })),
      }));
      localStorage.setItem(STORE_KEY, JSON.stringify(out));
      return true;
    } catch (e) {
      console.warn("Could not save chats:", e);
      toast("Couldn't save this chat — browser storage is full or blocked.");
      return false;
    }
  }

  function saveActive() {
    try {
      if (current && convs.includes(current)) localStorage.setItem(ACTIVE_KEY, current.id);
      else localStorage.removeItem(ACTIVE_KEY);
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------ conversation model
  function newDraft() {
    return { id: uid(), title: "New chat", renamed: false, created: now(), updated: now(), messages: [] };
  }

  function sortConvs() { convs.sort((a, b) => b.updated - a.updated); }

  function serverHistory(conv) {
    // same shape the backend already expects: [{role: "user"|"bot", text}]
    return conv.messages.filter((m) => !m.pending && !m.err && m.text).slice(-8).map((m) => ({ role: m.role, text: m.text }));
  }

  // ------------------------------------------------------------ message rendering
  function addRow(sender, parent = messagesEl) {
    const row = document.createElement("div");
    row.className = `row ${sender}`;
    if (sender === "bot") {
      const avatar = document.createElement("div");
      avatar.className = "avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.textContent = "🎓";
      row.appendChild(avatar);
    }
    const col = document.createElement("div");
    col.className = "col";
    const bubble = document.createElement("div");
    bubble.className = "bubble" + (sender === "bot" ? " bot-bubble" : "");
    col.appendChild(bubble);
    row.appendChild(col);
    parent.appendChild(row);
    return { row, col, bubble };
  }

  function typingDots(bubble) {
    bubble.classList.add("typing-bubble");
    bubble.setAttribute("aria-label", "Assistant is typing");
    bubble.innerHTML = '<span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>';
  }

  function stripNotices(text) {       // no quota notice, no "Sources:" block (also cleans old saved chats)
    return String(text || "")
      .replace(/^.*gemini quota reached.*$\n?/gim, "")
      .replace(/^.*showing available verified information.*$\n?/gim, "")
      .replace(/\n*\s*sources?\s*:[\s\S]*$/i, "")
      .trim();
  }

  function softClean(text) {          // while streaming: hide markdown symbols so it always looks clean
    return stripNotices(text).replace(/\*\*/g, "").replace(/__/g, "").replace(/`/g, "")
      .replace(/^#{1,6}\s*/gm, "").replace(/^[*-]\s+/gm, "• ");
  }

  function nearBottom() { return chatWindow.scrollHeight - chatWindow.scrollTop - chatWindow.clientHeight < 140; }
  function scrollToBottom(force) {
    if (force || nearBottom()) chatWindow.scrollTo({ top: chatWindow.scrollHeight, behavior: "auto" });
  }

  function renderConversation() {
    messagesEl.textContent = "";
    live && (live.bubble = null);
    const has = current.messages.length > 0;
    viewChat.classList.toggle("is-chatting", has);
    for (const m of current.messages) {
      const r = addRow(m.role);
      if (m.pending) {
        // an answer that is still streaming for this conversation: re-attach the live bubble
        if (live && live.msg === m) { live.bubble = r.bubble; live.col = r.col; }
        if (m.text) r.bubble.textContent = softClean(m.text); else typingDots(r.bubble);
      } else {
        r.bubble.textContent = stripNotices(m.text);
        renderMedia(r.col, m);
        if (m.err) r.bubble.classList.add("err");
      }
    }
    chatWindow.scrollTop = has ? chatWindow.scrollHeight : 0;
    updateTitle();
    updateSend();
  }

  function updateTitle() {
    topTitle.textContent = view === "history" ? "History" : (current && current.messages.length ? current.title : baseTitle);
  }

  // ------------------------------------------------------------ sending / streaming
  async function readStream(res, getBubble, msg) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", raw = "", final = null, started = false;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line); } catch (e) { continue; }
        const b = getBubble();
        if (ev.type === "delta") {
          raw += ev.text;
          msg.text = raw;
          if (b) {
            if (!started) { b.classList.remove("typing-bubble"); b.removeAttribute("aria-label"); }
            b.textContent = softClean(raw);
            scrollToBottom(false);
          }
          started = true;
        } else if (ev.type === "final") {
          final = ev;
        }
      }
    }
    return { final, raw };
  }

  async function sendMessage(text) {
    text = (text || "").trim().slice(0, 500);
    if (!text || busy) return;
    stopVoice();
    if (view !== "chat") setView("chat");

    // first message: the draft becomes a real, saved conversation (title from first message)
    if (!convs.includes(current)) {
      current.title = makeTitle(text);
      current.created = now();
      convs.unshift(current);
    }
    const history = serverHistory(current);            // history BEFORE this question
    current.messages.push({ role: "user", text });
    const botMsg = { role: "bot", text: "", pending: true, err: false };
    current.messages.push(botMsg);
    current.updated = now();
    sortConvs();
    saveConvs();
    saveActive();

    busy = true;
    const controller = new AbortController();
    live = { conv: current, msg: botMsg, bubble: null, col: null, controller };
    chatInput.value = "";
    autosize();
    renderConversation();
    renderLists();
    scrollToBottom(true);

    const conv = current;
    let result = null, failed = false;
    try {
      // the history that existed before this turn is sent along; the new question travels as `message`
      result = await askServer(JSON.stringify({ message: text, history }), botMsg, controller);
    } catch (err) {
      if (err.name === "AbortError") { finish(conv, botMsg, null, true); return; }
      failed = true;
    }
    finish(conv, botMsg, failed ? null : result, false);
  }

  // streaming first (words appear while the AI writes), normal request as fallback
  async function askServer(body, msg, controller) {
    const headers = { "Content-Type": "application/json" };
    const getBubble = () => (live && live.msg === msg ? live.bubble : null);
    if (window.ReadableStream && window.TextDecoder) {
      try {
        const res = await fetch("/api/chat-stream", { method: "POST", headers, body, signal: controller.signal });
        if (res.ok && res.body) {
          const { final, raw } = await readStream(res, getBubble, msg);
          if (final) return { text: final.text || softClean(raw), final };
          if (raw) return { text: softClean(raw), final: null };
        }
      } catch (e) {
        if (e.name === "AbortError") throw e;
      }
    }
    const res = await fetch("/api/chat", { method: "POST", headers, body, signal: controller.signal });
    const data = await res.json();
    return { text: data.reply || "Sorry, something went wrong.", final: { mode: data.mode, sources: data.sources, ms: data.ms, photos: data.photos, links: data.links } };
  }

  function finish(conv, msg, result, aborted) {
    const stillThere = convs.includes(conv);
    delete msg.pending;
    if (aborted || !stillThere) {
      // conversation was deleted while answering — nothing to keep
      busy = false; live = null; updateSend();
      return;
    }
    if (!result) {
      msg.text = msg.text ? softClean(msg.text) : "Network error. Please check your connection and try again.";
      msg.err = !msg.text || msg.text.startsWith("Network error");
    } else {
      msg.text = stripNotices(result.text);
      msg.photos = safePhotos(result.final && result.final.photos);
      msg.links = safeLinks(result.final && result.final.links);
    }
    conv.updated = now();
    sortConvs();
    saveConvs();
    busy = false;
    live = null;
    if (current === conv && view === "chat") {
      renderConversation();
      scrollToBottom(true);
      chatInput.focus({ preventScroll: true });
    }
    renderLists();
    updateSend();
  }

  // ------------------------------------------------------------ New / open
  function startNew() {
    stopVoice();
    saveConvs();                                   // make sure the previous conversation is stored
    const reuse = current && current.messages.length === 0;
    if (!reuse) current = newDraft();              // an empty draft is reused → no duplicate/blank entries
    saveActive();
    chatInput.value = "";
    autosize();
    renderConversation();
    setView("chat");
    renderLists();
    closeDrawer();
    chatInput.focus();
  }

  function openConv(id) {
    const c = convs.find((x) => x.id === id);
    if (!c) { toast("That conversation no longer exists."); renderLists(); return; }
    current = c;
    saveActive();
    chatInput.value = "";
    autosize();
    renderConversation();
    setView("chat");
    renderLists();
    closeDrawer();
    chatInput.focus({ preventScroll: true });
  }

  // ------------------------------------------------------------ rename / delete
  function commitRename(c, value) {
    const v = value.replace(/\s+/g, " ").trim().slice(0, 120);
    if (v && v !== c.title) {
      c.title = v; c.renamed = true;
      saveConvs();
      updateTitle();
      toast("Conversation renamed");
    }
    renderLists();
  }

  function startRename(c, titleHost, focusBackTo) {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "rename-input";
    input.value = c.title;
    input.maxLength = 120;
    input.setAttribute("aria-label", "Conversation name");
    let done = false;
    const finishEdit = (save) => {
      if (done) return; done = true;
      if (save) commitRename(c, input.value); else renderLists();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finishEdit(true); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finishEdit(false); }
      e.stopPropagation();
    });
    input.addEventListener("blur", () => finishEdit(true));
    input.addEventListener("click", (e) => e.stopPropagation());
    titleHost.replaceWith(input);
    input.focus(); input.select();
  }

  function askConfirm(title, text, okLabel) {
    return new Promise((resolve) => {
      if (typeof confirmDialog.showModal !== "function") { resolve(window.confirm(text)); return; }
      $("confirm-title").textContent = title;
      $("confirm-text").textContent = text;
      $("confirm-ok").textContent = okLabel;
      confirmDialog.returnValue = "cancel";
      const onClose = () => { confirmDialog.removeEventListener("close", onClose); resolve(confirmDialog.returnValue === "ok"); };
      confirmDialog.addEventListener("close", onClose);
      confirmDialog.showModal();
      $("confirm-cancel").focus();
    });
  }

  async function deleteConv(c) {
    const ok = await askConfirm("Delete conversation?", `“${c.title}” and all its messages will be permanently deleted. This can't be undone.`, "Delete");
    if (!ok) return;
    if (live && live.conv === c) live.controller.abort();
    convs = convs.filter((x) => x.id !== c.id);
    saveConvs();                                    // rewrites the stored list without it → really removed
    if (current === c) { current = newDraft(); renderConversation(); }
    saveActive();
    renderLists();
    toast("Conversation deleted");
  }

  // ------------------------------------------------------------ lists (sidebar "Recent" + History view)
  function matches(c, term) {
    if (!term) return true;
    const t = term.toLowerCase();
    return c.title.toLowerCase().includes(t) || c.messages.some((m) => m.text.toLowerCase().includes(t));
  }

  function snippetFor(c, term) {
    if (!term) {
      const last = [...c.messages].reverse().find((m) => !m.pending && m.text);
      return last ? last.text.replace(/\s+/g, " ").slice(0, 110) : "";
    }
    const t = term.toLowerCase();
    const m = c.messages.find((x) => x.text.toLowerCase().includes(t));
    if (!m || c.title.toLowerCase().includes(t)) return c.messages[0] ? c.messages[0].text.replace(/\s+/g, " ").slice(0, 110) : "";
    const i = m.text.toLowerCase().indexOf(t);
    const s = Math.max(0, i - 30);
    return (s > 0 ? "…" : "") + m.text.slice(s, s + 110).replace(/\s+/g, " ");
  }

  function renderRecent() {
    recentList.textContent = "";
    const items = convs.slice(0, 12);
    recentEmpty.hidden = items.length > 0;
    for (const c of items) {
      const li = document.createElement("li");
      li.className = "recent-item" + (c === current && view === "chat" ? " is-active" : "");
      const open = document.createElement("button");
      open.type = "button";
      open.className = "recent-open";
      open.textContent = c.title;
      open.title = c.title;
      if (c === current && view === "chat") open.setAttribute("aria-current", "true");
      open.addEventListener("click", () => openConv(c.id));
      const edit = iconButton("", `Rename “${c.title}”`, ICON_EDIT, () => startRename(c, open));
      const del = iconButton("danger", `Delete “${c.title}”`, ICON_TRASH, () => deleteConv(c));
      li.append(open, edit, del);
      recentList.appendChild(li);
    }
  }

  function renderHistory() {
    historyList.textContent = "";
    const total = convs.length;
    const items = convs.filter((c) => matches(c, searchTerm));
    historySub.textContent = total
      ? (searchTerm ? `${items.length} of ${total} conversation${total === 1 ? "" : "s"}` : `${total} saved conversation${total === 1 ? "" : "s"}`)
      : "Your saved conversations";
    historySearch.disabled = total === 0;

    const empty = items.length === 0;
    historyEmpty.hidden = !empty;
    if (empty) {
      const searching = total > 0 && searchTerm;
      $("history-empty-title").textContent = searching ? "No matching conversations" : "No conversations yet";
      $("history-empty-text").textContent = searching
        ? `Nothing matches “${searchTerm}”. Try a different word.`
        : "Start a chat and it will be saved here automatically.";
      $("empty-new").hidden = !!searching;
    }

    items.forEach((c, i) => {
      const li = document.createElement("li");
      li.className = "h-item";
      li.style.animationDelay = Math.min(i, 8) * 30 + "ms";
      const open = document.createElement("button");
      open.type = "button";
      open.className = "h-open";
      const title = document.createElement("span");
      title.className = "h-title";
      highlight(title, c.title, searchTerm);
      const meta = document.createElement("span");
      meta.className = "h-meta";
      const n = c.messages.filter((m) => m.role === "user").length;
      meta.textContent = `${formatWhen(c.updated)} · ${n} question${n === 1 ? "" : "s"}`;
      const snip = document.createElement("span");
      snip.className = "h-snippet";
      snip.textContent = snippetFor(c, searchTerm);
      open.append(title, meta, snip);
      open.addEventListener("click", () => openConv(c.id));
      const actions = document.createElement("div");
      actions.className = "h-actions";
      actions.append(
        iconButton("", `Rename “${c.title}”`, ICON_EDIT, () => startRename(c, title)),
        iconButton("danger", `Delete “${c.title}”`, ICON_TRASH, () => deleteConv(c)),
      );
      li.append(open, actions);
      historyList.appendChild(li);
    });
  }

  function renderLists() {
    historyCount.textContent = convs.length;
    historyCount.hidden = convs.length === 0;
    renderRecent();
    renderHistory();
  }

  // ------------------------------------------------------------ views
  function setView(next) {
    view = next;
    const isChat = next === "chat";
    viewChat.classList.toggle("is-active", isChat);
    viewHistory.classList.toggle("is-active", !isChat);
    viewChat.hidden = false; viewHistory.hidden = false;
    // keep the hidden view out of the tab order / screen readers
    viewChat.inert = !isChat;
    viewHistory.inert = isChat;
    navHistory.setAttribute("aria-current", String(!isChat));
    updateTitle();
    if (!isChat) { renderHistory(); }
    renderRecent();
    closeDrawer();
  }

  function showHistory() {
    setView("history");
    closeDrawer();
    historySearch.focus({ preventScroll: true });
  }

  // ------------------------------------------------------------ sidebar (collapse / mobile drawer)
  function applyCollapsed(collapsed) {
    appEl.classList.toggle("sb-collapsed", collapsed);
    const b = $("collapse-btn");
    b.setAttribute("aria-expanded", String(!collapsed));
    b.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
    b.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
  }
  function toggleCollapsed() {
    const next = !appEl.classList.contains("sb-collapsed");
    applyCollapsed(next);
    try { localStorage.setItem(SB_KEY, next ? "1" : "0"); } catch (e) { /* ignore */ }
  }
  function syncSidebarAccess() {
    // on phones the closed drawer is off-screen: make it unreachable by keyboard
    sidebar.inert = MOBILE_MQ.matches && !appEl.classList.contains("sb-open");
  }
  function openDrawer() {
    appEl.classList.add("sb-open");
    scrim.hidden = false;
    $("menu-btn").setAttribute("aria-expanded", "true");
    syncSidebarAccess();
    newBtn.focus();
  }
  function closeDrawer() {
    if (!appEl.classList.contains("sb-open")) { syncSidebarAccess(); return; }
    appEl.classList.remove("sb-open");
    scrim.hidden = true;
    $("menu-btn").setAttribute("aria-expanded", "false");
    syncSidebarAccess();
  }

  // ------------------------------------------------------------ theme
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    $("theme-label").textContent = t === "dark" ? "Light mode" : "Dark mode";
  }

  // ------------------------------------------------------------ composer
  function autosize() {
    chatInput.style.height = "auto";
    chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + "px";
    chatInput.style.overflowY = chatInput.scrollHeight > 160 ? "auto" : "hidden";   // no scrollbar until the box is full
    const len = chatInput.value.length;
    charCount.textContent = len > 400 ? `${len}/500` : "";
    charCount.classList.toggle("warn", len >= 480);
    updateSend();
  }
  function updateSend() {
    sendBtn.disabled = busy || !chatInput.value.trim();
    sendBtn.title = busy ? "Waiting for the answer…" : "Send (Enter)";
  }

  chatInput.placeholder = "";          // no placeholder text in the message box
  chatInput.addEventListener("input", autosize);
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); chatForm.requestSubmit(); }
  });
  chatForm.addEventListener("submit", (e) => { e.preventDefault(); sendMessage(chatInput.value); });
  document.querySelectorAll(".chip").forEach((chip) =>
    chip.addEventListener("click", () => sendMessage(chip.dataset.question)));


  // ------------------------------------------------------------ voice input (speech → text)
  // Uses the browser's built-in speech recognition (Chrome / Edge / Safari — NOT Firefox / Brave).
  // Needs http://localhost or https:// plus microphone permission.
  // Speak, then pause: the question is sent automatically. Tap the mic again to stop and edit before sending.
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const LANG_KEY = "campus_voice_lang";
  const VOICE_LANGS = [
    { code: "en-IN", label: "EN", name: "English" },
    { code: "hi-IN", label: "हिं", name: "Hindi" },
    { code: "pa-IN", label: "ਪੰ", name: "Punjabi" },
  ];
  const micBtn = $("mic-btn"), langBtn = $("lang-btn"), composerEl = $("chat-form");
  let voiceIdx = 0, recog = null, listening = false;
  let voiceBase = "", voiceGotText = false, voiceFinal = false, voiceManualStop = false, voiceErr = false, voiceRestart = false;
  try { const i = VOICE_LANGS.findIndex((l) => l.code === localStorage.getItem(LANG_KEY)); if (i >= 0) voiceIdx = i; } catch (e) { /* ignore */ }

  function paintVoice() {
    const l = VOICE_LANGS[voiceIdx];
    langBtn.textContent = l.label;
    langBtn.setAttribute("aria-label", `Voice language: ${l.name}. Click to change`);
    langBtn.title = `Voice language: ${l.name} (click to change)`;
    micBtn.classList.toggle("listening", listening);
    micBtn.setAttribute("aria-pressed", String(listening));
    micBtn.setAttribute("aria-label", listening ? "Stop voice input" : "Start voice input");
    micBtn.title = listening ? "Listening… tap to stop" : "Speak your question";
    composerEl.classList.toggle("is-listening", listening);
    chatInput.placeholder = listening ? `Listening (${l.name})… speak now` : "";
  }

  function stopVoice() {
    if (recog && listening) { voiceManualStop = true; try { recog.stop(); } catch (e) { /* ignore */ } }
  }

  function voiceError(code) {
    const here = location.origin;
    const msgs = {
      "not-allowed": window.isSecureContext
        ? "Microphone blocked hai. Browser ke address bar mein 🔒 / mic icon par click karke Microphone ko Allow karo, phir dobara try karo."
        : `Mic sirf localhost ya https par chalta hai. Abhi aap ${here} se khol rahe ho — http://localhost:5000 se kholo.`,
      "service-not-allowed": "Browser ne voice service block kar di hai. Chrome ya Edge use karo aur Microphone Allow karo.",
      "no-speech": "Kuch sunai nahi diya. Mic dabake thoda paas se bolo.",
      "audio-capture": "Microphone nahi mila. Mic connect karo ya Windows/Mac ki sound settings mein input device check karo.",
      "network": "Voice service tak connect nahi ho paya. Internet check karo — ye feature Chrome/Edge mein hi chalta hai (Brave/Firefox mein nahi).",
      "language-not-supported": "Is language ki voice aapke browser mein supported nahi hai. EN select karke try karo.",
    };
    toast(msgs[code] || "Voice input ruk gaya (" + code + ").", 6500);
  }

  function startVoice() {
    if (!SR) { toast("Is browser mein voice input supported nahi hai. Chrome, Edge ya Safari use karo.", 5000); return; }
    if (!window.isSecureContext) { voiceError("not-allowed"); return; }
    if (busy) { toast("Pehle current answer ka wait karo."); return; }
    if (listening) { stopVoice(); return; }
    if (view !== "chat") setView("chat");

    recog = new SR();
    recog.lang = VOICE_LANGS[voiceIdx].code;
    recog.interimResults = true;
    recog.continuous = false;
    recog.maxAlternatives = 1;
    voiceBase = chatInput.value.trim();
    voiceGotText = voiceFinal = voiceManualStop = voiceErr = false;

    recog.onstart = () => { listening = true; paintVoice(); };
    recog.onresult = (ev) => {
      let text = "", allFinal = true;
      for (let i = 0; i < ev.results.length; i++) {
        text += ev.results[i][0].transcript;
        if (!ev.results[i].isFinal) allFinal = false;
      }
      text = text.trim();
      voiceGotText = text.length > 0;
      voiceFinal = allFinal && voiceGotText;
      chatInput.value = ((voiceBase ? voiceBase + " " : "") + text).slice(0, 500);
      autosize();
    };
    recog.onerror = (ev) => {
      if (ev.error === "aborted") return;
      voiceErr = true;
      voiceError(ev.error);
    };
    recog.onend = () => {
      const manual = voiceManualStop, err = voiceErr, got = voiceGotText, fin = voiceFinal;
      listening = false; recog = null; paintVoice();
      if (voiceRestart) { voiceRestart = false; startVoice(); return; }
      if (got && !manual && !err) { sendMessage(chatInput.value); return; }   // natural pause → ask the question
      if (!got && !manual && !err) toast("Kuch sunai nahi diya. Mic dabake thoda paas se bolo.", 4500);
      chatInput.focus({ preventScroll: true });
    };
    try { recog.start(); }
    catch (e) { recog = null; listening = false; paintVoice(); toast("Microphone start nahi ho paya. Ek baar page refresh karke try karo."); }
  }

  micBtn.addEventListener("click", startVoice);
  langBtn.addEventListener("click", () => {
    voiceIdx = (voiceIdx + 1) % VOICE_LANGS.length;
    try { localStorage.setItem(LANG_KEY, VOICE_LANGS[voiceIdx].code); } catch (e) { /* ignore */ }
    if (listening && recog) { voiceRestart = true; stopVoice(); }      // restart in the new language
    paintVoice();
    toast("Voice language: " + VOICE_LANGS[voiceIdx].name, 1600);
  });
  paintVoice();

  // ------------------------------------------------------------ wiring
  newBtn.addEventListener("click", startNew);
  $("history-new").addEventListener("click", startNew);
  $("empty-new").addEventListener("click", startNew);
  navHistory.addEventListener("click", () => (view === "history" ? setView("chat") : showHistory()));
  $("collapse-btn").addEventListener("click", toggleCollapsed);
  $("menu-btn").addEventListener("click", openDrawer);
  scrim.addEventListener("click", closeDrawer);
  $("theme-btn").addEventListener("click", () => {
    const t = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    applyTheme(t);
    try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* ignore */ }
  });

  let searchTimer;
  historySearch.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { searchTerm = historySearch.value.trim(); renderHistory(); }, 120);
  });
  historySearch.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && historySearch.value) { historySearch.value = ""; searchTerm = ""; renderHistory(); e.stopPropagation(); }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && appEl.classList.contains("sb-open")) { closeDrawer(); $("menu-btn").focus(); }
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); startNew(); }
  });

  MOBILE_MQ.addEventListener("change", () => { closeDrawer(); syncSidebarAccess(); });

  // another tab changed the saved chats → keep this tab's lists in sync
  window.addEventListener("storage", (e) => {
    if (e.key !== STORE_KEY) return;
    const fresh = loadConvs();
    if (live) return;                                  // don't disturb an answer that is being written
    convs = fresh;
    if (current && current.messages.length && !convs.some((c) => c.id === current.id)) { current = newDraft(); renderConversation(); }
    else if (current) { const same = convs.find((c) => c.id === current.id); if (same) { current = same; renderConversation(); } }
    renderLists();
  });

  window.addEventListener("pagehide", saveConvs);

  // ------------------------------------------------------------ boot
  (function init() {
    applyTheme(document.documentElement.getAttribute("data-theme") || "light");
    try { applyCollapsed(localStorage.getItem(SB_KEY) === "1"); } catch (e) { applyCollapsed(false); }

    convs = loadConvs();
    let lastId = null;
    try { lastId = localStorage.getItem(ACTIVE_KEY); } catch (e) { /* ignore */ }
    current = convs.find((c) => c.id === lastId) || newDraft();   // refresh reopens the same conversation

    syncSidebarAccess();
    setView("chat");
    renderConversation();
    renderLists();
    chatInput.focus({ preventScroll: true });
    autosize();
  })();

  // --- Auto-fetch live updates from the college's real website on page load ---
  (async function loadLiveUpdates() {
    const panel = $("live-panel"), panelText = $("live-panel-text");
    if (!panel || !panelText) return;
    try {
      const res = await fetch("/api/live-updates");
      const data = await res.json();
      if (data.available && data.snippet) {
        panelText.textContent = data.snippet;
        panel.hidden = false;
      }
    } catch (err) { /* live updates are a bonus - fail silently */ }
  })();
})();
