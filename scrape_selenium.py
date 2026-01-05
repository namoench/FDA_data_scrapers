#!/usr/bin/env python3
"""
Scrape FDA OII FOIA Electronic Reading Room using Selenium.
"""

import json
import csv
import re
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

# Configuration
OUTPUT_DIR = Path(__file__).parent / "data"
URL = "https://www.fda.gov/about-fda/office-inspections-and-investigations/oii-foia-electronic-reading-room"
PAGE_SIZE = 100

# Set up logging
OUTPUT_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(OUTPUT_DIR / "scrape_selenium.log")
    ]
)
logger = logging.getLogger(__name__)


def scrape_reading_room(max_pages: int = None, headless: bool = True):
    """Scrape all records from the FDA OII FOIA Electronic Reading Room."""

    logger.info("Initializing Chrome driver...")
    options = Options()
    options.binary_location = "/usr/bin/chromium-browser"  # Use system Chromium
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--single-process")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    # Use manually installed chromedriver
    service = Service("/usr/local/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    wait = WebDriverWait(driver, 30)
    all_records = []

    try:
        logger.info(f"Loading page: {URL}")
        driver.get(URL)

        # Wait for DataTables to initialize
        wait.until(EC.presence_of_element_located((By.ID, "datatable")))
        time.sleep(5)  # Let JS fully initialize
        logger.info("Page loaded, DataTables initialized")

        # Change page size to 100 using JavaScript and DataTables API
        logger.info(f"Setting page size to {PAGE_SIZE}...")
        try:
            # Try using DataTables API directly
            driver.execute_script(f"""
                // Method 1: Try DataTables API
                if (typeof jQuery !== 'undefined' && jQuery.fn.DataTable) {{
                    var table = jQuery('#datatable').DataTable();
                    table.page.len({PAGE_SIZE}).draw();
                }} else {{
                    // Method 2: Direct select change with multiple events
                    var select = document.querySelector('select[name="datatable_length"]');
                    if (select) {{
                        select.value = '{PAGE_SIZE}';
                        var events = ['change', 'input'];
                        events.forEach(function(eventType) {{
                            select.dispatchEvent(new Event(eventType, {{ bubbles: true }}));
                        }});
                    }}
                }}
            """)
            time.sleep(8)  # Wait for table reload

            # Verify page size
            rows = driver.find_elements(By.CSS_SELECTOR, "#datatable tbody tr")
            logger.info(f"Rows visible after page size change: {len(rows)}")
        except Exception as e:
            logger.warning(f"Could not set page size: {e}")

        # Get total records
        try:
            info_elem = driver.find_element(By.ID, "datatable_info")
            info_text = info_elem.text
            total_match = re.search(r"of ([\d,]+) entries", info_text)
            total_records = int(total_match.group(1).replace(",", "")) if total_match else 0
            logger.info(f"Total records: {total_records:,}")
        except Exception as e:
            logger.warning(f"Could not get total records: {e}")
            total_records = 0

        total_pages = (total_records + PAGE_SIZE - 1) // PAGE_SIZE if total_records else 100
        if max_pages:
            total_pages = min(total_pages, max_pages)
        logger.info(f"Pages to scrape: {total_pages}")

        current_page = 1

        while True:
            logger.info(f"Scraping page {current_page}/{total_pages}...")

            # Wait for table body
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#datatable tbody tr")))
            time.sleep(1)

            # Get all rows
            rows = driver.find_elements(By.CSS_SELECTOR, "#datatable tbody tr")
            page_records = []

            for row in rows:
                try:
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if len(cells) < 8:
                        continue

                    record = {
                        "record_date": cells[0].text.strip(),
                        "company_name": cells[1].text.strip(),
                        "fei_number": cells[2].text.strip(),
                    }

                    # Get record type and PDF link
                    try:
                        link = cells[3].find_element(By.TAG_NAME, "a")
                        record["record_type"] = link.text.strip()
                        href = link.get_attribute("href")
                        if href:
                            media_match = re.search(r"/media/(\d+)", href)
                            if media_match:
                                record["pdf_media_id"] = media_match.group(1)
                                record["pdf_url"] = href if href.startswith("http") else f"https://www.fda.gov{href}"
                    except NoSuchElementException:
                        record["record_type"] = cells[3].text.strip()

                    record["state"] = cells[4].text.strip()
                    record["country"] = cells[5].text.strip()
                    record["establishment_type"] = cells[6].text.strip()
                    record["publish_date"] = cells[7].text.strip()

                    page_records.append(record)

                except Exception as e:
                    logger.warning(f"Error parsing row: {e}")
                    continue

            all_records.extend(page_records)
            logger.info(f"  Extracted {len(page_records)} records (total: {len(all_records):,})")

            # Check if we've reached the limit
            if max_pages and current_page >= max_pages:
                logger.info("Reached page limit")
                break

            # Check for next button
            try:
                next_button = driver.find_element(By.CSS_SELECTOR, "#datatable_next")
                if "disabled" in (next_button.get_attribute("class") or ""):
                    logger.info("Next button disabled, no more pages")
                    break

                # Store first company of current page for comparison
                first_company = page_records[0]["company_name"] if page_records else ""

                # Click next using JavaScript
                driver.execute_script("document.querySelector('#datatable_next').click();")
                time.sleep(3)

                # Wait for page to change
                for _ in range(20):
                    time.sleep(0.5)
                    new_rows = driver.find_elements(By.CSS_SELECTOR, "#datatable tbody tr")
                    if new_rows:
                        new_first = new_rows[0].find_elements(By.TAG_NAME, "td")
                        if len(new_first) > 1 and new_first[1].text.strip() != first_company:
                            break
                else:
                    logger.warning("Page didn't change")
                    break

            except NoSuchElementException:
                logger.info("No next button found, reached end")
                break
            except Exception as e:
                logger.warning(f"Error navigating to next page: {e}")
                break

            current_page += 1

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
    finally:
        driver.quit()

    logger.info(f"Scraping complete: {len(all_records):,} records")
    return all_records


def save_records(records: list, filter_483: bool = True):
    """Save records to JSON and CSV files."""

    # Filter for 483 records if requested
    if filter_483:
        records_483 = [r for r in records if r.get("record_type") in ("483", "Amended 483")]
        logger.info(f"Filtered to {len(records_483):,} Form 483 records")
    else:
        records_483 = records

    # Save all records
    json_path = OUTPUT_DIR / "reading_room_metadata.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved all records to: {json_path}")

    csv_path = OUTPUT_DIR / "reading_room_metadata.csv"
    if records:
        fieldnames = list(records[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)
    logger.info(f"Saved CSV to: {csv_path}")

    # Save 483-only records
    if filter_483 and records_483:
        json_483_path = OUTPUT_DIR / "reading_room_483_only.json"
        with open(json_483_path, "w", encoding="utf-8") as f:
            json.dump(records_483, f, indent=2, ensure_ascii=False)

        csv_483_path = OUTPUT_DIR / "reading_room_483_only.csv"
        fieldnames = list(records_483[0].keys())
        with open(csv_483_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records_483)
        logger.info(f"Saved 483-only CSV to: {csv_483_path}")

    return records_483


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Scrape FDA OII FOIA Electronic Reading Room (Selenium)")
    parser.add_argument("--test", action="store_true", help="Test mode: scrape only first 2 pages")
    parser.add_argument("--max-pages", type=int, help="Maximum pages to scrape")
    parser.add_argument("--visible", action="store_true", help="Run browser in visible mode")
    args = parser.parse_args()

    max_pages = 2 if args.test else args.max_pages

    logger.info("=" * 60)
    logger.info("FDA OII FOIA Electronic Reading Room Scraper (Selenium)")
    logger.info("=" * 60)

    records = scrape_reading_room(
        max_pages=max_pages,
        headless=not args.visible
    )

    records_483 = save_records(records, filter_483=True)

    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total records scraped: {len(records):,}")
    logger.info(f"Form 483 records: {len(records_483):,}")
    logger.info(f"Output directory: {OUTPUT_DIR.absolute()}")

    return records


if __name__ == "__main__":
    main()
