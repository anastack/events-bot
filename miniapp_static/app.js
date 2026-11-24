const tg = window.Telegram?.WebApp;
const initData = tg?.initData || "";
const headers = initData ? { "X-Telegram-Init-Data": initData } : {};
const sessionId = crypto.randomUUID();
const feed = document.querySelector("#feed");
const filters = document.querySelector("#filters");
const search = document.querySelector("#search");
const details = document.querySelector("#details");
const about = document.querySelector("#about");
const toast = document.querySelector("#toast");

let events = [];
let saved = new Set();
let authenticated = false;
let policy = "discovery_v2";
let listVersion = 0;
let pageObserver;
const pageSize = 16;
let surface = "discover";
let topic = "";
let observer;
let exposures = [];
let activeDialog;
let relatedObserver;
let relatedExposures = [];
let openCardVersion = 0;
let reels;
let toastTimer;
let sending = false;
const queue = [];
const acknowledgements = new Map();
const saving = new Set();

tg?.ready();
if (initData) {
  tg.expand();
  tg.setHeaderColor?.("#100f12");
  tg.setBackgroundColor?.("#100f12");
}

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value != null) node.textContent = value;
  return node;
}

function button(className, value, handler) {
  const node = element("button", className, value);
  node.type = "button";
  node.addEventListener("click", handler);
  return node;
}

function notify(message) {
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 2400);
}

async function flush() {
  if (!queue.length || sending) return;
  sending = true;
  const batch = queue.slice(0, 12);
  let retry = true;
  try {
    const response = await fetch("/api/actions", {
      method: "POST", headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify(batch), keepalive: true,
    });
    if (response.ok) {
      const accepted = new Set((await response.json()).accepted);
      finishBatch(batch, accepted);
      retry = false;
    } else if (response.status < 500) {
      finishBatch(batch, new Set());
      retry = false;
      notify(response.status === 401 ? "Открой афишу заново в Telegram." : "Действие не удалось сохранить.");
    }
  } catch {
    // Keep the same action IDs when retrying.
  } finally {
    sending = false;
    if (queue.length) setTimeout(flush, retry ? 2500 : 0);
  }
}

function finishBatch(batch, accepted) {
  queue.splice(0, batch.length);
  for (const action of batch) {
    acknowledgements.get(action.action_id)?.(accepted.has(action.action_id));
    acknowledgements.delete(action.action_id);
  }
}

function track(exposure, kind, extra = {}, onResult) {
  if (!authenticated || !exposure.id) return;
  const actionId = kind === "impression" ? exposure.id : crypto.randomUUID();
  if (onResult) acknowledgements.set(actionId, onResult);
  queue.push({
    action_id: actionId,
    impression_id: exposure.id,
    session_id: sessionId,
    event_id: exposure.event.id,
    kind,
    occurred_at: new Date().toISOString(),
    position: exposure.position,
    surface: exposure.surface,
    policy: exposure.policy,
    ...extra,
  });
  void flush();
}

function startExposure(exposure) {
  clearTimeout(exposure.timer);
  if (exposure.id) return;
  exposure.id = crypto.randomUUID();
  exposure.started = performance.now();
  track(exposure, "impression", { candidate_ids: exposure.candidates });
}

function endExposure(exposure) {
  clearTimeout(exposure.timer);
  if (!exposure.id || exposure.ended) return;
  exposure.ended = true;
  track(exposure, "view_end", {
    dwell_ms: Math.min(3600000, Math.round(performance.now() - exposure.started)),
  });
}

function stopWatching() {
  observer?.disconnect();
  exposures.forEach(endExposure);
}

function watch() {
  observer?.disconnect();
  observer = null;
  if (document.hidden || activeDialog || !authenticated) return;
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const exposure = exposures[Number(entry.target.dataset.position)];
      if (!exposure || exposure.node !== entry.target) continue;
      if (entry.intersectionRatio >= .5) {
        if (exposure.ended) {
          exposure.id = null;
          exposure.ended = false;
        }
        clearTimeout(exposure.timer);
        exposure.timer = setTimeout(() => {
          if (!document.hidden && !activeDialog) startExposure(exposure);
        }, 500);
      } else {
        endExposure(exposure);
      }
    }
  }, { threshold: .5, rootMargin: "0px 0px -62px 0px" });
  exposures.forEach(exposure => observer.observe(exposure.node));
}

function dateBadge(event) {
  const date = new Date(event.date_sort + "T12:00:00");
  return new Intl.DateTimeFormat("ru", { day: "numeric", month: "short" }).format(date);
}

function poster(event) {
  const cover = element("div", "poster v" + event.id % 5);
  if (event.photo) {
    const image = element("img");
    image.src = event.photo;
    image.alt = "";
    image.referrerPolicy = "no-referrer";
    image.loading = "lazy";
    image.addEventListener("error", () => image.remove(), { once: true });
    cover.append(image);
  }
  cover.append(
    element("span", "date-tile", dateBadge(event)),
    element("span", "poster-word", event.type || event.topic || "Событие"),
  );
  return cover;
}

function updateHeart(node, id, inDetails) {
  const selected = saved.has(id);
  node.textContent = inDetails ? (selected ? "Сохранено" : "Сохранить") : (selected ? "♥" : "♡");
  node.setAttribute("aria-pressed", String(selected));
  node.setAttribute("aria-label", selected ? "Убрать из сохранённых" : "Сохранить мероприятие");
}

function save(exposure) {
  if (!authenticated) return notify("Сохранять события можно в Telegram.");
  startExposure(exposure);
  const id = exposure.event.id;
  if (saving.has(id)) return;
  saving.add(id);
  const remove = saved.has(id);
  track(exposure, remove ? "unsave" : "save", {}, accepted => {
    saving.delete(id);
    if (!accepted) return notify("Действие не удалось сохранить.");
    if (remove) saved.delete(id);
    else saved.add(id);
    notify(remove ? "Убрано из сохранённых" : "Сохранено");
    document.querySelectorAll(`[data-save="${id}"]`).forEach(node =>
      updateHeart(node, id, node.classList.contains("cta"))
    );
    if (surface === "saved" && !activeDialog) render();
  });
}

function saveButton(exposure, inDetails = false) {
  const node = button(inDetails ? "cta" : "heart", "", () => save(exposure));
  node.dataset.save = exposure.event.id;
  updateHeart(node, exposure.event.id, inDetails);
  return node;
}

function showDialog(dialog) {
  activeDialog = dialog;
  stopWatching();
  if (!dialog.open) dialog.showModal();
  tg?.BackButton?.show();
}

function stopRelated() {
  relatedObserver?.disconnect();
  relatedExposures.forEach(endExposure);
  relatedExposures = [];
}

async function loadRelated(event, sheet, version) {
  try {
    const response = await fetch("/api/events/" + event.id + "/related?session=" + sessionId, { headers });
    if (!response.ok) return;
    const data = await response.json();
    if (version !== openCardVersion || !details.open || !data.events.length) return;
    const section = element("section", "related");
    section.append(element("h3", "", "Вам также могут понравиться"));
    const grid = element("div", "related-grid");
    const candidates = data.events.map(item => item.id);
    relatedExposures = data.events.map((item, position) => ({
      event: item, position, surface: "related", policy: data.policy, candidates,
    }));
    relatedExposures.forEach(view => grid.append(card(view, false)));
    section.append(grid);
    sheet.append(section);
    if (authenticated) {
      relatedObserver = new IntersectionObserver(entries => {
        for (const entry of entries) {
          const view = relatedExposures.find(view => view.node === entry.target);
          if (!view) continue;
          if (entry.intersectionRatio >= .5 && !document.hidden) {
            if (view.ended) { view.id = null; view.ended = false; }
            clearTimeout(view.timer);
            view.timer = setTimeout(() => {
              if (details.open && !document.hidden) startExposure(view);
            }, 500);
          } else endExposure(view);
        }
      }, { root: details, threshold: .5 });
      relatedExposures.forEach(view => relatedObserver.observe(view.node));
    }
  } catch {
    // The event card remains available if related events cannot load.
  }
}

async function loadDescription(event, paragraph, version) {
  if ((event.description || "").length >= 100) return;
  try {
    const response = await fetch("/api/events/" + event.id + "/details");
    if (!response.ok) return;
    const data = await response.json();
    if (version !== openCardVersion || !details.open) return;
    if (data.description) {
      event.description = data.description;
      paragraph.textContent = data.description;
    } else {
      paragraph.textContent = "Подробности и условия участия — на странице организатора.";
    }
  } catch {
    if (version === openCardVersion && !event.description) {
      paragraph.textContent = "Подробности и условия участия — на странице организатора.";
    }
  }
}

function stopReels() {
  if (!reels) return;
  reels.observer.disconnect();
  if (reels.active?.exposure) endExposure(reels.active.exposure);
  reels = null;
  document.body.classList.remove("reels-open");
  stopRelated();
}

async function waitForActions() {
  const until = performance.now() + 1800;
  void flush();
  while ((queue.length || sending) && performance.now() < until) {
    await new Promise(resolve => setTimeout(resolve, 30));
  }
}

function reelPage(state, event) {
  const node = element("section", "reel");
  node.setAttribute("aria-label", event?.name || "Следующая карточка");
  const view = { node, event, exposure: null, loading: false };
  state.pages.push(view);
  state.container.append(node);
  if (event) fillReel(state, view, event);
  else node.append(element("p", "reel-loading", "Подбираем следующее событие…"));
  state.observer.observe(node);
  return view;
}

function fillReel(state, view, event) {
  view.event = event;
  view.node.setAttribute("aria-label", event.name);
  const exposure = {
    event, position: state.pages.indexOf(view), surface: "reels",
    policy: state.policy, candidates: state.candidates,
  };
  view.exposure = exposure;
  const copy = element("div", "sheet-text reel-copy");
  copy.append(
    element("h2", "", event.name),
    element("p", "event-meta", [event.date_display, event.location].filter(Boolean).join(" · ")),
  );
  const description = element("p", "description reel-description",
    event.description || "Загружаем описание…");
  const expand = button("reel-expand", "Подробнее", () => {
    const expanded = description.classList.toggle("expanded");
    expand.textContent = expanded ? "Свернуть" : "Подробнее";
    expand.setAttribute("aria-expanded", String(expanded));
    if (expanded) track(exposure, "open");
  });
  expand.setAttribute("aria-expanded", "false");
  copy.append(description, expand);
  const actions = element("div", "reel-actions");
  actions.append(saveButton(exposure, true));
  if (event.link) {
    const link = element("a", "cta primary", "К организатору");
    link.href = event.link;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.addEventListener("click", click => {
      track(exposure, "outbound_click");
      if (initData && tg?.openLink) {
        click.preventDefault();
        tg.openLink(event.link);
      }
    });
    actions.append(link);
  }
  copy.append(actions);
  const similar = button("reel-expand", "Вам также могут понравиться", () => {
    similar.disabled = true;
    void loadRelated(event, copy, openCardVersion);
  });
  copy.append(similar);
  const cover = poster(event);
  cover.classList.add("reel-poster");
  view.node.replaceChildren(cover, copy);
  view.description = description;
  view.copy = copy;
}

async function activateReel(state, view) {
  if (reels !== state || state.active === view || document.hidden) return;
  if (state.active?.exposure) endExposure(state.active.exposure);
  stopRelated();
  ++openCardVersion;
  state.active = view;
  state.previous.disabled = state.pages.indexOf(view) === 0;
  if (!view.event) {
    if (view.loading) return;
    view.loading = true;
    await waitForActions();
    if (reels !== state || state.active !== view) { view.loading = false; return; }
    try {
      const data = await getFeed(AbortSignal.timeout(6500));
      if (reels !== state || state.active !== view) { view.loading = false; return; }
      events = data.events;
      state.policy = data.policy;
      state.candidates = events.map(event => event.id);
    } catch {
      if (reels !== state || state.active !== view) { view.loading = false; return; }
    }
    const event = events.find(event => !state.seen.has(event.id));
    view.loading = false;
    if (!event) {
      view.node.replaceChildren(element("p", "reel-loading", "Ты посмотрела все доступные события."),
        button("cta reel-end", "Вернуться к афише", () => details.close()));
      state.next.disabled = true;
      return;
    }
    state.seen.add(event.id);
    fillReel(state, view, event);
  }
  if (reels !== state || state.active !== view) return;
  state.next.disabled = false;
  const exposure = view.exposure;
  if (exposure.ended) { exposure.id = null; exposure.ended = false; }
  startExposure(exposure);
  void loadDescription(view.event, view.description, openCardVersion);
  if (state.pages.at(-1) === view && state.pages.length < 360) reelPage(state, null);
  else if (state.pages.length >= 360 && state.pages.at(-1) === view) state.next.disabled = true;
}

function moveReel(direction) {
  if (!reels) return;
  const index = reels.pages.indexOf(reels.active);
  const target = reels.pages[index + direction];
  target?.node.scrollIntoView({ behavior: "smooth", block: "start" });
}

function openEvent(exposure) {
  startExposure(exposure);
  track(exposure, "open");
  stopReels();
  stopRelated();
  ++openCardVersion;
  const body = document.querySelector("#details-body");
  const container = element("div", "reels");
  container.tabIndex = 0;
  container.setAttribute("aria-label", "Персональная лента мероприятий");
  const previous = button("reel-nav", "↑", () => moveReel(-1));
  const next = button("reel-nav", "↓", () => moveReel(1));
  previous.setAttribute("aria-label", "Предыдущее мероприятие");
  next.setAttribute("aria-label", "Следующее мероприятие");
  const close = button("close reel-close", "×", () => details.close());
  close.setAttribute("aria-label", "Закрыть ленту");
  const toolbar = element("div", "reel-toolbar");
  toolbar.append(element("span", "reel-label", "Для тебя"), previous, next, close);
  const state = {container, previous, next, pages: [], seen: new Set([exposure.event.id]),
    active: null, policy, candidates: events.map(event => event.id)};
  state.observer = new IntersectionObserver(entries => {
    const visible = entries.filter(entry => entry.isIntersecting && entry.intersectionRatio >= .65)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    const view = state.pages.find(view => view.node === visible.target);
    if (view) void activateReel(state, view);
  }, {root: container, threshold: [.65, .9]});
  reels = state;
  body.replaceChildren(container, toolbar);
  showDialog(details);
  document.body.classList.add("reels-open");
  const first = reelPage(state, exposure.event);
  reelPage(state, null);
  void activateReel(state, first);
  container.addEventListener("keydown", event => {
    if (event.target !== container) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      moveReel(event.key === "ArrowDown" ? 1 : -1);
    }
  });
  container.focus({preventScroll: true});
}

function card(exposure, featured) {
  const event = exposure.event;
  const node = element("article", featured ? "event featured" : "event");
  const open = button("event-open", "", () => openEvent(exposure));
  const info = element("div", "event-info");
  info.append(
    element("span", "event-name", event.name),
    element("span", "event-meta", [event.date_display, event.location].filter(Boolean).join(" · ")),
  );
  open.append(poster(event), info);
  node.append(open, saveButton(exposure));
  node.dataset.position = exposure.position;
  exposure.node = node;
  return node;
}

function matchingEvents() {
  const query = search.value.trim().toLocaleLowerCase("ru");
  return events.filter(event =>
    (surface !== "saved" || saved.has(event.id)) &&
    (!topic || event.topic === topic) &&
    (!query || [event.name, event.location, event.topic, event.type].join(" ").toLocaleLowerCase("ru").includes(query))
  );
}

async function getFeed(signal) {
  const response = await fetch("/api/feed?session=" + sessionId, { headers, signal });
  if (!response.ok) {
    throw new Error(response.status === 401 ? "Открой афишу заново в Telegram." : "Не удалось загрузить афишу.");
  }
  return response.json();
}

function render() {
  stopWatching();
  pageObserver?.disconnect();
  pageObserver = null;
  const version = ++listVersion;
  exposures = [];
  const initial = matchingEvents();
  if (!initial.length) {
    feed.replaceChildren(element("p", "empty", search.value.trim() || topic ? "По этому запросу ничего не нашлось." :
      surface === "saved" ? "Здесь будут мероприятия, которые ты сохранишь." : "Ближайших анонсов пока нет."));
    return;
  }

  const grid = element("div", "grid");
  const shown = new Set();
  const more = button("chip load-more", "Показать ещё", async () => {
    if (more.disabled) return;
    more.disabled = true;
    pageObserver?.disconnect();
    if (authenticated && surface === "discover") {
      try {
        const data = await getFeed();
        if (version !== listVersion) return;
        events = data.events;
        policy = data.policy;
      } catch {
        // The existing list still works if refreshing the order fails.
      }
    }
    if (version !== listVersion) return;
    appendPage(matchingEvents());
    more.disabled = false;
  });
  const appendPage = (filtered) => {
    const remaining = filtered.filter(event => !shown.has(event.id));
    const candidates = [...shown, ...remaining.map(event => event.id)].slice(0, 360);
    for (const event of remaining.slice(0, pageSize)) {
      const exposure = { event, position: exposures.length, surface, candidates, policy };
      if (exposure.position >= 360) break;
      shown.add(event.id);
      exposures.push(exposure);
      const node = card(exposure, exposure.position === 0 && surface === "discover");
      grid.append(node);
      if (!document.hidden && !activeDialog && authenticated) observer?.observe(node);
    }
    more.hidden = remaining.length <= pageSize || exposures.length >= 360;
    if (!more.hidden && pageObserver) pageObserver.observe(more);
  };
  feed.replaceChildren(grid, more);
  if (typeof IntersectionObserver === "function" && surface === "discover") {
    pageObserver = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) more.click();
    }, { rootMargin: "180px" });
  }
  appendPage(initial);
  watch();
}

function renderFilters() {
  const topics = ["", ...new Set(events.map(event => event.topic).filter(Boolean))];
  filters.replaceChildren(...topics.map(value => {
    const chip = button("chip", value || "Все события", () => {
      topic = value;
      renderFilters();
      render();
    });
    chip.setAttribute("aria-pressed", String(topic === value));
    return chip;
  }));
}

async function load() {
  feed.setAttribute("aria-busy", "true");
  try {
    const data = await getFeed();
    policy = data.policy;
    events = data.events;
    saved = new Set(data.saved);
    authenticated = data.authenticated;
    document.querySelector("#notice").hidden = authenticated;
    renderFilters();
    render();
  } catch (error) {
    const message = element("div", "empty", error.message);
    message.append(document.createElement("br"), button("chip", "Повторить", load));
    feed.replaceChildren(message);
  } finally {
    feed.setAttribute("aria-busy", "false");
  }
}

document.querySelectorAll("[data-tab]").forEach(tab => tab.addEventListener("click", () => {
  surface = tab.dataset.tab;
  topic = "";
  search.value = "";
  document.querySelectorAll("[data-tab]").forEach(node => node.removeAttribute("aria-current"));
  tab.setAttribute("aria-current", "page");
  const title = document.querySelector("#page-title");
  title.textContent = surface === "saved" ? "Сохранённые" : "Куда пойти";
  if (surface === "discover") title.append(element("span", "", "?"));
  renderFilters();
  render();
  window.scrollTo(0, 0);
}));
search.addEventListener("input", render);
document.querySelector("#info-button").addEventListener("click", () => showDialog(about));
about.querySelector("[data-close]").addEventListener("click", () => about.close());
[details, about].forEach(dialog => dialog.addEventListener("close", () => {
  activeDialog = null;
  if (dialog === details) {
    ++openCardVersion;
    stopReels();
    stopRelated();
  }
  tg?.BackButton?.hide();
  if (surface === "saved") render();
  else watch();
}));
tg?.BackButton?.onClick(() => activeDialog?.close());
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopWatching();
    if (reels?.active?.exposure) endExposure(reels.active.exposure);
    relatedExposures.forEach(endExposure);
  } else if (details.open) {
    if (reels?.active?.exposure) {
      const view = reels.active.exposure;
      if (view.ended) { view.id = null; view.ended = false; }
      startExposure(view);
    }
    relatedExposures.forEach(view => {
      if (view.ended) { view.id = null; view.ended = false; }
      relatedObserver?.unobserve(view.node);
      relatedObserver?.observe(view.node);
    });
  } else watch();
});
window.addEventListener("pagehide", () => {
  stopWatching();
  stopReels();
  stopRelated();
  void flush();
});
void load();
