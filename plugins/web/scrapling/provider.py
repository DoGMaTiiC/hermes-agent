"""Scrapling local content extraction provider.

Scrapling is a no-key local extraction backend.  This plugin intentionally
advertises extract-only capability so users can pair it with a separate search
backend such as SearXNG or ddgs.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


class ScraplingWebSearchProvider(WebSearchProvider):
    """Extract URL content via the installed ``scrapling`` package."""

    @property
    def name(self) -> str:
        return "scrapling"

    @property
    def display_name(self) -> str:
        return "Scrapling (local extract)"

    def is_available(self) -> bool:
        """Return True when Scrapling and markdown conversion deps import."""
        try:
            import scrapling  # noqa: F401
            import markdownify  # noqa: F401
            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract one or more URLs using ``scrapling.fetchers.Fetcher``.

        The returned shape matches the ``WebSearchProvider`` extract contract.
        ``max_chars`` is honored defensively when supplied by callers.
        """
        try:
            from markdownify import markdownify as html_to_markdown
            from scrapling.fetchers import Fetcher
        except ImportError as exc:
            return [
                {
                    "url": str(url),
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "metadata": {"backend": self.name},
                    "error": f"Scrapling provider unavailable: {exc}",
                }
                for url in urls
            ]

        max_chars = kwargs.get("max_chars")
        try:
            max_chars_int = int(max_chars) if max_chars else None
        except (TypeError, ValueError):
            max_chars_int = None

        results: List[Dict[str, Any]] = []
        for url in urls:
            url_str = str(url)
            try:
                page = Fetcher.get(url_str, timeout=30)
                status = getattr(page, "status", None)
                final_url = str(getattr(page, "url", url_str) or url_str)

                raw_bytes = getattr(page, "body", b"") or b""
                if isinstance(raw_bytes, bytes):
                    raw_html = raw_bytes.decode("utf-8", errors="replace")
                else:
                    raw_html = str(raw_bytes)

                title = ""
                try:
                    title = str(page.css("title::text").get() or "").strip()
                except Exception:  # noqa: BLE001 - selector failures should not fail extraction
                    title = ""

                markdown = html_to_markdown(raw_html, heading_style="ATX").strip()
                if max_chars_int and len(markdown) > max_chars_int:
                    markdown = markdown[:max_chars_int].rstrip() + "\n\n[truncated]"

                results.append(
                    {
                        "url": final_url,
                        "title": title,
                        "content": markdown,
                        "raw_content": raw_html,
                        "metadata": {"backend": self.name, "status": status},
                    }
                )
            except Exception as exc:  # noqa: BLE001 - per-URL failure should not abort the batch
                logger.warning("Scrapling extract failed for %s: %s", url_str, exc)
                results.append(
                    {
                        "url": url_str,
                        "title": "",
                        "content": "",
                        "raw_content": "",
                        "metadata": {"backend": self.name},
                        "error": f"Scrapling extract failed: {exc}",
                    }
                )
        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Scrapling (local extract)",
            "badge": "free · local · extract only",
            "tag": "Extract URL content locally with Scrapling; pair with SearXNG/ddgs for search.",
            "env_vars": [],
            "post_setup": "scrapling",
        }
