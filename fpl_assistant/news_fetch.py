"""Fetch news items from free RSS feeds and Reddit JSON endpoints."""
from __future__ import annotations

import datetime as dt
import re

import feedparser
import requests

UA = {"User-Agent": "fpl-squad-assistant/1.0 (personal use)"}


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
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


def fetch_rss(feeds: list[str]) -> list[dict]:
    items: list[dict] = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception:
            continue
        source = parsed.feed.get("title", url)
        for entry in parsed.entries:
            item = _normalize_entry(entry, source)
            if item["url"] and (item["title"] or item["body"]):
                items.append(item)
    return items


def fetch_reddit(subreddits: list[str], limit: int = 50) -> list[dict]:
    items: list[dict] = []
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/new.json?limit={limit}"
        try:
            resp = requests.get(url, headers=UA, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            title = d.get("title", "")
            body = (d.get("selftext") or "").strip()
            created = d.get("created_utc")
            published = dt.datetime.utcfromtimestamp(created).isoformat() if created else None
            items.append({
                "source": f"r/{sub}",
                "url": "https://www.reddit.com" + d.get("permalink", ""),
                "title": title,
                "published_at": published,
                "body": body or title,
            })
    return items
