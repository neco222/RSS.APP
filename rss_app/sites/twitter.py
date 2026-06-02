from __future__ import annotations

import datetime as dt
import html
import re
import typing as t
import urllib.parse
import xml.etree.ElementTree as ET

from rss_app.core import (
    build_rss_xml,
    fmt_human,
    fmt_rss,
    parse_atom_datetime,
    parse_rss_datetime,
    to_target,
)
from rss_app.sites.base import HandlerContext, HandlerResult, SiteHandler


class TwitterHandler(SiteHandler):
    name = "twitter"
    aliases = ("x", "twitter_rss", "x_rss")
    uses_page_hash = False
    requires_pre_fetch = False

    def matches(self, ctx: HandlerContext) -> bool:
        return _username_from_url(ctx.url) is not None

    def build(self, ctx: HandlerContext) -> HandlerResult:
        source_url = _source_url(ctx)
        body, _headers = ctx.fetch(source_url, ctx.user_agent)
        feed_items = _parse_feed_items(body)
        changed, matched_count = _update_archive(ctx, feed_items)

        if not changed:
            return HandlerResult(
                content=None,
                message=f"[info] no new qualifying X posts: {ctx.name} ({matched_count} matched)",
            )

        items = _archive_items(ctx)
        limit = ctx.rss_limit()
        out_items = items[:limit] if limit > 0 else items
        return HandlerResult(
            content=build_rss_xml(
                ctx.name,
                ctx.url,
                out_items,
                ctx.now_local,
                rss_time_tz=ctx.rss_time_tz,
            ),
            commit_message=f"feat: update RSS(twitter) for {ctx.name} at {fmt_human(ctx.now_local)}",
            log_label=f"twitter matched={matched_count} total={len(items)}",
        )


def _source_url(ctx: HandlerContext) -> str:
    rss_url = str(ctx.site.get("rss_url") or ctx.site.get("source_url") or "").strip()
    if rss_url:
        return rss_url

    username = str(ctx.site.get("username") or "").strip().lstrip("@")
    username = username or _username_from_url(ctx.url) or ""
    if not username:
        raise RuntimeError("twitter handler requires site.rss_url or an x.com profile URL")

    base = str(ctx.site.get("rsshub_base_url") or "https://rsshub.app").rstrip("/")
    return f"{base}/twitter/user/{urllib.parse.quote(username)}"


def _parse_feed_items(body: bytes) -> list[dict[str, str]]:
    text = body.decode("utf-8", errors="ignore")
    try:
        root = ET.fromstring(text)
    except Exception as e:
        raise RuntimeError("twitter RSS source did not return valid XML") from e

    if _strip_ns(root.tag).lower() == "rss":
        channel = _first_child(root, "channel")
        if channel is None:
            return []
        return [_parse_rss_item(item) for item in _children(channel, "item")]

    if _strip_ns(root.tag).lower() == "feed":
        return [_parse_atom_entry(entry) for entry in _children(root, "entry")]

    return []


def _parse_rss_item(item: ET.Element) -> dict[str, str]:
    title = _text(_first_child(item, "title"))
    link = _text(_first_child(item, "link"))
    description = _text(_first_child(item, "description"))
    guid = _text(_first_child(item, "guid")) or link
    pub_raw = _text(_first_child(item, "pubDate"))
    created = parse_rss_datetime(pub_raw) if pub_raw else None
    return {
        "title": title or link or guid,
        "link": link or guid,
        "guid": guid or link,
        "description": description,
        "pubDate": pub_raw,
        "_created_at": _created_iso(created),
        "_metric_text": f"{title}\n{description}",
    }


def _parse_atom_entry(entry: ET.Element) -> dict[str, str]:
    title = _text(_first_child(entry, "title"))
    link = _atom_link(entry)
    summary = _text(_first_child(entry, "summary"))
    content = _text(_first_child(entry, "content"))
    description = content or summary
    guid = _text(_first_child(entry, "id")) or link
    published = _text(_first_child(entry, "published")) or _text(_first_child(entry, "updated"))
    created = parse_atom_datetime(published) if published else None
    return {
        "title": title or link or guid,
        "link": link or guid,
        "guid": guid or link,
        "description": description,
        "pubDate": published,
        "_created_at": _created_iso(created),
        "_metric_text": f"{title}\n{description}",
    }


def _update_archive(ctx: HandlerContext, feed_items: list[dict[str, str]]) -> tuple[bool, int]:
    archive = ctx.entry_state.get("archive")
    if not isinstance(archive, dict):
        archive = {}
        ctx.entry_state["archive"] = archive

    changed = False
    matched_count = 0
    update_existing = _bool(ctx.site.get("update_existing_metrics"), False)

    for item in feed_items:
        enriched = _enrich_metrics(ctx, item)
        if not _passes_filters(ctx, enriched):
            continue
        matched_count += 1

        key = enriched.get("guid") or enriched.get("link")
        if not key:
            continue
        existing = archive.get(key)
        if existing is None:
            archive[key] = enriched
            changed = True
        elif update_existing and _public_item(existing) != _public_item(enriched):
            archive[key] = enriched
            changed = True

    max_keep = _int(ctx.site.get("archive_max_items"), 2000)
    if max_keep > 0 and len(archive) > max_keep:
        keep_keys = [
            key
            for key, _value in sorted(
                archive.items(),
                key=lambda kv: _created_dt(kv[1]),
                reverse=True,
            )[:max_keep]
        ]
        keep_set = set(keep_keys)
        for key in list(archive.keys()):
            if key not in keep_set:
                archive.pop(key, None)
                changed = True

    return changed, matched_count


def _enrich_metrics(ctx: HandlerContext, item: dict[str, str]) -> dict[str, str]:
    metric_text = _strip_html(item.get("_metric_text") or item.get("description") or "")
    like_count = _extract_metric(ctx, metric_text, "like_count", _DEFAULT_LIKE_PATTERNS)
    retweet_count = _extract_metric(ctx, metric_text, "retweet_count", _DEFAULT_RETWEET_PATTERNS)
    reply_count = _extract_metric(ctx, metric_text, "reply_count", _DEFAULT_REPLY_PATTERNS)
    quote_count = _extract_metric(ctx, metric_text, "quote_count", _DEFAULT_QUOTE_PATTERNS)

    out = dict(item)
    out["_like_count"] = str(like_count)
    out["_retweet_count"] = str(retweet_count)
    out["_reply_count"] = str(reply_count)
    out["_quote_count"] = str(quote_count)
    created = parse_atom_datetime(out.get("_created_at", ""))
    if created:
        out["pubDate"] = fmt_rss(to_target(created, ctx.rss_target(), ctx.timezone), ctx.rss_target())

    metrics_line = (
        f"likes: {like_count} / reposts: {retweet_count} / "
        f"replies: {reply_count} / quotes: {quote_count}"
    )
    description = out.get("description") or ""
    out["description"] = f"{metrics_line}\n\n{description}" if description else metrics_line
    return out


def _passes_filters(ctx: HandlerContext, item: dict[str, str]) -> bool:
    like_count = _int(item.get("_like_count"), 0)
    retweet_count = _int(item.get("_retweet_count"), 0)
    reply_count = _int(item.get("_reply_count"), 0)
    quote_count = _int(item.get("_quote_count"), 0)
    total = like_count + retweet_count + reply_count + quote_count

    if like_count < _int(ctx.site.get("min_like_count"), 0):
        return False
    if retweet_count < _int(ctx.site.get("min_retweet_count"), 0):
        return False
    if reply_count < _int(ctx.site.get("min_reply_count"), 0):
        return False
    if quote_count < _int(ctx.site.get("min_quote_count"), 0):
        return False
    if total < _int(ctx.site.get("min_total_engagement"), 0):
        return False

    haystack = f"{item.get('title', '')}\n{item.get('link', '')}\n{_strip_html(item.get('description', ''))}"
    include = ctx.include_patterns()
    exclude = ctx.exclude_patterns()
    if include and not any(pattern.search(haystack) for pattern in include):
        return False
    if exclude and any(pattern.search(haystack) for pattern in exclude):
        return False
    return True


def _archive_items(ctx: HandlerContext) -> list[dict[str, str]]:
    archive = ctx.entry_state.get("archive")
    if not isinstance(archive, dict):
        return []
    items: list[dict[str, str]] = []
    for _key, value in sorted(archive.items(), key=lambda kv: _created_dt(kv[1]), reverse=True):
        if isinstance(value, dict):
            items.append(_public_item(value))
    return items


def _extract_metric(
    ctx: HandlerContext,
    text: str,
    metric_name: str,
    default_patterns: list[str],
) -> int:
    patterns = _metric_patterns(ctx, metric_name, default_patterns)
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _parse_count(match.group(1))
    return 0


def _metric_patterns(ctx: HandlerContext, metric_name: str, default_patterns: list[str]) -> list[str]:
    custom = ctx.site.get("metrics_regex")
    if isinstance(custom, dict):
        value = custom.get(metric_name)
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value if item]
    return default_patterns


def _parse_count(value: str) -> int:
    compact = value.strip().replace(",", "")
    multiplier = 1
    if compact[-1:].lower() == "k":
        multiplier = 1000
        compact = compact[:-1]
    elif compact[-1:].lower() == "m":
        multiplier = 1000000
        compact = compact[:-1]
    try:
        return int(float(compact) * multiplier)
    except Exception:
        return 0


def _public_item(item: dict) -> dict[str, str]:
    return {str(k): str(v) for k, v in item.items() if not str(k).startswith("_")}


def _created_dt(item: dict) -> dt.datetime:
    parsed = parse_atom_datetime(str(item.get("_created_at") or ""))
    return parsed or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _created_iso(created: dt.datetime | None) -> str:
    if created is None:
        return ""
    return created.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _username_from_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    username = parts[0].lstrip("@")
    if username in {"home", "i", "intent", "search", "share"}:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
        return None
    return username


def _atom_link(entry: ET.Element) -> str:
    for child in entry:
        if _strip_ns(child.tag).lower() == "link":
            href = child.attrib.get("href")
            if href:
                return href
    return ""


def _first_child(parent: ET.Element | None, tag: str) -> ET.Element | None:
    if parent is None:
        return None
    for child in parent:
        if _strip_ns(child.tag).lower() == tag.lower():
            return child
    return None


def _children(parent: ET.Element, tag: str) -> list[ET.Element]:
    return [child for child in parent if _strip_ns(child.tag).lower() == tag.lower()]


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return html.unescape("".join(el.itertext()).strip())


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return html.unescape(" ".join(value.split()))


def _int(value: t.Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _bool(value: t.Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


_COUNT = r"([0-9][0-9,]*(?:\.[0-9]+)?[kKmM]?)"
_DEFAULT_LIKE_PATTERNS = [
    rf"(?:likes?|いいね)[^\d]{{0,16}}{_COUNT}",
    rf"{_COUNT}[^\w]{{0,8}}(?:likes?|いいね)",
]
_DEFAULT_RETWEET_PATTERNS = [
    rf"(?:retweets?|reposts?|リツイート|リポスト|RT)[^\d]{{0,16}}{_COUNT}",
    rf"{_COUNT}[^\w]{{0,8}}(?:retweets?|reposts?|リツイート|リポスト|RT)",
]
_DEFAULT_REPLY_PATTERNS = [
    rf"(?:replies|reply|返信)[^\d]{{0,16}}{_COUNT}",
    rf"{_COUNT}[^\w]{{0,8}}(?:replies|reply|返信)",
]
_DEFAULT_QUOTE_PATTERNS = [
    rf"(?:quotes?|引用)[^\d]{{0,16}}{_COUNT}",
    rf"{_COUNT}[^\w]{{0,8}}(?:quotes?|引用)",
]
