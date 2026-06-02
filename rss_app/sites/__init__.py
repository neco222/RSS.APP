from __future__ import annotations

from rss_app.sites.base import HandlerContext, SiteHandler
from rss_app.sites.booth import BoothHandler
from rss_app.sites.general import GeneralHtmlHandler
from rss_app.sites.github_search import GitHubSearchHandler
from rss_app.sites.pass_through import PassThroughFeedHandler


HANDLERS: list[SiteHandler] = [
    GitHubSearchHandler(),
    PassThroughFeedHandler(),
    BoothHandler(),
    GeneralHtmlHandler(),
]


def select_handler(ctx: HandlerContext) -> SiteHandler:
    requested = str(
        ctx.site.get("handler")
        or ctx.site.get("type")
        or ctx.site.get("module")
        or ""
    ).strip()
    if requested:
        for handler in HANDLERS:
            if handler.supports(requested):
                return handler
        raise ValueError(f"unknown site handler: {requested}")

    for handler in HANDLERS:
        if handler.matches(ctx):
            return handler
    return HANDLERS[-1]

