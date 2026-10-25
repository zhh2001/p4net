"""Emit absolute language alternate URLs in generated HTML metadata."""

from __future__ import annotations

import re
from html import escape, unescape
from urllib.parse import urljoin

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.pages import Page

_HREFLANG = re.compile(
    r"""(<link\b(?=[^>]*\bhreflang\s*=)[^>]*\bhref\s*=\s*)(["'])(.*?)\2""",
    re.IGNORECASE,
)


def _absolute_hreflang(output: str, base_url: str | None) -> str:
    def replace(match: re.Match[str]) -> str:
        url = escape(urljoin(base_url or "", unescape(match[3])), quote=True)
        return f"{match[1]}{match[2]}{url}{match[2]}"

    return _HREFLANG.sub(replace, output)


def on_post_page(output: str, *, page: Page, **_: object) -> str:
    return _absolute_hreflang(output, page.canonical_url)


def on_post_template(output: str, *, config: MkDocsConfig, **_: object) -> str:
    return _absolute_hreflang(output, config.site_url)
