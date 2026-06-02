from __future__ import annotations

import html
import re
import typing as t
import urllib.parse

from rss_app.core import build_rss_xml, extract_links, fmt_human
from rss_app.sites.base import HandlerContext, HandlerResult, SiteHandler


class BoothHandler(SiteHandler):
    name = "booth"
    aliases = ("booth_pm",)

    def matches(self, ctx: HandlerContext) -> bool:
        return urllib.parse.urlparse(ctx.url).netloc.lower().endswith("booth.pm")

    def build(self, ctx: HandlerContext) -> HandlerResult:
        default_include = [re.compile(r"^https?://(?:www\.)?booth\.pm/(?:ja/)?items/\d+$")]
        include = ctx.include_patterns() or default_include
        links = extract_links(
            ctx.body,
            base_url=ctx.url,
            limit=ctx.limit_arg(),
            include=include,
            exclude=ctx.exclude_patterns() or None,
        )

        items: list[dict[str, str]] = []
        for item in links:
            link = item["link"]
            title, price = self._scrape_item(ctx, link)
            items.append(
                {
                    "title": title or item.get("title") or link,
                    "link": link,
                    "description": price or "price unavailable",
                }
            )

        return HandlerResult(
            content=build_rss_xml(
                ctx.name,
                ctx.url,
                items,
                ctx.now_local,
                rss_time_tz=ctx.rss_time_tz,
            ),
            commit_message=f"feat: update RSS for {ctx.name} at {fmt_human(ctx.now_local)}",
            log_label="generated",
        )

    def _scrape_item(self, ctx: HandlerContext, url: str) -> tuple[str, str]:
        try:
            body, _headers = ctx.fetch(url, ctx.user_agent)
        except Exception:
            return "", ""

        text = body.decode("utf-8", errors="ignore")
        title = (
            self._parse_meta(text, "og:title", "property")
            or self._parse_meta(text, "twitter:title", "name")
            or self._parse_title_fallback(text)
            or ""
        ).strip()
        price = (
            self._parse_meta(text, "product:price:amount", "property")
            or self._parse_price_guess(text)
            or ""
        ).strip()
        return title, price

    @staticmethod
    def _parse_meta(text: str, name: str, attr: str = "property") -> t.Optional[str]:
        pat = re.compile(
            rf'<meta\s+(?:[^>]*?\s)?{attr}\s*=\s*["\']{re.escape(name)}["\']\s+'
            rf'[^>]*?content\s*=\s*["\'](.*?)["\']',
            re.IGNORECASE | re.DOTALL,
        )
        match = pat.search(text)
        return html.unescape(match.group(1).strip()) if match else None

    @staticmethod
    def _parse_title_fallback(text: str) -> t.Optional[str]:
        match = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        return html.unescape(match.group(1).strip()) if match else None

    @staticmethod
    def _parse_price_guess(text: str) -> t.Optional[str]:
        for pat in (r"¥\s?\d[\d,]*", r"\d[\d,]*\s*円"):
            match = re.search(pat, text)
            if match:
                return match.group(0).replace(" ", "")
        amount = BoothHandler._parse_meta(text, "product:price:amount")
        currency = BoothHandler._parse_meta(text, "product:price:currency")
        if amount:
            if currency and currency.upper() == "JPY":
                return f"JPY {amount}"
            return amount
        return None

