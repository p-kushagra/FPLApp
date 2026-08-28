"""Fetch news items from free RSS feeds and Reddit JSON endpoints.

Fetchers never raise: they return (items, errors) so ingestion can report which
sources failed instead of silently losing them.
"""
from __future__ import annotations

import datetime as dt
import re

import feedparser
import requests

# Some publishers reject non-browser agents; this UA is honest but accepted.
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 fpl-squad-assistant/1.0"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, application/json;q=0.9, */*;q=0.8",
}


def _strip_html(text: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", text or "", flags=re.DOTALL | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&nbsp;", " ")
                .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", text).strip()


def _iso(ts_struct) -> str | None:
    if not ts_struct:
        return None
    try:
        return dt.datetime(*ts_struct[:6]).isoformat()
    except (TypeError, ValueError):
        return None


def _normalize_entry(entry, source: str) -> dict:
    body = entry.get("summary", "")
    if entry.get("content"):
        body = entry["content"][0].get("value", body)
    return {
        "source": source,
        "url": entry.get("link", ""),
        "title": _strip_html(entry.get("title", "")),
        "published_at": _iso(entry.get("published_parsed") or entry.get("updated_parsed")),
        "body": _strip_html(body),
    }


def fetch_rss(feeds: list[str], timeout: int = 25) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    errors: list[str] = []
    for url in feeds:
        try:
            resp = requests.get(url, headers=UA, timeout=timeout)
            if resp.status_code != 200:
                errors.append(f"{url} -> HTTP {resp.status_code}")
                continue
            parsed = feedparser.parse(resp.content)
        except requests.RequestException as exc:
            errors.append(f"{url} -> {type(exc).__name__}")
            continue

        if not parsed.entries:
            errors.append(f"{url} -> no entries")
            continue

        source = parsed.feed.get("title") or url
        for entry in parsed.entries:
            item = _normalize_entry(entry, source)
            if item["url"] and (item["title"] or item["body"]):
                items.append(item)
    return items, errors


def fetch_reddit(subreddits: list[str], limit: int = 50,
                 timeout: int = 25) -> tuple[list[dict], list[str]]:
    """Best-effort. Reddit often returns 403 to non-OAuth clients; that is reported
    as a warning rather than treated as a hard failure."""
    items: list[dict] = []
    errors: list[str] = []
    for sub in subreddits:
        fetched = False
        for host in ("https://www.reddit.com", "https://old.reddit.com"):
            try:
                resp = requests.get(f"{host}/r/{sub}/new.json?limit={limit}",
                                    headers=UA, timeout=timeout)
            except requests.RequestException as exc:
                errors.append(f"r/{sub} -> {type(exc).__name__}")
                continue
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except ValueError:
                continue
            for child in data.get("data", {}).get("children", []):
                d = child.get("data", {})
                created = d.get("created_utc")
                published = (dt.datetime.utcfromtimestamp(created).isoformat()
                             if created else None)
                title = d.get("title", "")
                body = (d.get("selftext") or "").strip()
                items.append({
                    "source": f"r/{sub}",
                    "url": "https://www.reddit.com" + d.get("permalink", ""),
                    "title": title,
                    "published_at": published,
                    "body": body or title,
                })
            fetched = True
            break
        if not fetched:
            errors.append(f"r/{sub} -> blocked (Reddit 403); RSS sources still used")
    return items, errors
