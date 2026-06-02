from __future__ import annotations

from rss_app.core import build_rss_xml, extract_links, fmt_human
from rss_app.sites.base import HandlerContext, HandlerResult, SiteHandler


class GeneralHtmlHandler(SiteHandler):
    name = "general"
    aliases = ("html", "general_html")

    def matches(self, ctx: HandlerContext) -> bool:
        return True

    def build(self, ctx: HandlerContext) -> HandlerResult:
        links = extract_links(
            ctx.body,
            base_url=ctx.url,
            limit=ctx.limit_arg(),
            include=ctx.include_patterns() or None,
            exclude=ctx.exclude_patterns() or None,
        )
        items = [{"title": item["title"], "link": item["link"]} for item in links]
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

