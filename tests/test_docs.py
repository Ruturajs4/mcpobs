"""The customer documentation must not publish internal engineering documents.

`docs/` holds both. The internal set is not merely embarrassing:
`alpha-readiness.md` enumerates unpatched weaknesses and `deferred.md` lists
known gaps, so publishing either hands an attacker a prioritised checklist.

THE OBVIOUS GUARD DOES NOT WORK. Leaving a file out of `nav` only removes its
sidebar link -- MkDocs still renders it to `site/<name>/index.html` and still
writes its full text into `search/search_index.json`. Measured before relying on
it: a non-nav file was published at a guessable URL, and searching the built
site for "alpha" returned "not ready for a customer-facing alpha".

So the mechanism is `exclude_docs`, and the test greps the BUILT OUTPUT. A
config is a promise; the artefact is evidence.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "mkdocs.yml"
DOCS = ROOT / "docs"

#: Files that must never reach a customer. Named individually rather than
#: matched by pattern: a new internal document should fail this test loudly
#: until someone decides which side of the line it is on.
INTERNAL_FILES = {
    "Architecture.md",
    "decisions.md",
    "deferred.md",
    "alpha-readiness.md",
    "Day_01_Engineering_Doc_MCP_Observability.md",
    "Day_02_Engineering_Doc_MCP_Observability.md",
    "observed_attributes.md",
}

#: Phrases that exist only in the internal documents. Checked against the built
#: site so the assertion survives a file being renamed -- excluding
#: `alpha-readiness.md` by name does nothing if its contents move to
#: `readiness.md` and someone forgets to update the list.
INTERNAL_PHRASES = (
    "not ready for an external alpha",
    "Release blockers",
    "run as root",
    "no tenant isolation",
    "DF-19",
)


def _config() -> dict:
    # MkDocs uses `!!python/name:` tags that SafeLoader rejects, and those tags
    # are the reason this cannot simply use yaml.safe_load. Only the keys this
    # test cares about are read, so an unknown tag is stubbed rather than
    # executed -- the file is never evaluated.
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor(
        "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix
    )
    Loader.add_multi_constructor("!!python/name:", lambda loader, suffix, node: suffix)
    return yaml.load(CONFIG.read_text(encoding="utf-8"), Loader=Loader)


def _nav_targets(nav: object, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if isinstance(nav, str):
        out.add(nav)
    elif isinstance(nav, list):
        for item in nav:
            _nav_targets(item, out)
    elif isinstance(nav, dict):
        for value in nav.values():
            _nav_targets(value, out)
    return out


class TestInternalDocsAreExcluded:
    def test_every_canary_phrase_still_appears_in_an_internal_doc(self) -> None:
        """A phrase that matches nothing protects nothing.

        Rewriting `alpha-readiness.md` changed "not ready for a customer-facing
        alpha" to "not ready for an external alpha", which silently disarmed
        that canary -- the leak test kept passing because it was searching the
        built site for a string that no longer existed anywhere. Editing an
        internal document must not be able to weaken the guard on it.
        """
        corpus = "\n".join(
            (DOCS / f).read_text(encoding="utf-8", errors="ignore")
            for f in INTERNAL_FILES
            if (DOCS / f).exists()
        )
        dead = [p for p in INTERNAL_PHRASES if p not in corpus]
        assert not dead, (
            f"canary phrases matching no internal document: {dead}. "
            "They would never detect a leak. Replace them with text that is "
            "actually in the file you are trying to protect."
        )


    def test_every_internal_file_is_in_exclude_docs(self) -> None:
        """`exclude_docs` is the mechanism. `nav` is not."""
        excluded = set(_config().get("exclude_docs", "").split())
        missing = INTERNAL_FILES - excluded
        assert not missing, (
            f"{sorted(missing)} are not in exclude_docs. Being absent from `nav` "
            "does NOT prevent publication -- MkDocs still renders the page and "
            "indexes its full text for search."
        )

    def test_no_internal_file_is_referenced_by_the_nav(self) -> None:
        targets = _nav_targets(_config().get("nav", []))
        assert not (targets & INTERNAL_FILES)

    def test_the_nav_is_an_explicit_list(self) -> None:
        """A glob would publish whatever lands in `docs/` next."""
        assert _config().get("nav"), "nav must be declared, never inferred"

    def test_every_internal_file_still_exists(self) -> None:
        """Guards the guard.

        If someone renames `deferred.md`, this fails and forces the rename to be
        reflected here -- rather than the exclusion silently protecting a file
        that no longer exists while the renamed one publishes.
        """
        missing = {f for f in INTERNAL_FILES if not (DOCS / f).exists()}
        assert not missing, (
            f"{sorted(missing)} no longer exist. If they were renamed, update "
            "INTERNAL_FILES and mkdocs.yml -- the exclusion is now protecting "
            "nothing."
        )


def _mkdocs_available() -> bool:
    """Whether `python -m mkdocs` will run.

    Gating on `shutil.which("mkdocs")` looked equivalent and was not: the
    console script is not on PATH in a venv-invoked test run, so these tests
    skipped silently while appearing green -- a guard that protects nothing but
    reports success is worse than no guard.
    """
    return importlib.util.find_spec("mkdocs") is not None


@pytest.mark.skipif(not _mkdocs_available(), reason="mkdocs not installed")
class TestBuiltSiteIsClean:
    """The artefact, not the config."""

    @staticmethod
    @pytest.fixture(scope="class")
    def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
        out = tmp_path_factory.mktemp("site")
        subprocess.run(
            [sys.executable, "-m", "mkdocs", "build", "--site-dir", str(out)],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        return out

    def test_no_internal_page_was_rendered(self, site: Path) -> None:
        published = {p.parent.name for p in site.rglob("index.html")}
        leaked = {f for f in INTERNAL_FILES if f.removesuffix(".md") in published}
        assert not leaked, f"{sorted(leaked)} were published to the site"

    def test_no_internal_phrase_appears_anywhere(self, site: Path) -> None:
        """Including the search index, which is where the first leak was found."""
        hits: dict[str, list[str]] = {}
        for path in site.rglob("*"):
            if not path.is_file() or path.suffix not in (".html", ".json", ".txt", ".xml"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for phrase in INTERNAL_PHRASES:
                if phrase in text:
                    hits.setdefault(phrase, []).append(str(path.relative_to(site)))
        assert not hits, f"internal phrases reached the built site: {hits}"

    def test_no_internal_cross_references_in_the_sdk_reference(self, site: Path) -> None:
        """Docstrings become customer documentation the moment they are published.

        `mcpobs/__init__.py` cited "(V2 §18.2)" -- a design document the reader
        has no access to. Engineers will keep writing docstrings for engineers,
        so this fails the build rather than relying on anyone remembering that
        this particular package is public.
        """
        import re

        page = (site / "reference" / "sdk" / "index.html").read_text(
            encoding="utf-8", errors="ignore"
        )
        patterns = {
            "internal spec citation": r"V2 (?:§|&#167;)",
            "ADR reference": r"\bADR-\d",
            "deferral id": r"\bDF-\d",
            "decision id": r"\(D\d{1,3}[),]",
        }
        found = {
            label: re.findall(pattern, page)[:3]
            for label, pattern in patterns.items()
            if re.search(pattern, page)
        }
        assert not found, (
            f"internal cross-references reached the SDK reference: {found}. "
            "Rewrite the docstring so it stands alone for a customer."
        )
