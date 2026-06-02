from __future__ import annotations

from rss_app.sites.base import HandlerContext, SiteHandler
from rss_app.sites.booth import BoothHandler
from rss_app.sites.general import GeneralHtmlHandler
from rss_app.sites.github_search import GitHubSearchHandler
from rss_app.sites.pass_through import PassThroughFeedHandler
from rss_app.sites.twitter import TwitterHandler


HANDLERS: list[SiteHandler] = [
    TwitterHandler(),
    GitHubSearchHandler(),
    PassThroughFeedHandler(),
    BoothHandler(),
    GeneralHtmlHandler(),
]


def get_requested_handler(site: dict) -> SiteHandler | None:
    requested = str(
        site.get("handler")
        or site.get("type")
        or site.get("module")
        or ""
    ).strip()
    if not requested:
        return None
    for handler in HANDLERS:
        if handler.supports(requested):
            return handler
    raise ValueError(f"unknown site handler: {requested}")


def select_handler(ctx: HandlerContext) -> SiteHandler:
    requested_handler = get_requested_handler(ctx.site)
    if requested_handler is not None:
        return requested_handler

    for handler in HANDLERS:
        if handler.matches(ctx):
            return handler
    return HANDLERS[-1]
