#!/usr/bin/env python3
"""
Scrape FDA Warning Letters using Playwright.
Target: https://www.accessdata.fda.gov/scripts/warningletters/wlSearchResult.cfm
"""

import json
import csv
import re
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse, parse_qs

from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PlaywrightTimeout


# Configuration
OUTPUT_DIR = Path(__file__).parent / "data" / "warning_letters"
BASE_URL = "https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/warning-letters"
RATE_LIMIT_DELAY = 2.0  # seconds between page loads

# Set up logging
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(OUTPUT_DIR / "scrape_warning_letters.log")
    ]
)
logger = logging.getLogger(__name__)


def get_year_options(page: Page) -> Dict[str, str]:
    """
    Get all available year options from the Year dropdown filter.

    Returns dict mapping year text to option value (e.g., {'2025': '2025', '2024': '2024', ...})
    """
    logger.info("Looking for Year dropdown filter...")

    # The FDA warning letters page uses this specific ID for the year dropdown
    year_selector = "#field_letter_issue_datetime_2"

    year_options = {}

    try:
        dropdown = page.locator(year_selector).first
        if dropdown.count() > 0:
            options = dropdown.locator("option").all()
            for opt in options:
                text = opt.text_content().strip()
                value = opt.get_attribute("value") or ""
                logger.info(f"  Option: text='{text}', value='{value}'")

                # Check if it looks like a year (4 digits between 2000-2030)
                if text.isdigit() and 2000 <= int(text) <= 2030:
                    year_options[text] = value
    except Exception as e:
        logger.warning(f"Error getting year options: {e}")

    if year_options:
        years_list = sorted(year_options.keys(), key=int, reverse=True)
        logger.info(f"Found {len(year_options)} year options: {years_list[0]} to {years_list[-1]}")
    else:
        logger.warning("No year options found in dropdown")

    return year_options


def select_year_filter(page: Page, year_text: str, option_value: str) -> bool:
    """
    Select a year from the year dropdown filter and wait for table to reload.

    Args:
        page: Playwright page object
        year_text: The year text (e.g., "2024")
        option_value: The actual option value attribute to select

    Returns True if successful, False otherwise.
    """
    logger.info(f"Selecting year filter: {year_text} (option value='{option_value}')")

    year_selector = "#field_letter_issue_datetime_2"

    try:
        dropdown = page.locator(year_selector).first
        if dropdown.count() == 0:
            logger.warning(f"Year dropdown not found: {year_selector}")
            return False

        # Get current row count before changing
        try:
            old_rows = page.locator("#datatable tbody tr").all()
            old_count = len(old_rows)
        except:
            old_count = 0

        # Select the year using the actual option value
        value_to_use = option_value if option_value else year_text
        logger.info(f"Setting dropdown value to: '{value_to_use}'")

        # Use JavaScript to set value and trigger change event
        page.evaluate(f"""
            const dropdown = document.querySelector('{year_selector}');
            if (dropdown) {{
                dropdown.value = '{value_to_use}';
                dropdown.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
        """)

        logger.info(f"Selected year {year_text}, waiting for table reload...")

        # Wait for table to reload via AJAX
        time.sleep(3)  # Initial wait for AJAX

        # Scroll down to see the table
        table = page.locator("#datatable").first
        if table.count() > 0:
            table.scroll_into_view_if_needed()

        # Wait for table rows to appear
        page.wait_for_selector("#datatable tbody tr", timeout=15000)
        time.sleep(1)  # Let JS settle

        # Verify table changed (or at least loaded)
        new_rows = page.locator("#datatable tbody tr").all()
        new_count = len(new_rows)
        logger.info(f"Table reloaded: {new_count} rows visible")

        # Debug: Log the info text showing record count
        try:
            info_elem = page.locator("#datatable_info").first
            if info_elem.count() > 0:
                info_text = info_elem.text_content()
                logger.info(f"DataTable info: {info_text}")
        except:
            pass

        return True

    except Exception as e:
        logger.warning(f"Error selecting year {year_text}: {e}")
        return False


def explore_search_interface(page: Page) -> Dict:
    """
    Explore the warning letters search interface to understand available filters and controls.

    Returns dict with interface info.
    """
    logger.info("Exploring search interface...")

    interface_info = {
        "url": page.url,
        "title": page.title(),
        "forms": [],
        "search_fields": [],
        "date_filters": [],
        "pagination": {}
    }

    # Look for forms
    forms = page.locator("form").all()
    logger.info(f"Found {len(forms)} form(s)")

    for i, form in enumerate(forms):
        form_info = {
            "index": i,
            "action": form.get_attribute("action") or "",
            "method": form.get_attribute("method") or "",
            "inputs": []
        }

        # Get all inputs in this form
        inputs = form.locator("input, select, textarea").all()
        for input_elem in inputs:
            input_info = {
                "type": input_elem.get_attribute("type") or "text",
                "name": input_elem.get_attribute("name") or "",
                "id": input_elem.get_attribute("id") or "",
                "value": input_elem.get_attribute("value") or ""
            }

            # For select elements, get options
            if input_elem.evaluate("el => el.tagName").lower() == "select":
                input_info["type"] = "select"
                options = input_elem.locator("option").all()
                input_info["options"] = [opt.text_content() for opt in options]

            form_info["inputs"].append(input_info)

        interface_info["forms"].append(form_info)

    # Look for date fields specifically
    date_inputs = page.locator("input[type='text'][name*='date'], input[type='text'][name*='Date'], input[type='date']").all()
    for date_input in date_inputs:
        interface_info["date_filters"].append({
            "name": date_input.get_attribute("name") or "",
            "id": date_input.get_attribute("id") or "",
            "placeholder": date_input.get_attribute("placeholder") or ""
        })

    # Look for pagination elements
    pagination_links = page.locator("a[href*='page'], a[href*='Page']").all()
    interface_info["pagination"]["links_found"] = len(pagination_links)

    # Look for results table
    tables = page.locator("table").all()
    interface_info["tables_found"] = len(tables)

    logger.info(f"Interface exploration complete:")
    logger.info(f"  Forms: {len(interface_info['forms'])}")
    logger.info(f"  Date filters: {len(interface_info['date_filters'])}")
    logger.info(f"  Tables: {interface_info['tables_found']}")

    return interface_info


def extract_warning_letters_from_page(page: Page) -> List[Dict]:
    """
    Extract warning letter metadata from the current page.

    Returns list of warning letter records.
    """
    records = []

    # FDA Warning Letters uses DataTables with #datatable ID
    table_selectors = [
        "#datatable",
        "table.dataTable",
        "table.lcds-datatable",
        "table",
    ]

    table = None
    for selector in table_selectors:
        try:
            table = page.locator(selector).first
            if table.count() > 0:
                # Scroll table into view
                table.scroll_into_view_if_needed()
                time.sleep(0.5)
                logger.debug(f"Found table with selector: {selector}")
                break
        except:
            continue

    if not table or table.count() == 0:
        logger.warning("No table found on page")
        return records

    # Get data rows from tbody (skip header rows in thead)
    rows = table.locator("tbody tr").all()
    logger.info(f"Found {len(rows)} data rows in table tbody")

    # Debug: Log first row content to understand table structure
    if rows:
        first_row = rows[0]
        first_row_text = first_row.text_content()
        logger.info(f"First row text: {first_row_text[:200] if first_row_text else 'EMPTY'}...")
        cells = first_row.locator("td").all()
        logger.info(f"First row has {len(cells)} td cells")

    # If no tbody rows found, fall back to all tr and skip header
    if not rows:
        all_rows = table.locator("tr").all()
        logger.info(f"Fallback: Found {len(all_rows)} total rows in table")
        # Skip first row (header)
        rows = all_rows[1:] if len(all_rows) > 1 else []

    # Process data rows
    for i, row in enumerate(rows):
        try:
            cells = row.locator("td").all()
            if len(cells) < 3:  # Need at least a few columns
                continue

            record = {}

            # Extract text from each cell
            cell_texts = [cell.text_content().strip() for cell in cells]

            # Try to find links in cells
            links = row.locator("a").all()
            link_data = []
            for link in links:
                href = link.get_attribute("href") or ""
                text = link.text_content().strip()
                if href:
                    full_url = urljoin(page.url, href)
                    link_data.append({
                        "text": text,
                        "url": full_url
                    })

            # Actual column order (from FDA website):
            # 0: Posted Date, 1: Issue Date, 2: Company, 3: Issuing Office,
            # 4: Subject, 5-7: Response/Closeout letter columns (often empty)
            if len(cell_texts) >= 1:
                record["posted_date"] = cell_texts[0]
            if len(cell_texts) >= 2:
                record["issue_date"] = cell_texts[1]
            if len(cell_texts) >= 3:
                record["company_name"] = cell_texts[2]
            if len(cell_texts) >= 4:
                record["issuing_office"] = cell_texts[3]
            if len(cell_texts) >= 5:
                record["subject"] = cell_texts[4]
            if len(cell_texts) >= 6 and cell_texts[5]:
                record["response_letter_date"] = cell_texts[5]
            if len(cell_texts) >= 7 and cell_texts[6]:
                record["closeout_letter_date"] = cell_texts[6]

            # Add all cell texts for later analysis
            record["raw_cells"] = cell_texts

            # Add links
            if link_data:
                record["links"] = link_data
                # Try to identify letter URL and response/closeout URLs
                for i, link in enumerate(link_data):
                    link_lower = link["text"].lower()
                    # First link is usually the company name / main warning letter
                    if i == 0:
                        record["letter_url"] = link["url"]
                    # Look for response letter
                    if "response" in link_lower:
                        record["response_letter_url"] = link["url"]
                    # Look for closeout letter
                    if "closeout" in link_lower or "close-out" in link_lower:
                        record["closeout_letter_url"] = link["url"]

            records.append(record)

        except Exception as e:
            logger.warning(f"Error parsing row {i}: {e}")
            continue

    logger.info(f"Extracted {len(records)} records from page")
    return records


def try_download_xlsx(page: Page) -> Optional[str]:
    """
    Try to find and click the XLSX download button.

    Returns path to downloaded file if successful, None otherwise.
    """
    logger.info("Looking for XLSX download button...")

    download_selectors = [
        "a:has-text('Download XLSX')",
        "a:has-text('Download')",
        "button:has-text('Download XLSX')",
        "button:has-text('Download')",
        "a:has-text('Export')",
        "button:has-text('Export')",
    ]

    for selector in download_selectors:
        try:
            button = page.locator(selector).first
            if button.count() > 0:
                logger.info(f"Found download button: {selector}")
                # Set up download handler
                with page.expect_download() as download_info:
                    button.click()
                    time.sleep(2)

                download = download_info.value
                download_path = OUTPUT_DIR / download.suggested_filename
                download.save_as(download_path)
                logger.info(f"Downloaded file to: {download_path}")
                return str(download_path)
        except Exception as e:
            logger.debug(f"Could not use download button {selector}: {e}")
            continue

    logger.warning("Could not find download button")
    return None


def navigate_to_next_page(page: Page) -> bool:
    """
    Navigate to the next page of results.

    Returns True if successfully navigated, False if no more pages.
    """
    try:
        # First, scroll to the pagination area to ensure it's visible
        pagination = page.locator(".dataTables_paginate").first
        if pagination.count() > 0:
            pagination.scroll_into_view_if_needed()
            time.sleep(0.5)

        # Debug: Log pagination HTML structure
        try:
            pag_html = page.evaluate("document.querySelector('.dataTables_paginate')?.innerHTML || 'NOT FOUND'")
            logger.debug(f"Pagination HTML: {pag_html[:300]}...")
        except:
            pass

        # Try multiple selectors for the Next button
        next_selectors = [
            "a.paginate_button.next:not(.disabled)",
            "#datatable_next:not(.disabled)",
            ".dataTables_paginate a.next:not(.disabled)",
            "a:has-text('Next'):not(.disabled)",
            ".paginate_button:has-text('Next'):not(.disabled)",
        ]

        next_link = None
        for selector in next_selectors:
            try:
                elem = page.locator(selector).first
                if elem.count() > 0:
                    next_link = elem
                    logger.info(f"Found Next button with selector: {selector}")
                    break
            except:
                continue

        if next_link is None:
            # Try to get all pagination buttons for debugging
            all_buttons = page.locator(".paginate_button").all()
            logger.info(f"Found {len(all_buttons)} paginate_button elements")
            for btn in all_buttons[:5]:
                btn_text = btn.text_content()
                btn_class = btn.get_attribute("class")
                logger.info(f"  Button: '{btn_text}', class='{btn_class}'")

            logger.info("No clickable Next button found")
            return False

        # Check if disabled (double-check)
        classes = next_link.get_attribute("class") or ""
        if "disabled" in classes.lower():
            logger.info("Next button is disabled - no more pages")
            return False

        # Get current first row text to verify page changed
        try:
            first_row = page.locator("#datatable tbody tr").first
            old_text = first_row.text_content()[:50] if first_row.count() > 0 else ""
        except:
            old_text = ""

        # Scroll to and click next - ensure element is visible
        logger.info("Scrolling to and clicking Next button...")
        next_link.scroll_into_view_if_needed()
        time.sleep(0.5)  # Let scroll complete

        # Use JavaScript click as fallback since element might be styled oddly
        try:
            next_link.click(timeout=5000)
        except:
            # Fallback: use JavaScript click
            logger.info("Direct click failed, using JavaScript click...")
            page.evaluate("""
                const nextBtn = document.querySelector('#datatable_next a, #datatable_next');
                if (nextBtn) nextBtn.click();
            """)

        # Wait for table to update (content should change)
        for _ in range(15):
            time.sleep(0.5)
            try:
                new_first = page.locator("#datatable tbody tr").first
                new_text = new_first.text_content()[:50] if new_first.count() > 0 else ""
                if new_text and new_text != old_text:
                    logger.info("Page content changed - navigation successful")
                    time.sleep(0.5)
                    return True
            except:
                continue

        # Fallback: just wait and assume it worked
        logger.info("Content didn't change detectably, assuming success")
        time.sleep(RATE_LIMIT_DELAY)
        return True

    except Exception as e:
        logger.warning(f"Error navigating to next page: {e}")
        return False


def reset_to_first_page(page: Page) -> bool:
    """
    Reset pagination to the first page of results.

    Returns True if successful or already on first page, False otherwise.
    """
    try:
        # Look for "First" or page 1 link
        first_selectors = [
            "a:has-text('First')",
            "a:has-text('1'):not([class*='disabled'])",
            "a.paginate_button:has-text('1')",
            "#datatable_first",
        ]

        for selector in first_selectors:
            try:
                first_link = page.locator(selector).first
                if first_link.count() > 0:
                    classes = first_link.get_attribute("class") or ""
                    # If already on first page, the button may be disabled
                    if "disabled" in classes.lower() or "current" in classes.lower():
                        logger.debug("Already on first page")
                        return True
                    first_link.click()
                    time.sleep(RATE_LIMIT_DELAY)
                    return True
            except:
                continue

        # If no explicit first button, we might already be on page 1
        return True
    except Exception as e:
        logger.warning(f"Error resetting to first page: {e}")
        return False


def scrape_warning_letters(
    start_year: int = 2007,
    end_year: Optional[int] = None,
    max_pages: Optional[int] = None,
    headless: bool = True,
    explore_only: bool = False,
    resume: bool = True
) -> List[Dict]:
    """
    Main scraping function. Iterates through ALL years in the year dropdown.

    Args:
        start_year: Earliest year to scrape (default 2007)
        end_year: Latest year to scrape (default current year)
        max_pages: Maximum pages to scrape per year (None for all)
        headless: Run browser in headless mode
        explore_only: Only explore interface, don't scrape data
        resume: Resume from previous progress if available

    Returns:
        List of warning letter records
    """
    all_records = []
    total_pages_scraped = 0

    # Load existing progress if resuming
    log_path = OUTPUT_DIR / "scrape_log.json"
    metadata_path = OUTPUT_DIR / "warning_letters_metadata.json"

    if resume and log_path.exists() and metadata_path.exists():
        try:
            with open(log_path, "r") as f:
                scrape_log = json.load(f)
            with open(metadata_path, "r") as f:
                all_records = json.load(f)
            logger.info(f"Resuming from previous run: {len(all_records)} records already collected")
            logger.info(f"Years completed: {scrape_log.get('years_completed', [])}")
        except Exception as e:
            logger.warning(f"Could not load previous progress: {e}")
            scrape_log = None
    else:
        scrape_log = None

    # Initialize or update scrape log
    if scrape_log is None:
        scrape_log = {
            "started_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
            "years_completed": [],
            "years_remaining": [],
            "total_metadata_collected": 0,
            "total_pages_scraped": 0,
            "errors": [],
            "failed_downloads": []
        }

    with sync_playwright() as p:
        # Launch browser
        logger.info("Launching browser...")
        browser = p.firefox.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()

        # Set extra HTTP headers
        page.set_extra_http_headers({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        })

        try:
            # Navigate to search page
            logger.info(f"Loading {BASE_URL}")
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)  # Let page fully load and JS initialize

            # Explore interface
            interface_info = explore_search_interface(page)

            # Save interface info for reference
            interface_path = OUTPUT_DIR / "interface_exploration.json"
            with open(interface_path, "w", encoding="utf-8") as f:
                json.dump(interface_info, f, indent=2, ensure_ascii=False)
            logger.info(f"Saved interface info to {interface_path}")

            if explore_only:
                logger.info("Exploration complete (explore_only=True)")
                return []

            # Take screenshot of initial page
            screenshot_path = OUTPUT_DIR / "initial_page.png"
            page.screenshot(path=str(screenshot_path))
            logger.info(f"Saved screenshot to {screenshot_path}")

            # Scrape ALL warning letters without year filter
            # Year filter via dropdown doesn't work reliably - scrape everything
            logger.info("Scraping ALL warning letters (no year filter)")

            # Wait for table to load and scroll into view
            page.wait_for_selector("#datatable tbody tr", timeout=15000)
            table = page.locator("#datatable").first
            if table.count() > 0:
                table.scroll_into_view_if_needed()

            # Note: Page size change was disabled because it broke pagination
            # Using default page size (10 records per page)

            # Get total count from info element
            info_elem = page.locator("#datatable_info").first
            if info_elem.count() > 0:
                info_text = info_elem.text_content()
                logger.info(f"Table state: {info_text}")

            # Take screenshot before scraping
            debug_dir = OUTPUT_DIR / "debug"
            debug_dir.mkdir(exist_ok=True)
            page.screenshot(path=str(debug_dir / "before_scrape.png"), full_page=True)

            # Scrape all pages
            page_num = 1
            consecutive_empty_pages = 0
            MAX_EMPTY_PAGES = 3

            while True:
                logger.info(f"Scraping page {page_num}...")

                try:
                    records = extract_warning_letters_from_page(page)
                    all_records.extend(records)
                    total_pages_scraped += 1

                    logger.info(f"  Page {page_num}: {len(records)} records (total: {len(all_records):,})")

                    # Track empty pages
                    if len(records) == 0:
                        consecutive_empty_pages += 1
                        logger.warning(f"  Empty page ({consecutive_empty_pages}/{MAX_EMPTY_PAGES})")
                        if consecutive_empty_pages >= MAX_EMPTY_PAGES:
                            logger.info(f"Stopping: {MAX_EMPTY_PAGES} consecutive empty pages")
                            break
                    else:
                        consecutive_empty_pages = 0

                    # Save progress every 10 pages
                    if page_num % 10 == 0:
                        scrape_log["last_updated"] = datetime.now().isoformat()
                        scrape_log["total_metadata_collected"] = len(all_records)
                        scrape_log["total_pages_scraped"] = total_pages_scraped
                        save_progress(all_records, scrape_log)

                except Exception as e:
                    error_msg = f"Error extracting records from page {page_num}: {e}"
                    logger.error(error_msg)
                    scrape_log["errors"].append({
                        "page": page_num,
                        "error": str(e),
                        "timestamp": datetime.now().isoformat()
                    })

                # Check page limit
                if max_pages and page_num >= max_pages:
                    logger.info(f"Reached max pages limit ({max_pages})")
                    break

                # Try to navigate to next page
                if not navigate_to_next_page(page):
                    logger.info("No more pages available")
                    break

                page_num += 1

            # Update final log
            scrape_log["last_updated"] = datetime.now().isoformat()
            scrape_log["total_metadata_collected"] = len(all_records)
            scrape_log["total_pages_scraped"] = total_pages_scraped
            logger.info(f"Scraping complete: {len(all_records):,} records from {total_pages_scraped} pages")

        except Exception as e:
            error_msg = f"Fatal error during scraping: {e}"
            logger.error(error_msg)
            scrape_log["errors"].append({
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            })
            # Save what we have before raising
            save_progress(all_records, scrape_log)
            raise

        finally:
            scrape_log["last_updated"] = datetime.now().isoformat()
            scrape_log["total_metadata_collected"] = len(all_records)
            browser.close()

    logger.info(f"Scraping complete: {len(all_records)} total records from {total_pages_scraped} pages")

    # Final save
    save_progress(all_records, scrape_log)

    return all_records


def save_progress(records: List[Dict], scrape_log: Dict):
    """Save current progress to files."""

    # Save metadata JSON
    metadata_path = OUTPUT_DIR / "warning_letters_metadata.json"
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(records)} records to {metadata_path}")

    # Save scrape log
    log_path = OUTPUT_DIR / "scrape_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(scrape_log, f, indent=2, ensure_ascii=False)
    logger.debug(f"Saved scrape log to {log_path}")

    # Save CSV if we have records
    if records:
        csv_path = OUTPUT_DIR / "warning_letters_metadata.csv"

        # Get all unique keys from all records
        all_keys = set()
        for record in records:
            all_keys.update(record.keys())

        # Remove complex fields from CSV (keep them in JSON)
        csv_keys = sorted([k for k in all_keys if k not in ["raw_cells", "links"]])

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=csv_keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
        logger.info(f"Saved CSV to {csv_path}")


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Scrape FDA Warning Letters (Playwright)")
    parser.add_argument("--start-year", type=int, default=2007, help="Earliest year to scrape (default: 2007)")
    parser.add_argument("--end-year", type=int, help="Latest year to scrape (default: current year)")
    parser.add_argument("--test", action="store_true", help="Test mode: scrape only first 2 pages per year")
    parser.add_argument("--max-pages", type=int, help="Maximum pages to scrape per year")
    parser.add_argument("--visible", action="store_true", help="Run browser in visible mode")
    parser.add_argument("--explore", action="store_true", help="Only explore interface, don't scrape")
    parser.add_argument("--no-resume", action="store_true", help="Start fresh, don't resume from previous progress")
    args = parser.parse_args()

    max_pages = 2 if args.test else args.max_pages

    logger.info("=" * 70)
    logger.info("FDA Warning Letters Scraper (Playwright)")
    logger.info("=" * 70)
    logger.info(f"Year range: {args.start_year} - {args.end_year or 'present'}")
    logger.info(f"Resume mode: {not args.no_resume}")

    records = scrape_warning_letters(
        start_year=args.start_year,
        end_year=args.end_year,
        max_pages=max_pages,
        headless=not args.visible,
        explore_only=args.explore,
        resume=not args.no_resume
    )

    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info(f"Total records scraped: {len(records):,}")
    logger.info(f"Output directory: {OUTPUT_DIR.absolute()}")

    return records


if __name__ == "__main__":
    main()
