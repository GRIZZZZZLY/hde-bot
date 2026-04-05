"""
Playwright automation: login to HDE, request operator report, parse result table.

Flow:
  1. Login to HDE web UI
  2. Navigate to /ru/review/staff_user_report/
  3. Set date range (same day, start 00:00 / end 23:59), check "Дата закрытия"
  4. Click "Запросить"
  5. Wait ~5 s for report to generate, click "Показать"
  6. Find operator row in table, extract tickets + avg_time
  7. Return OperatorReportData

Selectors are marked # ADJUST: if they may differ in your HDE version.
Run once with headless=False to verify them visually.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from playwright.async_api import Page, async_playwright

logger = logging.getLogger(__name__)


@dataclass
class OperatorReportData:
    operator: str       # name as found in the table
    tickets: str        # "Количество заявок"
    avg_time: str       # "Среднее время на выполнение заявки"


async def get_operator_report_data(
    base_url: str,
    login: str,
    password: str,
    report_date: date,
    operator_name: str,
    screenshots_dir: Path,
    headless: bool = True,
) -> OperatorReportData:
    """
    Log in to HDE, generate the staff user report for *report_date*,
    find the row for *operator_name* and return its key metrics.

    Raises RuntimeError on any failure (screenshot saved automatically).
    """
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await _login(page, base_url, login, password)
            data = await _request_and_parse_report(page, base_url, report_date, operator_name)
            return data
        except Exception as exc:
            shot = screenshots_dir / f"error_{report_date.isoformat()}.png"
            await page.screenshot(path=str(shot), full_page=True)
            logger.error("Screenshot saved: %s", shot)
            raise RuntimeError(f"HDE report failed: {exc}") from exc
        finally:
            await browser.close()


async def _login(page: Page, base_url: str, login: str, password: str) -> None:
    logger.info("Logging in to %s", base_url)
    await page.goto(base_url, wait_until="load", timeout=30_000)

    # Fill email field (placeholder "Э-почта")
    await page.locator('input[type="email"], input[type="text"]').first.fill(login)
    # Fill password field
    await page.locator('input[type="password"]').fill(password)
    # Click "Войти" and wait for actual page navigation
    async with page.expect_navigation(wait_until="load", timeout=20_000):
        await page.locator('button:has-text("Войти"), button[type="submit"]').click()

    logger.info("After login, URL: %s", page.url)

    # If the login button is still visible — credentials are wrong
    if await page.locator('button:has-text("Войти")').count() > 0:
        raise RuntimeError(
            "Login failed — still on login page. "
            "Check HDE_API_EMAIL and HDE_REPORT_PASSWORD in .env"
        )
    logger.info("Login successful")


async def _request_and_parse_report(
    page: Page,
    base_url: str,
    report_date: date,
    operator_name: str,
) -> OperatorReportData:
    report_url = f"{base_url}/ru/review/staff_user_report/"
    logger.info("Navigating to %s", report_url)
    await page.goto(report_url, wait_until="load", timeout=30_000)

    # Set flatpickr date inputs using Date objects (avoids string-format/timezone issues).
    # We filter only inputs that actually have ._flatpickr attached, then set both to
    # the same calendar day — matching what a user sees when clicking a single date.
    y, m, d = report_date.year, report_date.month, report_date.day
    fp_debug = await page.evaluate(
        """([y, m, d]) => {
            // Collect all inputs that have a flatpickr instance attached
            const allInputs = Array.from(document.querySelectorAll('input'));
            const fpInputs  = allInputs.filter(inp => !!inp._flatpickr);

            const info = fpInputs.map((inp, i) => ({
                i,
                name:        inp.name        || '',
                id:          inp.id          || '',
                placeholder: inp.placeholder || '',
                valueBefore: inp.value       || '',
                enableTime:  inp._flatpickr.config.enableTime,
                dateFormat:  inp._flatpickr.config.dateFormat,
            }));

            // JS Date: month is 0-indexed
            const dt = new Date(y, m - 1, d);

            const dtEnd = new Date(y, m - 1, d, 23, 59);
            if (fpInputs[0]) fpInputs[0]._flatpickr.setDate(dt, true);
            if (fpInputs[1]) fpInputs[1]._flatpickr.setDate(dtEnd, true);

            return {
                total: fpInputs.length,
                info,
                afterSet: fpInputs.map(inp => inp.value),
            };
        }""",
        [y, m, d],
    )
    logger.info("Flatpickr debug → %s", fp_debug)

    # ADJUST: ensure "Дата закрытия" checkbox is checked
    checkbox = page.locator('input[type="checkbox"]').filter(has_text="")
    # Try to find by nearby label text
    close_date_cb = page.locator('label:has-text("Дата закрытия") input[type="checkbox"], '
                                  'input[type="checkbox"][id*="close"], '
                                  'input[type="checkbox"][name*="close"]')
    if await close_date_cb.count():
        if not await close_date_cb.is_checked():
            await close_date_cb.click()
    else:
        logger.warning("'Дата закрытия' checkbox not found — check selectors")

    # Click "Запросить"
    # ADJUST: find the primary submit button
    for sel in [
        'button:has-text("Запросить")',
        'input[value="Запросить"]',
        'button[type="submit"]',
        '.btn-primary',
    ]:
        if await page.locator(sel).count():
            await page.locator(sel).first.click()
            break
    else:
        raise RuntimeError("'Запросить' button not found — check selectors in hde_playwright.py")

    logger.info("Report requested, waiting for generation…")
    await page.wait_for_timeout(5_000)  # HDE generates in 1-3 s; 5 s is safe

    # Click "Показать" on the first (newest) report in the list
    show_btn = page.locator('a:has-text("Показать"), button:has-text("Показать")').first
    await show_btn.wait_for(state="visible", timeout=15_000)
    await show_btn.click()

    # Results appear inline via AJAX — wait for the results table (the one with "Сотрудник")
    await page.wait_for_selector('th:has-text("Сотрудник")', timeout=15_000)

    return await _parse_table(page, operator_name)


async def _parse_table(page: Page, operator_name: str) -> OperatorReportData:
    """
    Find the operator row in the results table.
    Dynamically resolves column indices from the header row.
    """
    # Find the results table — the one that contains "Сотрудник" header
    tables = page.locator("table")
    table = None
    headers: list[str] = []
    for i in range(await tables.count()):
        t = tables.nth(i)
        raw = await t.locator("thead tr th").all_text_contents()
        stripped = [h.strip() for h in raw]
        if any("сотрудник" in h.lower() for h in stripped):
            table = t
            headers = stripped
            break

    if table is None:
        # Log all tables for debugging
        all_h = []
        for i in range(await tables.count()):
            raw = await tables.nth(i).locator("thead tr th").all_text_contents()
            all_h.append([h.strip() for h in raw])
        raise RuntimeError(f"Results table not found. All tables headers: {all_h}")

    logger.debug("Results table headers: %s", headers)

    def find_col(keywords: list[str]) -> int | None:
        for i, h in enumerate(headers):
            h_lower = h.lower()
            if any(kw.lower() in h_lower for kw in keywords):
                return i
        return None

    # "Сотрудник" column
    col_name = find_col(["сотрудник", "оператор", "employee"])
    # "Количество заявок"
    col_tickets = find_col(["количество заявок", "кол-во заявок", "tickets"])
    # "Среднее время на выполнение заявки"
    col_avg = find_col(["среднее время на выполнение", "avg", "среднее время выполн"])

    if col_name is None:
        raise RuntimeError(f"Cannot find 'Сотрудник' column. Headers: {headers}")
    if col_tickets is None:
        raise RuntimeError(f"Cannot find 'Количество заявок' column. Headers: {headers}")
    if col_avg is None:
        raise RuntimeError(f"Cannot find 'Среднее время на выполнение' column. Headers: {headers}")

    # Scan body rows for the operator
    rows = await table.locator("tbody tr").all()
    name_lower = operator_name.strip().lower()

    for row in rows:
        cells = await row.locator("td").all_text_contents()
        cells = [c.strip() for c in cells]
        if not cells:
            continue
        if len(cells) <= max(col_name, col_tickets, col_avg):
            continue
        if cells[col_name].lower() == name_lower:
            logger.info(
                "Found operator '%s': tickets=%s avg_time=%s",
                cells[col_name], cells[col_tickets], cells[col_avg],
            )
            return OperatorReportData(
                operator=cells[col_name],
                tickets=cells[col_tickets],
                avg_time=cells[col_avg],
            )

    # Collect available names for a helpful error message
    available = [
        (await row.locator("td").nth(col_name).text_content() or "").strip()
        for row in rows
    ]
    raise RuntimeError(
        f"Operator '{operator_name}' not found in report table. "
        f"Available: {available}"
    )
