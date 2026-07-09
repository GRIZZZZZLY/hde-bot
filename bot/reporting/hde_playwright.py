"""
Playwright automation for two HDE report pages:

1. Staff user report  (/ru/review/staff_user_report/)
   → tickets count + avg completion time for the operator

2. Global report  (/ru/review/)
   → SLA violations ("Сгорел") count for the operator

Both reports are fetched in a single browser session (one login).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import asyncio

from playwright.async_api import Page, TimeoutError as PWTimeoutError, async_playwright

logger = logging.getLogger(__name__)


@dataclass
class OperatorReportData:
    operator: str
    tickets: str
    avg_time: str
    sla_violations: str = "0"


async def get_operator_report_data(
    base_url: str,
    login: str,
    password: str,
    report_date: date,
    operator_name: str,
    screenshots_dir: Path,
    headless: bool = True,
) -> OperatorReportData:
    """Log in to HDE once, fetch both reports and return combined data.

    Retries the whole flow once on Playwright timeout — HDE occasionally stalls
    during peak hours (observed at 19:00 MSK) and a single retry is usually
    enough to recover.
    """
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            return await _fetch_once(
                base_url, login, password, report_date,
                operator_name, screenshots_dir, headless,
            )
        except (PWTimeoutError, RuntimeError) as exc:
            # Permanent failures — do not retry.
            msg = str(exc)
            if (
                "Chromium is not installed" in msg
                or "Login failed" in msg
                or "Login form not found" in msg
            ):
                raise
            last_exc = exc
            if attempt == 1:
                logger.warning(
                    "HDE report attempt %d failed (%s) — retrying in 5s",
                    attempt, exc,
                )
                await asyncio.sleep(5)
                continue
            raise
    assert last_exc is not None
    raise RuntimeError(f"HDE report failed: {last_exc}") from last_exc


async def _fetch_once(
    base_url: str,
    login: str,
    password: str,
    report_date: date,
    operator_name: str,
    screenshots_dir: Path,
    headless: bool,
) -> OperatorReportData:
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=headless)
        except Exception as exc:
            raise RuntimeError(
                "Playwright Chromium is not installed. "
                "Run 'python -m playwright install chromium' on this machine."
            ) from exc

        context = await browser.new_context()
        page = await context.new_page()

        try:
            await _login(page, base_url, login, password)

            # 1. Staff report: tickets + avg_time
            data = await _get_staff_report(page, base_url, report_date, operator_name)

            # 2. Global report: SLA violations
            sla_violations = await _get_global_report_sla(
                page, base_url, report_date, operator_name
            )
            data.sla_violations = sla_violations

            return data

        except Exception as exc:
            screenshot = screenshots_dir / f"error_{report_date.isoformat()}.png"
            try:
                await page.screenshot(path=str(screenshot), full_page=True)
                logger.error("Saved Playwright error screenshot to %s", screenshot)
            except Exception as shot_exc:
                logger.debug("error screenshot failed: %s", shot_exc)
            raise RuntimeError(f"HDE report failed: {exc}") from exc
        finally:
            await browser.close()


# ── Login ────────────────────────────────────────────────────────────────────

async def _login(page: Page, base_url: str, login: str, password: str) -> None:
    logger.info("Logging in to %s", base_url)
    await page.goto(base_url, wait_until="domcontentloaded", timeout=30_000)

    _LOGIN_FIELD = (
        'input[type="email"], input[type="text"], '
        'input[name="email"], input[name="username"], input[name="login"]'
    )
    try:
        await page.wait_for_selector(_LOGIN_FIELD, timeout=20_000)
    except Exception:
        raise RuntimeError(
            f"Login form not found at {page.url}. "
            "The page may have redirected or the HDE login page structure changed. "
            "Check the error screenshot in artifacts/screenshots/."
        )

    await page.locator(_LOGIN_FIELD).first.fill(login)
    await page.locator('input[type="password"]').fill(password)

    async with page.expect_navigation(wait_until="load", timeout=45_000):
        await page.locator('button:has-text("Войти"), button[type="submit"]').click()

    if await page.locator('button:has-text("Войти")').count() > 0:
        raise RuntimeError(
            "Login failed: still on the login page. "
            "Check HDE_API_EMAIL and HDE_REPORT_PASSWORD."
        )

    logger.info("Login successful, current URL: %s", page.url)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _set_flatpickr_dates(page: Page, report_date: date) -> None:
    """Set both flatpickr date inputs: start = 00:00, end = 23:59.

    Addresses inputs by name (from_date / to_date) with positional fallback.
    Sets end date FIRST so flatpickr's minDate constraint on the 'from'
    picker doesn't reject start > current end. Verifies values after setting
    and retries once if they didn't stick (seen on slower VPS).
    """
    y, m, d = report_date.year, report_date.month, report_date.day
    expected = f"{d:02d}.{m:02d}.{y}"

    js = """([y, m, d]) => {
        const all = Array.from(document.querySelectorAll("input"))
            .filter((input) => !!input._flatpickr);
        const byName = (name) => all.find(i => i.name === name);
        const fromInput = byName("from_date") || all[0];
        const toInput   = byName("to_date")   || all[1];
        const start = new Date(y, m - 1, d, 0, 0);
        const end   = new Date(y, m - 1, d, 23, 59);
        if (toInput)   toInput._flatpickr.setDate(end, true);
        if (fromInput) fromInput._flatpickr.setDate(start, true);
        return {from: fromInput && fromInput.value, to: toInput && toInput.value};
    }"""

    # Fallback: write raw value and dispatch change events. Used when flatpickr
    # setDate silently drops the value (seen on the global report after drag).
    fallback_js = """([y, m, d, expected]) => {
        const all = Array.from(document.querySelectorAll("input"))
            .filter((input) => !!input._flatpickr);
        const byName = (name) => all.find(i => i.name === name);
        const fromInput = byName("from_date") || all[0];
        const toInput   = byName("to_date")   || all[1];
        const setVal = (inp) => {
            if (!inp) return null;
            inp.value = expected;
            inp.dispatchEvent(new Event('input',  {bubbles: true}));
            inp.dispatchEvent(new Event('change', {bubbles: true}));
            return inp.value;
        };
        const f = setVal(fromInput);
        const t = setVal(toInput);
        return {from: f, to: t};
    }"""

    def _ok(val: str | None) -> bool:
        # value may include time: "10.04.2026 00:00" — check prefix only
        return bool(val and val.startswith(expected))

    result = await page.evaluate(js, [y, m, d])
    for wait_ms in (600, 1200, 2000):
        if _ok(result.get("from")) and _ok(result.get("to")):
            break
        logger.warning(
            "Flatpickr dates did not stick (from=%s to=%s, expected %s) — retrying in %dms",
            result.get("from"), result.get("to"), expected, wait_ms,
        )
        await page.wait_for_timeout(wait_ms)
        result = await page.evaluate(js, [y, m, d])

    if not _ok(result.get("from")) or not _ok(result.get("to")):
        logger.warning(
            "Flatpickr setDate failed after retries — falling back to raw value injection"
        )
        result = await page.evaluate(fallback_js, [y, m, d, expected])
        if not _ok(result.get("from")) or not _ok(result.get("to")):
            raise RuntimeError(
                f"Failed to set flatpickr dates: got from={result.get('from')} "
                f"to={result.get('to')}, expected {expected}"
            )
    logger.info("Flatpickr dates set: from=%s to=%s", result["from"], result["to"])


async def _check_close_date(page: Page) -> None:
    """Ensure the 'Дата закрытия' checkbox is checked."""
    cb = page.locator(
        'label:has-text("Дата закрытия") input[type="checkbox"], '
        'input[type="checkbox"][id*="close"], '
        'input[type="checkbox"][name*="close"]'
    )
    if await cb.count():
        if not await cb.is_checked():
            await cb.click()
    else:
        logger.warning("Could not find the 'Дата закрытия' checkbox")


async def _click_request_button(page: Page) -> None:
    for selector in (
        'button:has-text("Запросить")',
        'input[value="Запросить"]',
        'button[type="submit"]',
        ".btn-primary",
    ):
        if await page.locator(selector).count():
            await page.locator(selector).first.click()
            return
    raise RuntimeError(
        "Could not find the 'Запросить' button. "
        "Adjust selectors in bot/reporting/hde_playwright.py."
    )


async def _click_show_first(page: Page) -> None:
    btn = page.locator('a:has-text("Показать"), button:has-text("Показать")').first
    await btn.wait_for(state="visible", timeout=30_000)
    await btn.click()


# ── Staff report ──────────────────────────────────────────────────────────────

async def _get_staff_report(
    page: Page,
    base_url: str,
    report_date: date,
    operator_name: str,
) -> OperatorReportData:
    url = f"{base_url}/ru/review/staff_user_report/"
    logger.info("Opening staff report: %s", url)
    await page.goto(url, wait_until="load", timeout=30_000)

    await _set_flatpickr_dates(page, report_date)
    await _check_close_date(page)
    await _click_request_button(page)
    await _click_show_first(page)

    await page.wait_for_selector('th:has-text("Сотрудник")', timeout=15_000)
    return await _parse_staff_table(page, operator_name)


async def _parse_staff_table(page: Page, operator_name: str) -> OperatorReportData:
    tables = page.locator("table")
    table = None
    headers: list[str] = []

    for i in range(await tables.count()):
        candidate = tables.nth(i)
        vals = [h.strip() for h in await candidate.locator("thead tr th").all_text_contents()]
        if any("сотрудник" in h.lower() for h in vals):
            table, headers = candidate, vals
            break

    if table is None:
        all_h = [
            [h.strip() for h in await tables.nth(i).locator("thead tr th").all_text_contents()]
            for i in range(await tables.count())
        ]
        raise RuntimeError(f"Staff table not found. Seen headers: {all_h}")

    def find_col(keywords: list[str]) -> int | None:
        for i, h in enumerate(headers):
            if any(k.lower() in h.lower() for k in keywords):
                return i
        return None

    col_name    = find_col(["сотрудник", "оператор", "employee"])
    col_tickets = find_col(["количество заявок", "кол-во заявок", "tickets"])
    col_avg     = find_col(["среднее время на выполнение", "среднее время выполн", "avg"])

    if col_name is None:
        raise RuntimeError(f"Cannot find 'Сотрудник' column. Headers: {headers}")
    if col_tickets is None:
        raise RuntimeError(f"Cannot find 'Количество заявок' column. Headers: {headers}")
    if col_avg is None:
        raise RuntimeError(f"Cannot find 'Среднее время на выполнение' column. Headers: {headers}")

    rows = await table.locator("tbody tr").all()
    expected = operator_name.strip().lower()

    for row in rows:
        cells = [c.strip() for c in await row.locator("td").all_text_contents()]
        if len(cells) <= max(col_name, col_tickets, col_avg):
            continue
        if cells[col_name].lower() == expected:
            logger.info("Staff report: operator=%s tickets=%s avg=%s",
                        cells[col_name], cells[col_tickets], cells[col_avg])
            return OperatorReportData(
                operator=cells[col_name],
                tickets=cells[col_tickets],
                avg_time=cells[col_avg],
            )

    available = [(await r.locator("td").nth(col_name).text_content() or "").strip() for r in rows]
    raise RuntimeError(
        f"Operator '{operator_name}' not found in staff report. Available: {available}"
    )


# ── Global report (SLA violations) ───────────────────────────────────────────

async def _add_param_partial(page: Page, name: str) -> None:
    """Move a param by partial text match (fallback for exact-match failures)."""
    result = await page.evaluate(
        """(name) => {
            const panels = document.querySelectorAll('div.connectedSortable');
            if (panels.length < 2) return null;
            const left = panels[0], right = panels[1];
            const span = Array.from(left.querySelectorAll('span.sortable-dual-list-element'))
                .find(s => s.textContent.trim().includes(name));
            if (span) { right.appendChild(span); return span.textContent.trim(); }
            return null;
        }""",
        name,
    )
    if result:
        logger.info("Added '%s' via partial match: '%s'", name, result)
    else:
        logger.warning("Could not find '%s' even by partial match", name)


async def _get_global_report_sla(
    page: Page,
    base_url: str,
    report_date: date,
    operator_name: str,
) -> str:
    """
    Open the HDE global report, ensure 'Исполнитель' + 'Сгорел' are in the
    right panel, request the report, and return the 'Сгорел' value for the operator.
    """
    url = f"{base_url}/ru/review/global_report/"
    logger.info("Opening global report: %s", url)
    await page.goto(url, wait_until="load", timeout=30_000)

    # Wait until param items are rendered
    await page.wait_for_selector("span.sortable-dual-list-element", timeout=15_000)

    await _setup_global_report_params(page)
    await _set_flatpickr_dates(page, report_date)
    await _check_close_date(page)
    await _click_request_button(page)
    await _click_show_first(page)

    # Wait for result table — column header will match whatever param name HDE uses
    await page.wait_for_selector(
        'th:has-text("Исполнитель"), th:has-text("Сгорел"), th:has-text("owner")',
        timeout=30_000,
    )
    return await _parse_global_table(page, operator_name)


async def _setup_global_report_params(page: Page) -> None:
    """
    Configure right panel: drag defaults back to left, drag required to right.

    DOM structure:
      Left:  div#ui-sortable-dual-list-1  span.sortable-dual-list-element
      Right: div#ui-sortable-dual-list-2  span.sortable-dual-list-element

    Uses real Playwright drag_to() so jQuery UI sortable events fire correctly.
    """
    RIGHT_ID = "ui-sortable-dual-list-2"
    LEFT_ID  = "ui-sortable-dual-list-1"
    REQUIRED = ["Исполнитель", "Сгорел"]

    right_panel = page.locator(f"#{RIGHT_ID}")
    left_panel  = page.locator(f"#{LEFT_ID}")

    # Step 1: drag all items currently in right panel back to left
    right_spans = right_panel.locator("span.sortable-dual-list-element")
    count = await right_spans.count()
    for i in range(count):
        span = right_spans.nth(0)  # always take first — list shrinks after each drag
        await span.drag_to(left_panel)
        await page.wait_for_timeout(200)

    # Step 2: drag required items from left to right
    for name in REQUIRED:
        span = left_panel.locator(f"span.sortable-dual-list-element:has-text('{name}')").first
        if await span.count():
            await span.drag_to(right_panel)
            await page.wait_for_timeout(300)
            logger.info("Dragged '%s' to right panel", name)
        else:
            logger.warning("Could not find '%s' in left panel", name)

    # Verify
    right_items = await right_panel.locator("span.sortable-dual-list-element").all_text_contents()
    logger.info("Right panel after setup: %s", [t.strip() for t in right_items])


async def _parse_global_table(page: Page, operator_name: str) -> str:
    """Find operator row in global report table and return 'Сгорел' cell value."""
    tables = page.locator("table")
    table = None
    headers: list[str] = []

    for i in range(await tables.count()):
        candidate = tables.nth(i)
        vals = [h.strip() for h in await candidate.locator("thead tr th").all_text_contents()]
        if any("исполнитель" in h.lower() or "сгорел" in h.lower() for h in vals):
            table, headers = candidate, vals
            break

    if table is None:
        all_h = [
            [h.strip() for h in await tables.nth(i).locator("thead tr th").all_text_contents()]
            for i in range(await tables.count())
        ]
        logger.warning("Global report table not found. Seen headers: %s — defaulting to 0", all_h)
        return "0"

    def find_col(keywords: list[str]) -> int | None:
        for i, h in enumerate(headers):
            if any(k.lower() in h.lower() for k in keywords):
                return i
        return None

    col_name = find_col(["исполнитель", "сотрудник", "operator"])
    col_sla  = find_col(["сгорел", "sla", "нарушен"])

    if col_name is None or col_sla is None:
        logger.warning(
            "Cannot find required columns in global report. Headers: %s — defaulting to 0", headers
        )
        return "0"

    rows = await table.locator("tbody tr").all()
    expected = operator_name.strip().lower()

    for row in rows:
        cells = [c.strip() for c in await row.locator("td").all_text_contents()]
        if len(cells) <= max(col_name, col_sla):
            continue
        if cells[col_name].lower() != expected:
            continue

        raw = cells[col_sla]
        logger.info("Global report: operator=%s sla_cell=%r", cells[col_name], raw)

        if not raw:
            return "0"

        # Each violation is a separate line (e.g. "19:10:13 31.03.2026 антон")
        # Count non-empty lines
        count = sum(1 for line in raw.splitlines() if line.strip())
        return str(count)

    logger.warning(
        "Operator '%s' not found in global report — defaulting to 0", operator_name
    )
    return "0"
