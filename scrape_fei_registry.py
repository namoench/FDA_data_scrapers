#!/usr/bin/env python3
"""
FDA FEI Registry Scraper (Device Registration & Listing)

Fetches all establishment registration and listing data from openFDA API.
Contains FEI numbers, establishment names, addresses, and device listings.

Uses the public openFDA API: https://api.fda.gov/device/registrationlisting.json

Usage:
    python scrape_fei_registry.py
"""

import sys
import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

# Configuration
BASE_URL = "https://api.fda.gov"
ENDPOINT = "/device/registrationlisting.json"
LIMIT_PER_REQUEST = 1000  # Max allowed by openFDA
DEFAULT_DELAY_SECONDS = 1.0
MAX_RETRIES = 3
BACKOFF_FACTOR = 2
SAVE_INTERVAL = 50000  # Save progress every N records (larger dataset)
OUTPUT_DIR = Path(__file__).parent / "data" / "fei_registry"


def setup_logging() -> logging.Logger:
    """Configure logging with both console and file output."""
    logger = logging.getLogger("fei_registry")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

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
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log_file = OUTPUT_DIR / f"scrape_{datetime.now():%Y%m%d_%H%M%S}.log"
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


def fetch_with_retry(
    session: requests.Session,
    url: str,
    params: dict,
    max_retries: int = MAX_RETRIES,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    logger: logging.Logger | None = None
) -> dict | None:
    """Fetch URL with retry logic. Returns JSON result or None on failure."""
    logger = logger or logging.getLogger("fei_registry")

    for attempt in range(1, max_retries + 1):
        try:
            response = session.get(url, params=params, timeout=60)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning(f"Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
                continue

            if response.status_code == 404:
                return None

            if response.status_code == 400:
                # openFDA returns 400 for deep pagination - this is expected
                logger.debug(f"HTTP 400 at params={params}")
                return None

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                wait_time = BACKOFF_FACTOR ** attempt
                logger.warning(f"Request failed: {e}. Retry {attempt}/{max_retries} in {wait_time}s...")
                time.sleep(wait_time)
            else:
                logger.error(f"Request failed after {max_retries} retries: {e}")
                return None

    return None


# US states and territories for chunking
US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP"  # Territories
]

# Top countries by FDA registrations (non-US)
TOP_COUNTRIES = [
    "CN", "DE", "JP", "GB", "FR", "IT", "KR", "TW", "CA", "IN",
    "NL", "CH", "AU", "BE", "ES", "IL", "SE", "AT", "DK", "IE",
    "MX", "BR", "PL", "SG", "MY", "TH", "CZ", "HU", "FI", "NO",
    "PT", "NZ", "ZA", "AR", "CL", "CO", "HK", "PH", "VN", "ID"
]


def paginate_search(
    session: requests.Session,
    search_filter: str,
    delay_seconds: float,
    logger: logging.Logger,
    max_skip: int = 25000
) -> list[dict]:
    """Paginate through a search filter up to max_skip."""
    records = []
    skip = 0

    while skip < max_skip:
        # Build URL manually to avoid requests auto-encoding the + in AND
        # The openFDA API expects "+" or spaces between AND clauses
        url = f"{BASE_URL}{ENDPOINT}?search={search_filter}&skip={skip}&limit={LIMIT_PER_REQUEST}"

        result = fetch_with_retry(
            session, url, {},  # Empty params since we built the URL manually
            delay_seconds=delay_seconds, logger=logger
        )

        if result is None:
            break

        batch = result.get("results", [])
        if not batch:
            break

        records.extend(batch)

        if len(batch) < LIMIT_PER_REQUEST:
            break

        skip += LIMIT_PER_REQUEST
        time.sleep(delay_seconds)

    return records


def extract_fei_registry_by_year(
    year: int,
    session: requests.Session,
    delay_seconds: float,
    logger: logging.Logger
) -> list[dict]:
    """Extract FEI records for a specific registration year using state/country chunking."""

    all_records = []
    seen_ids = set()  # Track FEI numbers to avoid duplicates

    # First, get the year total
    year_search = f"registration.reg_expiry_date_year:{year}"
    url = f"{BASE_URL}{ENDPOINT}?search={year_search}&limit=1"
    result = fetch_with_retry(
        session, url, {},
        delay_seconds=delay_seconds, logger=logger
    )
    if result:
        year_total = result.get("meta", {}).get("results", {}).get("total", 0)
        logger.info(f"  Year {year}: {year_total:,} records total")
    else:
        logger.warning(f"  Year {year}: Could not get total count")
        return []

    # Collect US records by state (state_code implies US, so no need for iso_country_code)
    logger.info(f"  Year {year}: Collecting US records by state...")
    for state in US_STATES:
        # Use just year + state - the 3-field AND causes API 500 errors
        search_filter = f"registration.reg_expiry_date_year:{year}+AND+registration.state_code:{state}"

        records = paginate_search(session, search_filter, delay_seconds, logger)

        # Deduplicate by FEI number
        new_records = []
        for r in records:
            fei = r.get("registration", {}).get("fei_number")
            if fei and fei not in seen_ids:
                seen_ids.add(fei)
                new_records.append(r)

        if new_records:
            all_records.extend(new_records)
            logger.debug(f"    {state}: {len(new_records)} records")

    us_count = len(all_records)
    logger.info(f"  Year {year}: Collected {us_count:,} US records")

    # Collect non-US records by country
    logger.info(f"  Year {year}: Collecting international records...")
    for country in TOP_COUNTRIES:
        search_filter = f"registration.reg_expiry_date_year:{year}+AND+registration.iso_country_code:{country}"

        records = paginate_search(session, search_filter, delay_seconds, logger)

        new_records = []
        for r in records:
            fei = r.get("registration", {}).get("fei_number")
            if fei and fei not in seen_ids:
                seen_ids.add(fei)
                new_records.append(r)

        if new_records:
            all_records.extend(new_records)
            logger.debug(f"    {country}: {len(new_records)} records")

    intl_count = len(all_records) - us_count
    logger.info(f"  Year {year}: Collected {intl_count:,} international records")

    # Try to collect any remaining countries not in our list
    # by querying without country filter and checking what's missing
    logger.info(f"  Year {year}: Checking for remaining countries...")
    search_filter = f"registration.reg_expiry_date_year:{year}"
    remaining = paginate_search(session, search_filter, delay_seconds, logger, max_skip=26000)

    new_records = []
    for r in remaining:
        fei = r.get("registration", {}).get("fei_number")
        if fei and fei not in seen_ids:
            seen_ids.add(fei)
            new_records.append(r)

    if new_records:
        all_records.extend(new_records)
        logger.info(f"  Year {year}: Found {len(new_records):,} additional records from other countries")

    logger.info(f"  Year {year}: Total collected: {len(all_records):,} records")
    return all_records


def extract_fei_registry(
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    max_records: int | None = None,
    start_year: int = 2015,
    end_year: int | None = None,
    logger: logging.Logger | None = None
) -> list[dict]:
    """Extract FEI registration/listing records from openFDA using year-based chunking.

    Uses year filtering to bypass openFDA's 26K pagination limit.
    """

    logger = logger or logging.getLogger("fei_registry")

    if end_year is None:
        end_year = datetime.now().year + 1  # Include next year for renewals

    session = requests.Session()
    session.headers.update({
        "User-Agent": "OpenFDA-FEI-Registry-Scraper/1.0"
    })

    all_records = []
    request_count = 0
    error_count = 0

    scrape_status = {
        "source": "fei_registry",
        "endpoint": f"{BASE_URL}{ENDPOINT}",
        "started_at": datetime.now().isoformat(),
        "status": "in_progress",
        "total_records": None,
        "downloaded": 0,
        "failed": 0,
        "notes": f"Year-based extraction: {start_year}-{end_year}"
    }

    logger.info("=" * 60)
    logger.info("FDA FEI Registry Extractor (openFDA)")
    logger.info("=" * 60)
    logger.info(f"Endpoint: {BASE_URL}{ENDPOINT}")
    logger.info(f"Strategy: Year-based chunking ({start_year}-{end_year})")
    logger.info(f"Limit per request: {LIMIT_PER_REQUEST}")
    logger.info(f"Delay between requests: {delay_seconds}s")

    # First, get the overall total
    result = fetch_with_retry(
        session, f"{BASE_URL}{ENDPOINT}?limit=1", {},
        delay_seconds=delay_seconds, logger=logger
    )
    if result:
        total_count = result.get("meta", {}).get("results", {}).get("total", 0)
        scrape_status["total_records"] = total_count
        logger.info(f"Total records in database: {total_count:,}")

    # Iterate through years
    for year in range(start_year, end_year + 1):
        if max_records and len(all_records) >= max_records:
            logger.info(f"Reached max records limit: {max_records}")
            break

        logger.info(f"Fetching year {year}...")

        year_records = extract_fei_registry_by_year(
            year, session, delay_seconds, logger
        )

        all_records.extend(year_records)
        scrape_status["downloaded"] = len(all_records)
        scrape_status["last_updated"] = datetime.now().isoformat()

        logger.info(f"  Year {year}: collected {len(year_records):,} records (total: {len(all_records):,})")

        # Save progress after each year
        save_data(all_records, OUTPUT_DIR, "establishments_partial.json", logger)
        save_scrape_log(OUTPUT_DIR, scrape_status)

        time.sleep(delay_seconds)  # Brief pause between years

    # Final status update
    scrape_status["status"] = "completed"
    scrape_status["completed_at"] = datetime.now().isoformat()
    scrape_status["downloaded"] = len(all_records)
    save_scrape_log(OUTPUT_DIR, scrape_status)

    logger.info("=" * 60)
    logger.info(f"Extraction complete: {len(all_records):,} records")
    logger.info("=" * 60)

    return all_records


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract FDA FEI Registry data from openFDA"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: fetch only 1000 records"
    )
    parser.add_argument(
        "--max-records",
        type=int,
        help="Maximum records to fetch"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Delay between requests (default: {DEFAULT_DELAY_SECONDS}s)"
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=2015,
        help="Start year for registration expiry (default: 2015)"
    )
    parser.add_argument(
        "--end-year",
        type=int,
        help="End year for registration expiry (default: current year + 1)"
    )

    args = parser.parse_args()

    # Set up
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger = setup_logging()

    # Determine max records
    if args.test:
        max_records = 1000
        logger.info("TEST MODE: Fetching 1000 records")
    else:
        max_records = args.max_records

    # Extract data
    try:
        data = extract_fei_registry(
            delay_seconds=args.delay,
            max_records=max_records,
            start_year=args.start_year,
            end_year=args.end_year,
            logger=logger
        )

        # Save final output
        save_data(data, OUTPUT_DIR, "establishments.json", logger)

        # Clean up partial file if exists
        partial_file = OUTPUT_DIR / "establishments_partial.json"
        if partial_file.exists():
            partial_file.unlink()
            logger.info("Cleaned up partial file")

        logger.info(f"Output saved to: {OUTPUT_DIR}")

    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
