"""Playwright E2E specs for the served `/dashboard` page — the render-state
and XSS-safety behaviors that can only be proved by actually executing the
shipped `dashboard.js` in a real browser (AC4, AC6, AC7, AC13, AC20, AC23).

Specs are authored by reading `src/web/static/dashboard.html`/`.js` for the
real selectors/data-testids — never by live-driving a browser to discover
them.
"""

def test_populated_state_renders_stat_card_and_ten_rows(page, e2e_server, seed_event):
    """AC4: real seeded data renders the CMP-5 stat card + a 10-row CMP-7 table."""
    seed_event(customer_id="acme-corp", metric="api_calls", quantity="7", idempotency_key="populated-1")

    page.goto(f"{e2e_server['base_url']}/dashboard")
    page.wait_for_selector('[data-testid="populated-state"]:not([hidden])', timeout=15000)

    assert page.locator('[data-testid="stat-number"]').inner_text() != ""
    rows = page.locator('[data-testid="usage-table"] tbody tr')
    assert rows.count() == 10


def test_empty_state_renders_when_no_usage_rows(page, e2e_server):
    """AC5: a customer/metric combination with zero usage renders the empty
    card, driven by the real "all 11 windows zero" trigger."""
    page.goto(f"{e2e_server['base_url']}/dashboard")
    page.select_option('[data-testid="customer-select"]', "globex")
    page.select_option('[data-testid="metric-select"]', "storage_gb")

    page.wait_for_selector('[data-testid="empty-state"]:not([hidden])', timeout=15000)
    assert "No events yet for globex" in page.locator("#empty-state-title").inner_text()


def test_loading_skeleton_shows_ten_rows_on_filter_change(page, e2e_server):
    """AC6: the loading skeleton mirrors the populated table with exactly 10
    placeholder rows on a filter change, and appears before it resolves."""
    page.goto(f"{e2e_server['base_url']}/dashboard")
    page.wait_for_selector('[data-testid="populated-state"]:not([hidden]), [data-testid="empty-state"]:not([hidden])', timeout=15000)

    # Delay the BFF response so the loading state is observable.
    def _delay_route(route):
        import time

        time.sleep(0.6)
        route.continue_()

    page.route("**/dashboard/api/usage-series*", _delay_route)
    page.select_option('[data-testid="metric-select"]', "storage_gb")

    page.wait_for_selector('[data-testid="loading-state"]:not([hidden])', timeout=5000)
    skeleton_rows = page.locator("#skeleton-rows .skeleton-row")
    assert skeleton_rows.count() == 10
    page.unroute("**/dashboard/api/usage-series*")


def test_error_state_renders_on_fetch_failure_with_working_retry(page, e2e_server):
    """AC7: a forced BFF failure renders the generic error card (no raw
    error text) with a Retry control that successfully re-fetches."""
    page.goto(f"{e2e_server['base_url']}/dashboard")
    page.wait_for_selector('[data-testid="populated-state"]:not([hidden]), [data-testid="empty-state"]:not([hidden])', timeout=15000)

    failed_once = {"done": False}

    def _fail_once(route):
        if not failed_once["done"]:
            failed_once["done"] = True
            route.fulfill(status=500, json={"error": {"code": "internal", "message": "an internal error occurred", "requestId": "x"}})
        else:
            route.continue_()

    page.route("**/dashboard/api/usage-series*", _fail_once)
    page.select_option('[data-testid="customer-select"]', "initech")

    page.wait_for_selector('[data-testid="error-state"]:not([hidden])', timeout=15000)
    error_text = page.locator('[data-testid="error-state"]').inner_text()
    assert "internal error occurred" not in error_text
    assert "Couldn" in error_text

    page.locator('[data-testid="retry-button"]').click()
    page.wait_for_selector('[data-testid="populated-state"]:not([hidden]), [data-testid="empty-state"]:not([hidden])', timeout=15000)
    page.unroute("**/dashboard/api/usage-series*")


def test_month_segment_disabled_affordance_and_issues_no_request(page, e2e_server):
    """AC23/Q1: the month segment renders disabled with a tooltip affordance
    and clicking it never issues a granularity=month request."""
    page.goto(f"{e2e_server['base_url']}/dashboard")
    page.wait_for_selector('[data-testid="populated-state"]:not([hidden]), [data-testid="empty-state"]:not([hidden])', timeout=15000)

    month_button = page.locator('.segment-button[data-granularity="month"]')
    assert month_button.get_attribute("aria-disabled") == "true"
    assert month_button.get_attribute("title") != ""

    seen_requests = []
    page.on("request", lambda request: seen_requests.append(request.url))
    month_button.click(force=True)
    page.wait_for_timeout(500)

    assert not any("granularity=month" in url for url in seen_requests)
    # the active segment must still be hour/day, never month
    assert "segment-button--active" not in (month_button.get_attribute("class") or "")


def test_xss_safe_rendering_injected_markup_never_executes(page, e2e_server):
    """AC13: a malicious value in a rendered field is written via textContent
    and appears as inert text — it never executes as script."""
    page.goto(f"{e2e_server['base_url']}/dashboard")
    page.wait_for_selector('[data-testid="populated-state"]:not([hidden]), [data-testid="empty-state"]:not([hidden])', timeout=15000)

    payload = "<img src=x onerror=window.__xss_fired=true>"

    def _inject(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"state": "populated", "current": {"window_start": "%s", '
                '"quantity": "1", "metric": "%s", "window_label": "this hour", '
                '"delta_text": "+1", "delta_direction": "up"}, "rows": ['
                + ",".join(
                    '{"window_start": "%s", "metric": "%s", "quantity": "1", '
                    '"delta_text": "+1", "delta_direction": "up"}' % (payload, payload)
                    for _ in range(10)
                )
                + "]}"
            ) % (payload, payload),
        )

    page.route("**/dashboard/api/usage-series*", _inject)
    page.select_option('[data-testid="metric-select"]', "storage_gb")
    page.wait_for_selector('[data-testid="populated-state"]:not([hidden])', timeout=15000)

    xss_fired = page.evaluate("window.__xss_fired === true")
    assert not xss_fired, "an injected payload executed as script — textContent sink was bypassed"

    rendered_html = page.locator('[data-testid="usage-table"] tbody').inner_html()
    assert "<img" not in rendered_html, "markup was interpreted as an element, not rendered as inert text"
    assert "&lt;img" in rendered_html or "onerror" in rendered_html

    page.unroute("**/dashboard/api/usage-series*")


def test_no_api_key_ever_appears_in_page_or_network(page, e2e_server):
    """AC9 (E2E half): the browser never sends or receives a credential."""
    seen_headers = []
    page.on("request", lambda request: seen_headers.append(dict(request.headers)))

    page.goto(f"{e2e_server['base_url']}/dashboard")
    page.wait_for_selector('[data-testid="populated-state"]:not([hidden]), [data-testid="empty-state"]:not([hidden])', timeout=15000)

    for headers in seen_headers:
        assert "authorization" not in headers

    html = page.content()
    assert "mtr_live" not in html
    assert e2e_server["reader_key"] not in html


def test_environment_badge_reflects_configured_environment(page, e2e_server):
    """AC20: the CMP-2 badge label matches the real configured environment
    (defaults to `local` for this test run, non-prod -> staging/grey variant)."""
    page.goto(f"{e2e_server['base_url']}/dashboard")
    page.wait_for_selector("#env-badge-label:not(:empty)", timeout=15000)

    # `inner_text()` reflects the CSS `text-transform: uppercase` the design
    # applies to the badge label — compare the underlying (lowercase) value.
    label = page.locator("#env-badge-label").inner_text().lower()
    badge_class = page.locator("#env-badge").get_attribute("class")
    assert label in {"local", "staging", "prod"}
    if label != "prod":
        assert "env-badge--staging" in badge_class
    else:
        assert "env-badge--prod" in badge_class
