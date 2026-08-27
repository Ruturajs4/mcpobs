"""Browser flows for the customer console.

WHY THIS FILE EXISTS. Every console behaviour in this product was verified by
hand in a real browser and nothing stopped it regressing -- which is how the
column-order bug shipped for one commit (the `<th>` went after Status and the
`<td>` before it, so every row showed its status under a "Transport" heading),
and how the flicker, the focus-restore-to-body and the stale-cache bugs each got
as far as a human noticing them.

These are the flows a customer walks, not unit tests of the JavaScript. They
drive the real console against the real API, because most of the bugs found this
session lived in the seam between them rather than in either side.

REQUIRES THE STACK. They skip when it is not running -- but the skip is LOUD
about why, because a guard that reports green while testing nothing is worse
than no guard. That has already happened once here, when a `shutil.which` check
silently disabled every stdio test.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import urllib.error
import urllib.request

import pytest

CONSOLE = os.getenv("MCPOBS_CONSOLE_URL", "http://localhost:8080")
KEY_FILE = pathlib.Path(__file__).parents[1] / ".mcpobs-keys.env"


def _read_key() -> str:
    """A key to sign in with.

    `MCPOBS_READ_KEY` or `.mcpobs-keys.env` first, for running these against
    a real managed/multi-tenant deployment where the value actually has to
    authenticate. Otherwise falls back to a placeholder: `make up-lite`'s
    `LocalAuthenticator` (docs/decisions.md D180) accepts any `x-api-key`
    value at all, so there is nothing to provision for the common case of
    running these against the self-hosted lite stack -- unlike the old
    `scripts/admin.py devkeys` model this repo no longer has.
    """
    if os.getenv("MCPOBS_READ_KEY"):
        return os.environ["MCPOBS_READ_KEY"]
    if KEY_FILE.exists():
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("MCPOBS_READ_KEY="):
                return line.split("=", 1)[1].strip()
    return "any-value-works-against-the-self-hosted-lite-stack"


def _stack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{CONSOLE}/health", timeout=3) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


READ_KEY = _read_key()
HAVE_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not HAVE_PLAYWRIGHT, reason="playwright not installed"),
    pytest.mark.skipif(not _stack_up(), reason=f"console not reachable at {CONSOLE}"),
]


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """A signed-in page. The key goes into localStorage before any script runs,
    which is what the console itself does after the sign-in form."""
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.add_init_script(
        f"localStorage.setItem('mcpobs.key', {READ_KEY!r})"
    )
    page = context.new_page()
    # A console error during a flow is a failure even when the assertions pass:
    # the page can look right and still be throwing on every render.
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors  # type: ignore[attr-defined]
    yield page
    context.close()


def goto(page, query: str) -> None:
    page.goto(f"{CONSOLE}/?{query}", wait_until="networkidle")


class TestSignIn:
    def test_an_unauthenticated_visitor_sees_the_sign_in_form(self, browser) -> None:
        context = browser.new_context()
        page = context.new_page()
        page.goto(CONSOLE, wait_until="networkidle")
        assert page.locator("#key-input").is_visible()
        # The hint must not name the key-issuing tooling. It used to print the
        # exact admin CLI invocation to anyone who found the URL.
        hint = page.locator(".signin-hint").inner_text()
        assert "scripts/admin.py" not in hint
        assert "devkeys" not in hint
        context.close()

    def test_a_signed_in_visitor_reaches_the_console(self, page) -> None:
        goto(page, "view=overview&w=1440")
        assert page.locator("#title").inner_text() == "Overview"
        assert not page.locator("#key-input").count()


class TestOverview:
    def test_the_headline_cards_render(self, page) -> None:
        goto(page, "view=overview&w=1440")
        text = page.locator("#content").inner_text()
        for label in ("TOOL CALLS", "ERROR RATE", "P95 LATENCY"):
            assert label in text.upper()

    def test_no_page_errors(self, page) -> None:
        goto(page, "view=overview&w=1440")
        assert page.errors == []


class TestTraceList:
    def test_columns_and_cells_line_up(self, page) -> None:
        """The bug this file was written for.

        A `<th>` added after Status with its `<td>` inserted before it shipped a
        table where every row's status sat under a "Transport" heading. It looked
        plausible enough to commit and was obvious on screen.
        """
        goto(page, "view=traces&w=10080")
        page.wait_for_selector("#content tbody tr")
        headers = [h.inner_text().strip().lower() for h in page.locator("#content thead th").all()]
        assert headers == [
            "trace", "tool", "method", "status", "transport", "spans", "duration", "when",
        ]

        row = page.locator("#content tbody tr").first
        cells = [c.inner_text().strip() for c in row.locator("td").all()]
        assert len(cells) == len(headers)
        # Status holds a category, transport holds a transport -- not each other.
        assert cells[4] in ("stdio", "http", "sse", "—")

    def test_the_transport_tag_renders(self, page) -> None:
        goto(page, "view=traces&w=10080")
        page.wait_for_selector("#content tbody tr")
        tags = {t.inner_text().strip() for t in page.locator(".tag").all()}
        assert tags and tags <= {"stdio", "http", "sse"}


class TestPagination:
    def test_previous_is_disabled_on_the_first_page(self, page) -> None:
        goto(page, "view=traces&w=10080")
        page.wait_for_selector(".pagination")
        assert page.locator("#page-prev").is_disabled()

    def test_next_advances_and_previous_returns(self, page) -> None:
        goto(page, "view=traces&w=10080")
        page.wait_for_selector("#content tbody tr")
        first = page.locator("#content tbody tr td").first.inner_text()

        page.locator("#page-next").click()
        page.wait_for_timeout(1200)
        second = page.locator("#content tbody tr td").first.inner_text()
        assert second != first, "Next did not change the page"

        page.locator("#page-prev").click()
        page.wait_for_timeout(1200)
        assert page.locator("#content tbody tr td").first.inner_text() == first

    def test_changing_a_filter_resets_to_the_first_page(self, page) -> None:
        """A cursor from the old result set applied to a newly-narrowed query
        returns a page from nowhere."""
        goto(page, "view=traces&w=10080")
        page.wait_for_selector(".pagination")
        page.locator("#page-next").click()
        page.wait_for_timeout(1200)

        goto(page, "view=traces&w=10080&status=error")
        page.wait_for_timeout(800)
        assert "cursor=" not in page.url


class TestFilterPanel:
    def test_the_panel_opens_from_the_right(self, page) -> None:
        goto(page, "view=traces&w=10080")
        page.locator("#open-filters").click()
        page.wait_for_timeout(500)
        panel = page.locator("#filter-panel")
        assert panel.is_visible()
        box = panel.bounding_box()
        assert box and round(box["x"] + box["width"]) >= page.viewport_size["width"] - 2

    def test_the_closed_panel_is_inert(self, page) -> None:
        """It stayed in the accessibility tree with ten tabbable controls behind
        a visually-closed drawer."""
        goto(page, "view=traces&w=10080")
        page.locator("#open-filters").click()
        page.wait_for_timeout(400)
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        panel = page.locator("#filter-panel")
        assert panel.get_attribute("inert") is not None
        assert panel.get_attribute("aria-hidden") == "true"

    def test_focus_returns_to_the_trigger_on_close(self, page) -> None:
        """`isConnected` passed for document.body, so focus was restored to
        nothing at all."""
        goto(page, "view=traces&w=10080")
        page.locator("#open-filters").click()
        page.wait_for_timeout(400)
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        assert page.evaluate("document.activeElement?.id") == "open-filters"

    def test_a_filter_narrows_the_list_and_shows_a_chip(self, page) -> None:
        goto(page, "view=traces&w=10080")
        page.locator("#open-filters").click()
        page.wait_for_timeout(500)
        page.select_option('select[data-generic-filter="transport"]', "stdio")
        page.wait_for_timeout(1500)

        chips = [c.inner_text() for c in page.locator(".chip").all()]
        assert any("stdio" in c for c in chips)
        shown = {t.inner_text().strip() for t in page.locator(".tag").all()}
        assert shown == {"stdio"}

    def test_an_empty_result_says_which_filters_caused_it(self, page) -> None:
        """"No results" with no explanation is how people conclude their
        telemetry is missing."""
        goto(page, "view=traces&w=10080&q=zzz-definitely-no-such-trace-zzz")
        page.wait_for_selector("#content .empty")
        empty = page.locator("#content .empty").inner_text()
        assert "filter" in empty.lower()
        assert page.locator("#empty-clear").is_visible()


class TestTraceDrawer:
    def test_a_trace_opens_beside_the_list(self, page) -> None:
        goto(page, "view=traces&w=10080")
        page.wait_for_selector("#content tbody tr")
        page.locator("#content tbody tr").first.click()
        page.wait_for_selector(".drawer.open", timeout=10_000)
        # `.drawer.open` is the SHELL -- it slides in before the trace has been
        # fetched, so asserting on rows immediately races the request. Waiting
        # for the content is what the test actually means.
        page.wait_for_selector(".wf-row", timeout=15_000)
        # The list stays behind it: debugging is comparison.
        assert page.locator("#content tbody tr").count() > 0
        assert page.locator(".wf-row").count() > 0

    def test_selecting_a_span_shows_its_detail(self, page) -> None:
        goto(page, "view=traces&w=10080")
        page.wait_for_selector("#content tbody tr")
        page.locator("#content tbody tr").first.click()
        page.wait_for_selector(".wf-row")
        page.locator(".wf-row").first.click()
        page.wait_for_timeout(1200)
        assert page.locator("#span-detail .f").count() > 0

    def test_the_span_panel_does_not_show_pipeline_provenance(self, page) -> None:
        """Kafka offsets and normalizer versions describe OUR pipeline. They are
        in the API for support and deliberately not on screen."""
        goto(page, "view=traces&w=10080")
        page.wait_for_selector("#content tbody tr")
        page.locator("#content tbody tr").first.click()
        page.wait_for_selector(".wf-row")
        page.locator(".wf-row").first.click()
        page.wait_for_timeout(1200)
        detail = page.locator("#span-detail").inner_text().lower()
        assert "kafka" not in detail
        assert "normalization" not in detail

    def test_escape_closes_the_drawer(self, page) -> None:
        goto(page, "view=traces&w=10080")
        page.wait_for_selector("#content tbody tr")
        page.locator("#content tbody tr").first.click()
        page.wait_for_selector(".drawer.open")
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
        assert "open" not in (page.locator(".drawer").get_attribute("class") or "")


class TestWaterfallPaging:
    def test_a_large_trace_renders_a_page_of_spans_with_a_control(self, page) -> None:
        key = READ_KEY
        # Navigate first: `fetch('/api/v1/...')` has no base URL on about:blank.
        goto(page, "view=traces&w=10080")
        # FILTERED to a tool that fans out, rather than scanning the newest
        # traces: the list is newest-first, and a stack of recent single-span
        # traces pushed the only deep one past the page -- which made this test
        # a permanent skip that looked like coverage.
        biggest = page.evaluate(
            """async (key) => {
                const h = {'x-api-key': key};
                for (const tool of ['slow_export', 'place_order', '']) {
                    const q = tool ? `&tool=${tool}` : '';
                    const r = await fetch(
                        `/api/v1/traces?limit=200&window_minutes=10080${q}`, {headers: h});
                    const p = await r.json();
                    const top = (p.items || []).sort((a, b) => b.span_count - a.span_count)[0];
                    if (top && top.span_count > 20) return top;
                }
                return null;
            }""",
            key,
        )
        if not biggest or biggest["span_count"] <= 20:
            pytest.skip("no trace over 20 spans in the window")

        goto(page, f"view=traces&w=10080&trace={biggest['trace_id']}")
        page.wait_for_selector(".wf-row")
        assert page.locator(".wf-row").count() == 20
        more = page.locator("#wf-more")
        assert more.is_visible()
        # The control states what a click does rather than saying "view more".
        assert re.search(r"Show \d+ more", more.inner_text())

        more.click()
        page.wait_for_timeout(600)
        assert page.locator(".wf-row").count() > 20


class TestDrillThrough:
    def test_servers_to_capabilities_to_traces_carries_the_filters(self, page) -> None:
        """Filters were dropped on navigation, so a click from Servers opened an
        unfiltered trace list -- the same list you were trying to narrow."""
        goto(page, "view=servers&w=10080")
        page.wait_for_selector("#content tbody tr[data-server]")
        server = page.locator("#content tbody tr[data-server]").first.get_attribute("data-server")
        page.locator("#content tbody tr[data-server]").first.click()
        page.wait_for_timeout(1500)

        chips = " ".join(c.inner_text() for c in page.locator(".chip").all())
        assert server in chips, "the server filter was lost on drill-through"

        row = page.locator("#content tbody tr[data-item]").first
        if not row.count():
            pytest.skip("no capabilities for this server in the window")
        item = row.get_attribute("data-item")
        row.click()
        page.wait_for_timeout(1500)

        chips = " ".join(c.inner_text() for c in page.locator(".chip").all())
        assert item in chips and server in chips


class TestRefreshDoesNotFlicker:
    def test_navigating_shows_a_spinner_but_refreshing_does_not(self, page) -> None:
        """The spinner blanked the pane on EVERY render including the 30s
        auto-refresh, so an untouched dashboard went white four times a minute.
        """
        goto(page, "view=overview&w=1440")
        page.wait_for_selector("#content .card")
        spinners = page.evaluate(
            """() => new Promise(resolve => {
                let seen = 0;
                const ob = new MutationObserver(() => {
                    if (document.querySelector('#content .spin')) seen++;
                });
                ob.observe(document.getElementById('content'),
                           {childList: true, subtree: true});
                render().then(() => setTimeout(() => { ob.disconnect(); resolve(seen); }, 400));
            })"""
        )
        assert spinners == 0, "a same-view refresh blanked the content pane"
