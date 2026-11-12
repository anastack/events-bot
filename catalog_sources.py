"""Public Moscow calendars used by the Mini App."""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

import database

logger = logging.getLogger("uvicorn.error" if __name__ != "__main__" else __name__)
MOSCOW = ZoneInfo("Europe/Moscow")
HSE = "https://www.hse.ru/announcements/moscow/"
WINZAVOD = "https://www.winzavod.ru/calendar/"
GES = "https://ges-2.org/calendar"
MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
DATE = re.compile(r"(\d{1,2}) (" + "|".join(MONTHS) + r")(?: (\d{4}))?")
CLOCK = re.compile(r"\b[0-2]\d:[0-5]\d\b")


@dataclass(frozen=True)
class Event:
    title: str
    start: date
    end: date
    display: str
    location: str
    link: str
    kind: str
    topic: str
    photo: str | None = None
    description: str = ""


def page(url):
    request = Request(url, headers={"User-Agent": "CUDA-IDTI Calendar Reader/1.0"})
    with urlopen(request, timeout=8) as response:
        return BeautifulSoup(response.read(), "html.parser", from_encoding="utf-8")


def text(node):
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def date_text(day):
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def date_matches(value):
    return list(DATE.finditer(value))


def date_from(match, year):
    day, month, explicit_year = match.groups()
    return date(int(explicit_year or year), MONTHS.index(month) + 1, int(day))


def kind_for(title, fallback):
    title = title.casefold()
    for word, kind in (
        ("выстав", "Выставка"), ("концерт", "Концерт"), ("фестивал", "Фестиваль"),
        ("спектакл", "Спектакль"), ("мастер-класс", "Мастер-класс"), ("вебинар", "Вебинар"),
        ("лекци", "Лекция"), ("семинар", "Семинар"), ("экскурс", "Экскурсия"),
    ):
        if word in title:
            return kind
    return fallback


def secure_url(base, value):
    if not value:
        return None
    url = urljoin(base, value)
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname and not parts.username:
        return url
    return None


def hse_events(today):
    for group in page(HSE).select(".ann-cards__group"):
        matches = date_matches(text(group.select_one(".ann-cards__day")))
        if not matches:
            continue
        start = date_from(matches[0], today.year)
        if not matches[0][3] and start < today - timedelta(days=180):
            start = start.replace(year=today.year + 1)
        for card in group.select(".ann-cards__item"):
            title_node = card.select_one(".ann-card__title")
            anchor = title_node.select_one("a[href]") if title_node else None
            title = text(title_node)
            link = secure_url(HSE, anchor["href"]) if anchor else None
            if not title or not link:
                continue
            location = text(card.select_one(".ann-card__tags span.ann-tags__item")) or "НИУ ВШЭ, Москва"
            clock = CLOCK.search(text(card.select_one(".ann-cards__clock")))
            display = date_text(start) + (", " + clock[0] if clock else "")
            kind = kind_for(title, "Встреча")
            topic = "Культура" if kind in {"Выставка", "Концерт", "Фестиваль", "Спектакль"} else "Образование"
            yield Event(title, start, start, display, location, link, kind, topic)


def winzavod_events(today):
    for card in page(WINZAVOD).select("a.item[href]"):
        title = text(card.select_one(".events__name"))
        link = secure_url(WINZAVOD, card["href"])
        label = text(card.select_one(".events__date"))
        matches = date_matches(label)
        kind = text(card.select_one(".events__catergory")) or "Событие"
        if not title or not link or not matches or "подкаст" in title.casefold() or "детские" in kind.casefold():
            continue
        explicit_year = next((int(match[3]) for match in reversed(matches) if match[3]), today.year)
        start = date_from(matches[0], explicit_year)
        end = date_from(matches[-1], explicit_year)
        if start > end and not matches[0][3]:
            start = start.replace(year=end.year - 1)
        image = card.select_one(".events__img img[src]")
        photo = secure_url(WINZAVOD, image["src"]) if image else None
        price = text(card.select_one(".events__cost"))
        yield Event(title, start, end, label, "Винзавод, 4-й Сыромятнический пер., 1/8",
                    link, kind, "Культура", photo, price)


def ges_events(today):
    def get_day(day):
        url = GES + "?" + urlencode({"date": day.isoformat()})
        try:
            return day, page(url).select("a[href][aria-label]")
        except OSError:
            logger.warning("ГЭС-2: календарь за %s недоступен", day)
            return day, []

    seen = set()
    days = [today + timedelta(days=offset) for offset in range(14)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        pages = list(pool.map(get_day, days))
    for day, cards in pages:
        for card in cards:
            label = " ".join(card["aria-label"].split())
            kind, separator, details = label.partition(" - ")
            clock = CLOCK.search(details)
            link = secure_url(GES, card["href"])
            if not separator or not clock or not link or link in seen or "индивидуальный" in details.casefold():
                continue
            title = details[:clock.start()].rstrip(" .")
            if not title:
                continue
            image = card.select_one("img[src]")
            photo = secure_url(GES, image["src"]) if image else None
            seen.add(link)
            yield Event(title, day, day, f"{date_text(day)}, {clock[0]}", "ГЭС-2, Болотная наб., 15",
                        link, kind, "Культура", photo)


SOURCES = {"ВШЭ": hse_events, "Винзавод": winzavod_events, "ГЭС-2": ges_events}


def title_key(title):
    return re.sub(r"[^\w]+", " ", title.casefold().replace("ё", "е")).strip()


def collect(today):
    def get_events(entry):
        name, reader = entry
        try:
            events = [event for event in reader(today)
                      if event.end >= today and event.start <= today + timedelta(days=60)]
        except (OSError, ValueError):
            logger.exception("Календарь %s недоступен", name)
            events = []
        logger.info("Календарь %s: %d актуальных карточек", name, len(events))
        return events

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        batches = list(pool.map(get_events, SOURCES.items()))
    events = {}
    for event in sorted((event for batch in batches for event in batch), key=lambda event: event.start):
        events.setdefault(event.link, event)
    return list(events.values())


def init_catalog(cursor):
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS miniapp_catalog_link
        ON events(link) WHERE status = 'miniapp_catalog'
    """)


def refresh():
    today = datetime.now(MOSCOW).date()
    events = collect(today)
    if not events:
        return
    native = database._fetch(
        "SELECT name, date_sort, link FROM events WHERE status = 'approved' AND date_sort >= ?",
        (today.isoformat(),),
    )
    native_titles = {(title_key(title), start) for title, start, _ in native}
    native_links = {link.rstrip("/") for _, _, link in native if link}
    inserted = 0
    seen = set()
    with database._conn() as connection:
        cursor = connection.cursor()
        for event in events:
            key = (title_key(event.title), event.start.isoformat())
            if key in seen or key in native_titles or event.link.rstrip("/") in native_links:
                continue
            seen.add(key)
            day = max(today, event.start).isoformat()
            cursor.execute(database._ph("""
                INSERT INTO events (name, date_display, date_sort, location, link, description,
                                    photo_file_id, user_id, created_at, event_type, topic, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'miniapp_catalog')
                ON CONFLICT (link) WHERE status = 'miniapp_catalog' DO UPDATE SET
                    name = excluded.name, date_display = excluded.date_display,
                    date_sort = excluded.date_sort, location = excluded.location,
                    description = excluded.description, photo_file_id = excluded.photo_file_id,
                    event_type = excluded.event_type, topic = excluded.topic
            """), (event.title, event.display, day, event.location, event.link, event.description,
                   event.photo, datetime.now(MOSCOW).isoformat(), event.kind, event.topic))
            inserted += 1
    logger.info("Обновлено карточек внешней афиши: %d", inserted)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collect(datetime.now(MOSCOW).date())
