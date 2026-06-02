from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
import re
import typing as t


FetchFunc = t.Callable[[str, t.Optional[str]], tuple[bytes, dict]]


@dataclass
class HandlerContext:
    site: dict
    name: str
    url: str
    body: bytes
    headers: dict
    timezone: dt.tzinfo
    now_local: dt.datetime
    rss_time_tz: str
    site_state: dict
    entry_state: dict
    fetch: FetchFunc
    user_agent: str
    rss_items_default: int

    def rss_target(self) -> str:
        return "local" if self.rss_time_tz == "local" else "utc"

    def rss_limit(self) -> int:
        raw = self.site.get("rss_items", self.rss_items_default)
        try:
            return int(raw) if raw is not None else 0
        except Exception:
            return 0

    def limit_arg(self) -> t.Optional[int]:
        limit = self.rss_limit()
        return None if limit <= 0 else limit

    def include_patterns(self) -> list[re.Pattern[str]]:
        return compile_patterns(self.site.get("include_regex", []))

    def exclude_patterns(self) -> list[re.Pattern[str]]:
        return compile_patterns(self.site.get("exclude_regex", []))


@dataclass
class HandlerResult:
    content: t.Optional[bytes]
    commit_message: t.Optional[str] = None
    log_label: str = "updated"
    message: t.Optional[str] = None


class SiteHandler:
    name = "base"
    aliases: tuple[str, ...] = ()
    uses_page_hash = True
    requires_pre_fetch = True

    def supports(self, value: str) -> bool:
        key = value.strip().lower()
        return key == self.name or key in self.aliases

    def matches(self, ctx: HandlerContext) -> bool:
        raise NotImplementedError

    def build(self, ctx: HandlerContext) -> HandlerResult:
        raise NotImplementedError


def compile_patterns(values: t.Iterable[t.Any]) -> list[re.Pattern[str]]:
    return [re.compile(str(p)) for p in values if p]
