"""Scrapling local web extract plugin — bundled, auto-loaded."""

from __future__ import annotations

from plugins.web.scrapling.provider import ScraplingWebSearchProvider


def register(ctx) -> None:
    """Register the Scrapling provider with the plugin context."""
    ctx.register_web_search_provider(ScraplingWebSearchProvider())
