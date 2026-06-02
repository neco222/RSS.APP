from __future__ import annotations

import datetime as dt
import json
import typing as t
import urllib.parse

from rss_app.core import (
    build_rss_xml,
    fmt_human,
    fmt_rss,
    parse_atom_datetime,
    sniff_json_type,
    to_target,
)
from rss_app.sites.base import HandlerContext, HandlerResult, SiteHandler


class GitHubSearchHandler(SiteHandler):
    name = "github_search"
    aliases = ("github", "json", "github_search_json")
    uses_page_hash = False

    def matches(self, ctx: HandlerContext) -> bool:
        return sniff_json_type(ctx.body, ctx.headers) == "json"

    def build(self, ctx: HandlerContext) -> HandlerResult:
        try:
            max_keep = int(ctx.site.get("archive_max_items", 2000))
        except Exception:
            max_keep = 2000

        rss_items, new_count, archive_changed = update_archive(
            body=ctx.body,
            tz=ctx.timezone,
            rss_time_tz=ctx.rss_time_tz,
            entry_state=ctx.entry_state,
            include=ctx.include_patterns() or None,
            exclude=ctx.exclude_patterns() or None,
            max_keep=max_keep,
        )
        if not archive_changed:
            return HandlerResult(
                content=None,
                message=f"[info] no new items (json): {ctx.name}",
            )

        limit = ctx.rss_limit()
        out_items = rss_items[:limit] if limit > 0 else rss_items
        return HandlerResult(
            content=build_rss_xml(
                ctx.name,
                ctx.url,
                out_items,
                ctx.now_local,
                rss_time_tz=ctx.rss_time_tz,
            ),
            commit_message=f"feat: update RSS(json) for {ctx.name} at {fmt_human(ctx.now_local)}",
            log_label=f"json new={new_count} total={len(rss_items)}",
        )


def update_archive(
    body: bytes,
    tz: dt.tzinfo,
    rss_time_tz: str,
    entry_state: dict,
    include: t.Optional[list],
    exclude: t.Optional[list],
    max_keep: int = 2000,
) -> tuple[list[dict[str, str]], int, bool]:
    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except Exception:
        return [], 0, False

    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return [], 0, False

    parsed_now: list[tuple[dt.datetime, dict[str, str]]] = []
    newest_dt: t.Optional[dt.datetime] = None

    for item in data["items"]:
        if not isinstance(item, dict) or "pull_request" not in item:
            continue

        created_at = item.get("created_at") or ""
        html_url = item.get("html_url") or ""
        title = (item.get("title") or "").strip()
        user = (item.get("user") or {}).get("login") if isinstance(item.get("user"), dict) else None

        if not html_url:
            continue
        if include and not any(p.search(html_url) for p in include):
            continue
        if exclude and any(p.search(html_url) for p in exclude):
            continue

        created_dt = parse_atom_datetime(created_at)
        if not created_dt:
            continue

        if newest_dt is None or created_dt > newest_dt:
            newest_dt = created_dt

        target = "local" if rss_time_tz == "local" else "utc"
        pub = fmt_rss(to_target(created_dt, target, tz), target)

        desc_parts: list[str] = []
        if user:
            desc_parts.append(f"author: {user}")
        try:
            parts = urllib.parse.urlparse(html_url).path.strip("/").split("/")
            if len(parts) >= 4:
                desc_parts.append(f"{parts[0]}/{parts[1]}#{parts[3]}")
        except Exception:
            pass

        created_iso = created_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        parsed_now.append(
            (
                created_dt,
                {
                    "title": title or html_url,
                    "link": html_url,
                    "guid": html_url,
                    "pubDate": pub,
                    "description": " / ".join(desc_parts),
                    "_created_at": created_iso,
                },
            )
        )

    archive = entry_state.get("archive")
    if not isinstance(archive, dict):
        archive = {}
        entry_state["archive"] = archive

    changed = False
    new_count = 0
    for _created_dt, item in parsed_now:
        key = item.get("link", "")
        if key and key not in archive:
            archive[key] = item
            changed = True
            new_count += 1

    if newest_dt is not None:
        newest_s = newest_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        if entry_state.get("last_created_at") != newest_s:
            entry_state["last_created_at"] = newest_s

    if max_keep > 0 and len(archive) > max_keep:
        keep_keys = [
            key
            for key, _value in sorted(
                archive.items(),
                key=lambda kv: created_dt_from_item(kv[1]),
                reverse=True,
            )[:max_keep]
        ]
        keep_set = set(keep_keys)
        for key in list(archive.keys()):
            if key not in keep_set:
                archive.pop(key, None)
                changed = True

    rss_items: list[dict[str, str]] = []
    for _key, value in sorted(
        archive.items(),
        key=lambda kv: created_dt_from_item(kv[1]),
        reverse=True,
    ):
        rss_items.append({k: v for k, v in value.items() if not k.startswith("_")})

    return rss_items, new_count, changed


def created_dt_from_item(value: dict) -> dt.datetime:
    d = parse_atom_datetime(value.get("_created_at", ""))
    return d or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

