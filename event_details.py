import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from time import monotonic
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bs4 import BeautifulSoup

import database


def allowed_source(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        return (parts.scheme == "https" and parts.port in (None, 443) and not parts.username
                and (host in {"ges-2.org", "www.winzavod.ru", "www.mmdm.ru"}
                     or host == "hse.ru" or host.endswith(".hse.ru")))
    except ValueError:
        return False


class SourceRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, url):
        if not allowed_source(url):
            raise ValueError("Unexpected source redirect")
        return super().redirect_request(request, fp, code, msg, headers, url)


def clean_text(node):
    for unwanted in node.select("script, style, nav, button, form, iframe, footer"):
        unwanted.decompose()
    lines = [line.strip() for line in node.get_text("\n", strip=True).splitlines()]
    return "\n".join(dict.fromkeys(line for line in lines if line))[:8000]


@lru_cache(maxsize=512)
def cached_description(url, cache_period):
    if not allowed_source(url):
        return ""
    request = Request(url, headers={"User-Agent": "CUDA-IDTI Calendar Reader/1.0"})
    with build_opener(SourceRedirect()).open(request, timeout=10) as response:
        soup = BeautifulSoup(response.read(2_000_000), "html.parser")

    host = urlsplit(url).hostname
    selectors = {
        "www.winzavod.ru": ".exhibition-detail-info__right, .event-detail__text, .event__description, .detail-text",
        "www.mmdm.ru": ".ps_block_event_performance_content, .event_description, .event-description, .event_text, .event_detail_description",
        "ges-2.org": "article, [class*='rich-text'], [class*='RichText']",
    }
    selector = selectors.get(host, ".post__text, .js-mediator-article, .b-text, article")
    for node in soup.select(selector):
        value = clean_text(node)
        if len(value) >= 100:
            return value
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                for entry in node.get("@graph", [node]) if isinstance(node, dict) else []:
                    if isinstance(entry, dict) and "Event" in str(entry.get("@type", "")):
                        description = entry.get("description", "")
                        if isinstance(description, str) and len(description) >= 80:
                            return clean_text(BeautifulSoup(description, "html.parser"))
        except (ValueError, TypeError):
            continue
    if host == "ges-2.org":
        script = soup.select_one("#__NEXT_DATA__")
        if script:
            try:
                state = json.loads(script.string)["props"]["pageProps"]["__APOLLO_STATE__"]
                for key, node in state.items():
                    if key.startswith("Material:") and isinstance(node, dict):
                        translation = node.get("translation") or node
                        for field in ("content", "description", "text", "body", "lead"):
                            value = translation.get(field)
                            if isinstance(value, str) and len(value) > 100:
                                return clean_text(BeautifulSoup(value, "html.parser"))
            except (KeyError, ValueError, TypeError):
                pass
    for selector in ('meta[property="og:description"]', 'meta[name="description"]'):
        node = soup.select_one(selector)
        value = (node.get("content") or "").strip() if node else ""
        if len(value) >= 60:
            return clean_text(BeautifulSoup(value, "html.parser"))
    raise ValueError("Source description unavailable")


def source_description(url, cache_period):
    try:
        return cached_description(url, cache_period)
    except (OSError, ValueError):
        logging.getLogger("uvicorn.error").warning("Описание недоступно: %s", urlsplit(url).hostname)
        return ""


def description_for(event_id):
    row = database._fetchone(
        "SELECT description, link, status FROM events WHERE id = ? AND status IN ('approved', 'miniapp_catalog')",
        (event_id,),
    )
    if not row:
        return None
    current, link, status = row
    if status != "miniapp_catalog" or len(current or "") >= 100:
        return current or ""
    description = source_description(link, int(monotonic() // 21600))
    if description:
        with database._conn() as connection:
            connection.cursor().execute(database._ph(
                "UPDATE events SET description = ? WHERE id = ? AND status = 'miniapp_catalog' "
                "AND (description IS NULL OR length(description) < 100)"
            ), (description, event_id))
    return description or current or ""


def enrich_catalog():
    rows = database._fetch(
        "SELECT id FROM events WHERE status = 'miniapp_catalog' "
        "AND date_sort >= ? AND (description IS NULL OR length(description) < 100) "
        "ORDER BY date_sort, id LIMIT 360",
        (datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat(),),
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        return sum(bool(value and len(value) >= 100) for value in pool.map(description_for, [r[0] for r in rows]))
