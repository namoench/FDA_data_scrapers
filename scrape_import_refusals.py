#!/usr/bin/env python3
"""
FDA Import Refusals Data Extractor

Pulls all import refusal records from the FDA Data Dashboard API.
Uses the same authentication as fda_data_extractor.py.

Usage:
    # Full extraction (503K+ records)
    python scrape_import_refusals.py

    # Test mode (100 records)
    python scrape_import_refusals.py --test
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
BASE_URL = "https://api-datadashboard.fda.gov/v1"
ENDPOINT = "/import_refusals"
MAX_ROWS_PER_REQUEST = 5000
DEFAULT_DELAY_SECONDS = 1.0
OUTPUT_DIR = Path(__file__).parent / "data" / "import_refusals"
SAVE_INTERVAL = 50000  # Save progress every N records


def setup_logging(output_dir: Path) -> logging.Logger:
    """Configure logging with both console and file output."""
    logger = logging.getLogger("import_refusals")
    logger.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    # File handler
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / f"scrape_{datetime.now():%Y%m%d_%H%M%S}.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(funcName)s:%(lineno)d | %(message)s"
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    return logger


def save_scrape_log(output_dir: Path, status: dict) -> None:
    """Save scrape progress log."""
    log_path = output_dir / "scrape_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, default=str)


def save_data(data: list, output_dir: Path, filename: str, logger: logging.Logger) -> None:
    """Save data to JSON file."""
    filepath = output_dir / filename
    logger.info(f"Saving {len(data):,} records to {filepath.name}")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    file_size = filepath.stat().st_size / (1024 * 1024)
    logger.info(f"Saved {filepath.name} ({file_size:.2f} MB)")


def extract_import_refusals(
    api_user: str,
    api_key: str,
    max_rows: int | None = None,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    logger: logging.Logger | None = None
) -> list[dict]:
    """Extract all import refusal records with pagination."""

    logger = logger or logging.getLogger("import_refusals")

    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Authorization-User": api_user,
        "Authorization-Key": api_key
    })

    all_records = []
    start = 1
    total_count = None
    request_count = 0
    retry_count = 0
    max_retries = 3

    scrape_status = {
        "source": "import_refusals",
        "started_at": datetime.now().isoformat(),
        "status": "in_progress",
        "total_records": None,
        "downloaded": 0,
        "failed": 0,
        "notes": ""
    }

    logger.info("=" * 60)
    logger.info("FDA Import Refusals Extractor")
    logger.info("=" * 60)

    while True:
        rows_to_fetch = MAX_ROWS_PER_REQUEST
        if max_rows is not None:
            remaining = max_rows - len(all_records)
            if remaining <= 0:
                break
            rows_to_fetch = min(rows_to_fetch, remaining)

        payload = {
            "start": start,
            "rows": rows_to_fetch,
            "returntotalcount": (total_count is None),
            "sort": "",
            "sortorder": "ASC",
            "filters": {},
            "columns": []
        }

        try:
            response = session.post(
                f"{BASE_URL}{ENDPOINT}",
                json=payload,
                timeout=120
            )

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning(f"Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
                continue

            response.raise_for_status()
            result = response.json()
            request_count += 1
            retry_count = 0  # Reset retry counter on success

        except requests.exceptions.RequestException as e:
            retry_count += 1
            if retry_count <= max_retries:
                wait_time = 5 * retry_count
                logger.warning(f"Request failed: {e}. Retry {retry_count}/{max_retries} in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                logger.error(f"Request failed after {max_retries} retries: {e}")
                scrape_status["status"] = "error"
                scrape_status["notes"] = str(e)
                raise

        # Get total count from first response
        if total_count is None:
            total_count = result.get("totalrecordcount", 0)
            scrape_status["total_records"] = total_count
            logger.info(f"Total records available: {total_count:,}")
            if max_rows:
                logger.info(f"Limiting extraction to: {max_rows:,} rows")

        # Extract records
        records = result.get("result", [])
        if not records:
            logger.debug("No more records returned")
            break

        all_records.extend(records)
        scrape_status["downloaded"] = len(all_records)
        scrape_status["last_updated"] = datetime.now().isoformat()

        # Progress logging
        effective_total = min(total_count, max_rows) if max_rows else total_count
        progress_pct = (len(all_records) / effective_total * 100) if effective_total > 0 else 100
        logger.info(
            f"Progress: {len(all_records):,}/{effective_total:,} records "
            f"({progress_pct:.1f}%) - Request #{request_count}"
        )

        # Incremental save
        if len(all_records) % SAVE_INTERVAL == 0:
            save_data(all_records, OUTPUT_DIR, "import_refusals_partial.json", logger)
            save_scrape_log(OUTPUT_DIR, scrape_status)

        # Check if done
        if len(all_records) >= total_count:
            break
        if max_rows and len(all_records) >= max_rows:
            break

        # Move to next page
        start += rows_to_fetch

        # Rate limiting
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    scrape_status["status"] = "completed"
    scrape_status["completed_at"] = datetime.now().isoformat()
    save_scrape_log(OUTPUT_DIR, scrape_status)

    logger.info("=" * 60)
    logger.info(f"Extraction complete: {len(all_records):,} records in {request_count} requests")
    logger.info("=" * 60)

    return all_records


def main():
    parser = argparse.ArgumentParser(
        description="Extract FDA Import Refusals data"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: fetch only 100 records"
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        help="Maximum rows to fetch"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Delay between requests (default: {DEFAULT_DELAY_SECONDS}s)"
    )

    args = parser.parse_args()

    # Set up
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(OUTPUT_DIR)

    # Get credentials
    api_user = os.getenv("FDA_API_USER")
    api_key = os.getenv("FDA_API_KEY")

    if not api_user or not api_key:
        logger.error("Missing FDA_API_USER or FDA_API_KEY in environment")
        sys.exit(1)

    # Determine max rows
    if args.test:
        max_rows = 100
        logger.info("TEST MODE: Fetching 100 records")
    else:
        max_rows = args.max_rows

    # Extract data
    try:
        data = extract_import_refusals(
            api_user=api_user,
            api_key=api_key,
            max_rows=max_rows,
            delay_seconds=args.delay,
            logger=logger
        )

        # Save final output
        save_data(data, OUTPUT_DIR, "import_refusals.json", logger)

        # Clean up partial file if exists
        partial_file = OUTPUT_DIR / "import_refusals_partial.json"
        if partial_file.exists():
            partial_file.unlink()
            logger.info("Cleaned up partial file")

        logger.info(f"Output saved to: {OUTPUT_DIR}")

    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
