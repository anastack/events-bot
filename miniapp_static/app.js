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
let surface = "discover";
let topic = "";
let observer;
let exposures = [];
let activeDialog;
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
  const batch = queue.slice(0, 30);
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
  dialog.showModal();
  tg?.BackButton?.show();
}

function openEvent(exposure) {
  startExposure(exposure);
  track(exposure, "open");
  // A later rerender can reuse the card state; keep this impression for the sheet.
  const view = { ...exposure };
  const event = view.event;
  const body = document.querySelector("#details-body");
  const sheet = element("div", "sheet-text");
  sheet.append(
    element("h2", "", event.name),
    element("p", "event-meta", [event.date_display, event.location].filter(Boolean).join(" · ")),
  );
  if (event.description) sheet.append(element("p", "description", event.description));
  if (event.link) {
    const link = element("a", "cta primary", "К организатору ↗");
    link.href = event.link;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.addEventListener("click", (click) => {
      track(view, "outbound_click");
      if (initData && tg?.openLink) {
        click.preventDefault();
        tg.openLink(event.link);
      }
    });
    sheet.append(link);
  }
  sheet.append(saveButton(view, true));
  const close = button("close", "×", () => details.close());
  close.setAttribute("aria-label", "Закрыть");
  body.replaceChildren(close, poster(event), sheet);
  showDialog(details);
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

function render() {
  stopWatching();
  const query = search.value.trim().toLocaleLowerCase("ru");
  const filtered = events.filter(event =>
    (surface !== "saved" || saved.has(event.id)) &&
    (!topic || event.topic === topic) &&
    (!query || [event.name, event.location, event.topic, event.type].join(" ").toLocaleLowerCase("ru").includes(query))
  );
  const candidates = filtered.map(event => event.id);
  exposures = filtered.map((event, position) => ({ event, position, surface, candidates }));
  const grid = element("div", "grid");
  exposures.forEach((exposure, index) => grid.append(card(exposure, index === 0 && surface === "discover")));
  if (exposures.length) feed.replaceChildren(grid);
  else feed.replaceChildren(element("p", "empty", query || topic ? "По этому запросу ничего не нашлось." :
    surface === "saved" ? "Здесь будут мероприятия, которые ты сохранишь." : "Ближайших анонсов пока нет."));
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
    const response = await fetch("/api/feed", { headers });
    if (!response.ok) throw new Error(response.status === 401 ? "Открой афишу заново в Telegram." : "Не удалось загрузить афишу.");
    const data = await response.json();
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
  tg?.BackButton?.hide();
  if (surface === "saved") render();
  else watch();
}));
tg?.BackButton?.onClick(() => activeDialog?.close());
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopWatching();
  else watch();
});
window.addEventListener("pagehide", () => {
  stopWatching();
  void flush();
});
void load();
