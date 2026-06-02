from __future__ import annotations

from rss_app.core import fmt_human, rewrite_feed_datetimes, sniff_feed_type
from rss_app.sites.base import HandlerContext, HandlerResult, SiteHandler


class PassThroughFeedHandler(SiteHandler):
    name = "pass_through"
    aliases = ("feed", "rss", "atom", "pass-through")

    def matches(self, ctx: HandlerContext) -> bool:
        return sniff_feed_type(ctx.body, ctx.headers) is not None

    def build(self, ctx: HandlerContext) -> HandlerResult:
        feed_type = sniff_feed_type(ctx.body, ctx.headers) or "feed"
        return HandlerResult(
            content=rewrite_feed_datetimes(
                ctx.body,
                target=ctx.rss_target(),
                tz=ctx.timezone,
            ),
            commit_message=(
                f"feat: pass-through feed ({feed_type}) for "
                f"{ctx.name} at {fmt_human(ctx.now_local)}"
            ),
            log_label="pass-through",
        )

