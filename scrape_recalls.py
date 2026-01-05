#!/usr/bin/env python3
"""
OpenFDA Recalls Scraper

Fetches all enforcement/recall records from openFDA public API endpoints:
- Drug enforcement reports
- Food enforcement reports
- Device recall reports

Uses pagination with skip/limit parameters and includes rate limiting,
error handling, and incremental saves.

Usage:
    python scrape_recalls.py
"""

import os
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
LIMIT_PER_REQUEST = 1000  # Max allowed by openFDA
DEFAULT_DELAY_SECONDS = 1.0
MAX_RETRIES = 3
BACKOFF_FACTOR = 2
SAVE_INTERVAL = 10000  # Save progress every N records
OUTPUT_DIR = Path(__file__).parent / "data" / "recalls"

# Endpoints to scrape
ENDPOINTS = {
    "drug_enforcement": {
        "path": "/drug/enforcement.json",
        "description": "Drug enforcement reports",
        "expected_count": 14658
    },
    "food_enforcement": {
        "path": "/food/enforcement.json",
        "description": "Food enforcement reports",
        "expected_count": 28248
    },
    "device_recall": {
        "path": "/device/recall.json",
        "description": "Device recall reports",
        "expected_count": 56994
    }
}


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Configure logging with both console and file output."""
    logger = logging.getLogger("openfda_scraper")
    logger.setLevel(getattr(logging, log_level.upper()))

    # Remove any existing handlers
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
    log_file = OUTPUT_DIR.parent / f"scrape_recalls_{datetime.now():%Y%m%d_%H%M%S}.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(funcName)s:%(lineno)d | %(message)s"
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    return logger


class OpenFDARecallsScraper:
    """Scrapes recall/enforcement data from openFDA public API."""

    def __init__(
        self,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        logger: logging.Logger | None = None
    ):
        self.delay_seconds = delay_seconds
        self.logger = logger or logging.getLogger("openfda_scraper")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "OpenFDA-Recalls-Scraper/1.0"
        })

    def _make_request(
        self,
        endpoint_path: str,
        skip: int,
        limit: int
    ) -> dict[str, Any]:
        """
        Make a single API request with retry logic.

        Args:
            endpoint_path: API endpoint path (e.g., /drug/enforcement.json)
            skip: Number of records to skip
            limit: Number of records to fetch

        Returns:
            API response JSON
        """
        url = f"{BASE_URL}{endpoint_path}"
        params = {
            "skip": skip,
            "limit": limit
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self.logger.debug(f"Request to {url}: skip={skip}, limit={limit} (attempt {attempt})")

                response = self.session.get(url, params=params, timeout=60)

                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    self.logger.warning(f"Rate limited (429). Waiting {retry_after}s...")
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                return response.json()

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 429:
                    # Already handled above, but just in case
                    backoff = BACKOFF_FACTOR ** attempt
                    self.logger.warning(f"Rate limited. Backing off {backoff}s...")
                    time.sleep(backoff)
                    continue
                elif e.response.status_code == 404:
                    # No more results
                    self.logger.debug(f"No more results (404) at skip={skip}")
                    return {"results": []}
                else:
                    self.logger.error(f"HTTP error {e.response.status_code}: {e}")
                    if attempt < MAX_RETRIES:
                        backoff = BACKOFF_FACTOR ** attempt
                        self.logger.info(f"Retrying in {backoff}s...")
                        time.sleep(backoff)
                    else:
                        raise

            except requests.exceptions.RequestException as e:
                self.logger.error(f"Request failed: {e}")
                if attempt < MAX_RETRIES:
                    backoff = BACKOFF_FACTOR ** attempt
                    self.logger.info(f"Retrying in {backoff}s...")
                    time.sleep(backoff)
                else:
                    raise

        raise Exception(f"Failed after {MAX_RETRIES} retries")

    def _save_progress(
        self,
        endpoint_key: str,
        records: list[dict],
        skip: int,
        total_fetched: int
    ) -> None:
        """Save current progress to JSON file."""
        output_file = OUTPUT_DIR / f"{endpoint_key}.json"
        self.logger.info(f"Saving progress: {len(records):,} records to {output_file.name}")

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)

        file_size = output_file.stat().st_size / (1024 * 1024)
        self.logger.info(f"Saved {output_file.name} ({file_size:.2f} MB)")

    def scrape_endpoint(self, endpoint_key: str) -> tuple[list[dict], dict]:
        """
        Scrape all records from a single endpoint.

        Args:
            endpoint_key: Key from ENDPOINTS dict

        Returns:
            Tuple of (all_records, stats)
        """
        endpoint_info = ENDPOINTS[endpoint_key]
        endpoint_path = endpoint_info["path"]
        expected_count = endpoint_info["expected_count"]

        self.logger.info("=" * 60)
        self.logger.info(f"Starting scrape: {endpoint_key}")
        self.logger.info(f"Description: {endpoint_info['description']}")
        self.logger.info(f"Expected records: ~{expected_count:,}")
        self.logger.info("=" * 60)

        all_records = []
        skip = 0
        request_count = 0
        error_count = 0
        consecutive_errors = 0
        start_time = time.time()
        last_save_count = 0

        while True:
            try:
                result = self._make_request(
                    endpoint_path=endpoint_path,
                    skip=skip,
                    limit=LIMIT_PER_REQUEST
                )
                request_count += 1
                consecutive_errors = 0  # Reset on success

                # Extract results
                results = result.get("results", [])

                if not results:
                    self.logger.debug(f"No more records at skip={skip}")
                    break

                all_records.extend(results)

                # Progress logging with percentage
                progress_pct = (len(all_records) / expected_count * 100) if expected_count > 0 else 0
                self.logger.info(
                    f"Progress: {len(all_records):,} records fetched "
                    f"({progress_pct:.1f}%) - Request #{request_count}"
                )

                # Incremental save every SAVE_INTERVAL records
                if len(all_records) - last_save_count >= SAVE_INTERVAL:
                    self._save_progress(endpoint_key, all_records, skip, len(all_records))
                    last_save_count = len(all_records)

                # Check if we got fewer results than requested (last page)
                if len(results) < LIMIT_PER_REQUEST:
                    self.logger.debug(f"Last page: only {len(results)} records returned")
                    break

                # Move to next page
                skip += LIMIT_PER_REQUEST

                # Rate limiting delay
                if self.delay_seconds > 0:
                    time.sleep(self.delay_seconds)

            except Exception as e:
                error_count += 1
                consecutive_errors += 1
                self.logger.error(f"Error at skip={skip}: {e}")

                # If we've hit 5 consecutive errors, assume we've reached the pagination limit
                if consecutive_errors >= 5:
                    self.logger.warning(
                        f"Hit {consecutive_errors} consecutive errors. "
                        f"Likely reached API pagination limit. Stopping at {len(all_records):,} records."
                    )
                    break

                # Save what we have so far
                if all_records:
                    self._save_progress(endpoint_key, all_records, skip, len(all_records))

                # Continue to next batch
                skip += LIMIT_PER_REQUEST
                time.sleep(self.delay_seconds * 2)  # Extra delay after error

        # Final save
        self._save_progress(endpoint_key, all_records, skip, len(all_records))

        elapsed_time = time.time() - start_time

        stats = {
            "endpoint": endpoint_key,
            "total_records": len(all_records),
            "expected_records": expected_count,
            "request_count": request_count,
            "error_count": error_count,
            "elapsed_seconds": elapsed_time,
            "timestamp": datetime.now().isoformat()
        }

        self.logger.info("-" * 60)
        self.logger.info(f"Completed {endpoint_key}:")
        self.logger.info(f"  Total records: {len(all_records):,}")
        self.logger.info(f"  Requests made: {request_count}")
        self.logger.info(f"  Errors encountered: {error_count}")
        self.logger.info(f"  Time elapsed: {elapsed_time:.1f}s")
        self.logger.info("-" * 60)

        return all_records, stats


def save_scrape_log(all_stats: list[dict], logger: logging.Logger) -> None:
    """Save scraping summary to scrape_log.json."""
    log_path = OUTPUT_DIR / "scrape_log.json"

    summary = {
        "timestamp": datetime.now().isoformat(),
        "total_endpoints": len(all_stats),
        "endpoints": all_stats,
        "grand_total_records": sum(s["total_records"] for s in all_stats),
        "total_time_seconds": sum(s["elapsed_seconds"] for s in all_stats)
    }

    logger.info(f"Saving scrape log to {log_path}")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    logger.info(f"Scrape log saved: {log_path}")


def main():
    """Main entry point."""
    logger = setup_logging()

    logger.info("=" * 60)
    logger.info("OpenFDA Recalls Scraper")
    logger.info("=" * 60)
    logger.info(f"Output directory: {OUTPUT_DIR.absolute()}")
    logger.info(f"Limit per request: {LIMIT_PER_REQUEST}")
    logger.info(f"Delay between requests: {DEFAULT_DELAY_SECONDS}s")
    logger.info(f"Save interval: every {SAVE_INTERVAL:,} records")
    logger.info("=" * 60)

    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize scraper
    scraper = OpenFDARecallsScraper(
        delay_seconds=DEFAULT_DELAY_SECONDS,
        logger=logger
    )

    # Scrape all endpoints
    all_stats = []
    overall_start = time.time()

    for endpoint_key in ENDPOINTS.keys():
        try:
            records, stats = scraper.scrape_endpoint(endpoint_key)
            all_stats.append(stats)
        except Exception as e:
            logger.error(f"Failed to complete {endpoint_key}: {e}")
            # Continue with next endpoint

    overall_elapsed = time.time() - overall_start

    # Save summary log
    save_scrape_log(all_stats, logger)

    # Final summary
    logger.info("=" * 60)
    logger.info("SCRAPING SUMMARY")
    logger.info("=" * 60)

    for stats in all_stats:
        logger.info(
            f"{stats['endpoint']}: {stats['total_records']:,} records "
            f"({stats['request_count']} requests, {stats['error_count']} errors)"
        )

    total_records = sum(s["total_records"] for s in all_stats)
    logger.info("-" * 60)
    logger.info(f"Grand total: {total_records:,} records")
    logger.info(f"Overall time: {overall_elapsed:.1f}s ({overall_elapsed/60:.1f} minutes)")
    logger.info(f"Output directory: {OUTPUT_DIR.absolute()}")
    logger.info("=" * 60)
    logger.info("Done!")

    return all_stats


if __name__ == "__main__":
    main()
