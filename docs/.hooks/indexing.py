"""Keep error pages available without adding them to search engine indexes."""

from __future__ import annotations

from typing import Any

from mkdocs.structure.files import File
from mkdocs.structure.pages import Page


def _is_indexable(file: File) -> bool:
    page = file.page
    return page is None or page.meta.get("robots") != "noindex"


def _noindex(output: str) -> str:
    return output.replace("</head>", '<meta name="robots" content="noindex">\n</head>', 1)


def on_post_page(output: str, *, page: Page, **_: object) -> str:
    return _noindex(output) if page.meta.get("robots") == "noindex" else output


def on_post_template(output: str, *, template_name: str, **_: object) -> str:
    return _noindex(output) if template_name == "404.html" else output


def on_template_context(
    context: dict[str, Any], *, template_name: str, **_: object
) -> dict[str, Any]:
    if template_name == "sitemap.xml":
        context["pages"] = [file for file in context["pages"] if _is_indexable(file)]
        context["i18n_alternates"] = {
            locale: [file for file in files if _is_indexable(file)]
            for locale, files in context["i18n_alternates"].items()
        }
    return context
