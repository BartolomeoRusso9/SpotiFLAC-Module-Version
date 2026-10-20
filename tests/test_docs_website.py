from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"
SITE_PAGES = ("index.html", "downloads.html", "partners.html")


class _LocalReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if element_id := attributes.get("id"):
            self.ids.add(element_id)
        for name in ("href", "src"):
            if value := attributes.get(name):
                self.references.append(value)


def test_documentation_sites_share_current_branding_and_accessibility() -> None:
    for page_name in SITE_PAGES:
        html = (DOCS_DIR / page_name).read_text(encoding="utf-8")
        parser = _LocalReferences()
        parser.feed(html)

        assert 'href="site.css?' in html
        assert 'src="assets/images/logo.png"' in html
        assert 'href="#main-content"' in html
        assert "main-content" in parser.ids
        assert "Mornye Module Version" not in html
        assert "pip install Mornye" not in html


def test_documentation_site_local_assets_and_links_exist() -> None:
    for page_name in SITE_PAGES:
        page = DOCS_DIR / page_name
        parser = _LocalReferences()
        parser.feed(page.read_text(encoding="utf-8"))

        for reference in parser.references:
            parsed = urlsplit(reference)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (page.parent / unquote(parsed.path)).resolve()
            assert target.is_file(), f"{page_name} refers to missing {reference}"


def test_homepage_examples_use_installable_distribution_and_import_name() -> None:
    html = (DOCS_DIR / "index.html").read_text(encoding="utf-8")

    assert "python -m pip install ." in html
    assert "from</span> mornye <span" in html
    assert "pip install Mornye" not in html
    assert "from Mornye " not in html
