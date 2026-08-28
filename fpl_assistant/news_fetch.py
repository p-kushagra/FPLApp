"""Fetch news items from free RSS feeds.

The fetcher never raises: it returns (items, errors) so ingestion can report which
sources failed instead of silently losing them.

Source names come from `config/sources.yaml` rather than the feed's own <title>.
Publishers get this wrong often enough to matter — Fantasy Football Scout ships an
empty title, Metro ships one containing a replacement character — and either way
the name ends up blank or garbled in the UI.
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlparse

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


def _domain_name(url: str) -> str:
    """A readable fallback name, e.g. 'www.football.london' -> 'Football London'."""
    host = urlparse(url).netloc.removeprefix("www.")
    parts = [p for p in host.split(".") if p not in ("com", "co", "uk", "net", "org")]
    return " ".join(p.replace("-", " ") for p in parts).title() or url


def clean_source_name(value: str) -> str:
    """Drop replacement characters and control junk publishers leave in feed titles.

    Metro's feed title arrives with a replacement character where its dash should
    be — the character did not survive the publisher's own encoding. Stripping it
    here is what stops that reaching the UI as a garbled source name.
    """
    cleaned = re.sub(r"[^\w\s&'.,:!?()/-]+", " ", value or "")
    cleaned = re.sub(r"\s*[-–—|]\s*$", "", cleaned.strip())
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_sources(entries: list) -> list[dict]:
    """Accept either `{name, url, tier}` mappings or bare URL strings."""
    out: list[dict] = []
    for entry in entries or []:
        if isinstance(entry, str):
            out.append({"name": None, "url": entry, "tier": None})
        elif isinstance(entry, dict) and entry.get("url"):
            out.append({
                "name": clean_source_name(entry.get("name") or "") or None,
                "url": entry["url"],
                "tier": entry.get("tier"),
            })
    return out


def fetch_rss(feeds: list, timeout: int = 25) -> tuple[list[dict], list[str]]:
    """Fetch every configured feed. Returns (items, errors)."""
    items: list[dict] = []
    errors: list[str] = []
    for feed in normalize_sources(feeds):
        url = feed["url"]
        try:
            resp = requests.get(url, headers=UA, timeout=timeout)
            if resp.status_code != 200:
                errors.append(f"{feed['name'] or url} -> HTTP {resp.status_code}")
                continue
            parsed = feedparser.parse(resp.content)
        except requests.RequestException as exc:
            errors.append(f"{feed['name'] or url} -> {type(exc).__name__}")
            continue

        if not parsed.entries:
            errors.append(f"{feed['name'] or url} -> no entries")
            continue

        # Configured name wins; a usable feed title is the fallback; then the domain.
        source = (feed["name"] or clean_source_name(parsed.feed.get("title") or "")
                  or _domain_name(url))
        for entry in parsed.entries:
            item = _normalize_entry(entry, source)
            if item["url"] and (item["title"] or item["body"]):
                items.append(item)
    return items, errors


def probe_sources(feeds: list, timeout: int = 15) -> list[dict]:
    """Health-check every feed without ingesting: status, item count, newest item.

    Powers the source panel on the Refresh Config page so a feed that quietly dies
    or goes stale is visible rather than just producing less news.
    """
    out: list[dict] = []
    for feed in normalize_sources(feeds):
        url = feed["url"]
        name = feed["name"] or _domain_name(url)
        row = {"name": name, "url": url, "tier": feed["tier"],
               "ok": False, "items": 0, "newest": None, "age_days": None, "note": ""}
        try:
            resp = requests.get(url, headers=UA, timeout=timeout)
        except requests.RequestException as exc:
            row["note"] = type(exc).__name__
            out.append(row)
            continue

        if resp.status_code != 200:
            row["note"] = f"HTTP {resp.status_code}"
            out.append(row)
            continue

        parsed = feedparser.parse(resp.content)
        row["items"] = len(parsed.entries)
        if not parsed.entries:
            row["note"] = "no entries"
            out.append(row)
            continue

        newest = None
        for entry in parsed.entries:
            ts = entry.get("published_parsed") or entry.get("updated_parsed")
            if ts:
                try:
                    when = dt.datetime(*ts[:6])
                except (TypeError, ValueError):
                    continue
                newest = when if newest is None or when > newest else newest

        row["ok"] = True
        if newest:
            row["newest"] = newest.isoformat(timespec="minutes")
            row["age_days"] = max(0, (dt.datetime.utcnow() - newest).days)
            if row["age_days"] > 14:
                row["note"] = f"stale — newest item {row['age_days']}d old"
        else:
            row["note"] = "items carry no dates"
        out.append(row)
    return out
