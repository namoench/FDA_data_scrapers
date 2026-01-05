#!/usr/bin/env python3
"""
Download FDA Warning Letter HTML pages using Playwright.

Uses headless browser to render JavaScript and get full page content.
"""

import json
import os
import re
import time
import logging
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright

# Configuration
OUTPUT_DIR = Path("data/warning_letters/html")
METADATA_FILE = Path("data/warning_letters/warning_letters_metadata.json")
PROGRESS_FILE = Path("data/warning_letters/download_progress.json")
RATE_LIMIT_DELAY = 2.0  # seconds between requests

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/warning_letters/download_letters.log")
    ]
)
logger = logging.getLogger(__name__)

# Create output directory
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str, max_length: int = 100) -> str:
    """Create a safe filename from company name."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', '_', name)
    name = re.sub(r'[^\w\-_.]', '', name)
    if len(name) > max_length:
        name = name[:max_length]
    return name


def load_progress() -> dict:
    """Load download progress from file."""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
    return {
        "downloaded": [],
        "failed": [],
        "started_at": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat()
    }


def save_progress(progress: dict):
    """Save download progress to file."""
    progress["last_updated"] = datetime.now().isoformat()
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2)


def main():
    """Main download function."""
    logger.info("=" * 70)
    logger.info("FDA Warning Letters HTML Downloader (Playwright)")
    logger.info("=" * 70)

    # Load metadata
    if not METADATA_FILE.exists():
        logger.error(f"Metadata file not found: {METADATA_FILE}")
        return

    with open(METADATA_FILE, 'r') as f:
        letters = json.load(f)

    logger.info(f"Found {len(letters)} warning letters to download")

    # Load progress
    progress = load_progress()
    downloaded_urls = set(progress.get("downloaded", []))

    logger.info(f"Already downloaded: {len(downloaded_urls)}")

    # Filter to letters not yet downloaded
    to_download = [l for l in letters if l.get('letter_url') not in downloaded_urls]
    logger.info(f"Remaining to download: {len(to_download)}")

    if not to_download:
        logger.info("All letters already downloaded!")
        return

    with sync_playwright() as p:
        # Launch browser
        logger.info("Launching browser...")
        browser = p.firefox.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"
        )
        page = context.new_page()

        success_count = 0
        fail_count = 0

        try:
            for i, letter in enumerate(to_download, 1):
                url = letter.get('letter_url', '')
                company = letter.get('company_name', 'unknown')
                date = letter.get('posted_date', '') or letter.get('letter_issue_date', '')

                if not url:
                    logger.warning(f"[{i}/{len(to_download)}] No URL for {company}")
                    continue

                # Create filename
                safe_name = sanitize_filename(company)
                safe_date = date.replace('/', '-') if date else 'unknown'
                filename = f"{safe_date}_{safe_name}.html"
                filepath = OUTPUT_DIR / filename

                # Skip if already exists
                if filepath.exists():
                    progress["downloaded"].append(url)
                    logger.info(f"[{i}/{len(to_download)}] Already exists: {filename}")
                    continue

                logger.info(f"[{i}/{len(to_download)}] Downloading: {company[:50]}...")

                try:
                    # Navigate to page and wait for content
                    page.goto(url, wait_until="networkidle", timeout=30000)

                    # Wait for main content to load
                    page.wait_for_selector("#main-content", timeout=10000)
                    time.sleep(1)  # Extra time for JS to settle

                    # Get rendered HTML
                    html = page.content()

                    # Verify we got actual content (check for letter body text)
                    if "field--name-body" in html or "lcds-description-list" in html:
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.write(html)

                        success_count += 1
                        progress["downloaded"].append(url)
                        logger.info(f"  ✓ Saved: {filename} ({len(html):,} bytes)")
                    else:
                        fail_count += 1
                        progress["failed"].append({"url": url, "error": "No content found"})
                        logger.warning(f"  ✗ No content found")

                except Exception as e:
                    fail_count += 1
                    progress["failed"].append({"url": url, "error": str(e)})
                    logger.error(f"  ✗ Failed: {str(e)[:100]}")

                # Save progress every 25 downloads
                if i % 25 == 0:
                    save_progress(progress)
                    logger.info(f"Progress saved: {success_count} downloaded, {fail_count} failed")

                # Rate limit
                time.sleep(RATE_LIMIT_DELAY)

        finally:
            browser.close()

    # Final save
    save_progress(progress)

    logger.info("=" * 70)
    logger.info("DOWNLOAD COMPLETE")
    logger.info("=" * 70)
    logger.info(f"Successfully downloaded: {success_count}")
    logger.info(f"Failed: {fail_count}")
    logger.info(f"Total in progress file: {len(progress['downloaded'])}")
    logger.info(f"Output directory: {OUTPUT_DIR.absolute()}")


if __name__ == "__main__":
    main()
