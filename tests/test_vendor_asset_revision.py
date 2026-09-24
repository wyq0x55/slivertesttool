"""Regression guard for deployment-safe vendor asset URLs.

Fixed-name vendor bundles (``univer``, ``collab``, ``sbs``, ``charts``) are
served under a stable filename, so the URL must carry a content revision. The
caching layer only grants the one-year ``immutable`` max-age when the requested
``?v=`` matches the revision of the file on disk; anything else is downgraded to
``no-cache``. A template that reaches a vendor file through a plain
``url_for('static', ...)`` therefore loses the deployment-safety property the
revision exists for.

These tests are filesystem/render level: no database, no Silver, no worker.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "app" / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"

# A vendor path reached through url_for('static', ...) instead of the revision
# helper. Matches single or double quotes and an optional trailing argument.
UNVERSIONED_VENDOR_URL = re.compile(
    r"""url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"]vendor/""",
    re.VERBOSE,
)


def _iter_template_files():
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def _content_revision(relpath: str) -> str:
    digest = hashlib.blake2b(digest_size=8)
    with (STATIC_DIR / relpath).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_templates_found():
    """Guard the guard: a moved templates dir must not silently pass."""
    assert _iter_template_files(), f"no templates found under {TEMPLATES_DIR}"


def test_no_template_links_a_vendor_asset_without_a_revision():
    """Every vendor reference must go through ``static_asset_url``.

    This is the regression guard for the stale-bundle bug: an unversioned vendor
    URL cannot be pinned as immutable, and before the revision existed a
    deployment kept serving the old bundle for a year.
    """
    offenders = []
    for path in _iter_template_files():
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            if UNVERSIONED_VENDOR_URL.search(line):
                offenders.append(
                    f"{path.relative_to(TEMPLATES_DIR.parent.parent)}:{lineno}: "
                    f"{line.strip()}")
    assert not offenders, (
        "vendor assets must be referenced via static_asset_url() so the URL "
        "carries a content revision:\n" + "\n".join(offenders))


@pytest.fixture(scope="module")
def app():
    """A minimal app, only for its Jinja environment.

    ``create_app`` is side-effect-light and builds its engine lazily, so no
    database connection or bootstrap is needed to exercise a template global.
    """
    from app import create_app

    return create_app()


@pytest.mark.parametrize("relpath", [
    "vendor/charts/echarts.min.js",
    "vendor/univer/univer.full.umd.js",
    "vendor/collab/collab.umd.js",
])
def test_static_asset_url_versions_the_bundle(app, relpath):
    """The helper must emit a revision that matches the file's content."""
    with app.test_request_context():
        url = app.jinja_env.globals["static_asset_url"](relpath)

    assert "?v=" in url, f"{relpath} was served without a revision: {url}"
    emitted = url.split("?v=", 1)[1]
    assert emitted == _content_revision(relpath), (
        f"{relpath} revision does not match its content; a deployment could pin "
        f"a stale bundle (emitted={emitted})")