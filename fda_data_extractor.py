#!/usr/bin/env python3
"""
FDA Data Dashboard API Extractor

Pulls all Form 483 inspection and citation data from the FDA Data Dashboard API.
Handles pagination, rate limiting, and exports to JSON/CSV formats.

Usage:
    # Test mode (10 rows per endpoint to verify auth)
    python fda_data_extractor.py --test

    # Full extraction
    python fda_data_extractor.py

    # Extract specific endpoint only
    python fda_data_extractor.py --endpoint inspections_classifications
    python fda_data_extractor.py --endpoint inspections_citations
"""

import os
import sys
import json
import csv
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
BASE_URL = "https://api-datadashboard.fda.gov/v1"
MAX_ROWS_PER_REQUEST = 5000
DEFAULT_DELAY_SECONDS = 1.0  # Delay between requests for rate limiting
OUTPUT_DIR = Path(__file__).parent / "data"

# Endpoints to extract
ENDPOINTS = {
    "inspections_classifications": {
        "path": "/inspections_classifications",
        "description": "Inspection metadata (firm info, dates, classifications)"
    },
    "inspections_citations": {
        "path": "/inspections_citations",
        "description": "Individual 483 citation details with CFR references"
    }
}

# Set up logging
def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Configure logging with both console and file output."""
    logger = logging.getLogger("fda_extractor")
    logger.setLevel(getattr(logging, log_level.upper()))

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
    OUTPUT_DIR.mkdir(exist_ok=True)
    log_file = OUTPUT_DIR / f"extraction_{datetime.now():%Y%m%d_%H%M%S}.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(funcName)s:%(lineno)d | %(message)s"
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    return logger


class FDADataExtractor:
    """Extracts data from FDA Data Dashboard API with pagination support."""

    def __init__(
        self,
        api_user: str,
        api_key: str,
        rows_per_request: int = MAX_ROWS_PER_REQUEST,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        logger: logging.Logger | None = None
    ):
        self.api_user = api_user
        self.api_key = api_key
        self.rows_per_request = rows_per_request
        self.delay_seconds = delay_seconds
        self.logger = logger or logging.getLogger("fda_extractor")

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Authorization-User": self.api_user,
            "Authorization-Key": self.api_key
        })

    def _make_request(
        self,
        endpoint: str,
        start: int,
        rows: int,
        return_total_count: bool = False,
        filters: dict | None = None,
        columns: list | None = None,
        sort: str = "",
        sort_order: str = "ASC"
    ) -> dict[str, Any]:
        """Make a single API request with the given parameters."""
        url = f"{BASE_URL}{endpoint}"

        payload = {
            "start": start,
            "rows": rows,
            "returntotalcount": return_total_count,
            "sort": sort,
            "sortorder": sort_order,
            "filters": filters or {},
            "columns": columns or []
        }

        self.logger.debug(f"Request to {url}: start={start}, rows={rows}")

        response = self.session.post(url, json=payload, timeout=120)
        response.raise_for_status()

        return response.json()

    def _handle_rate_limit(self, response: requests.Response) -> bool:
        """Check for rate limiting and handle appropriately. Returns True if rate limited."""
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            self.logger.warning(f"Rate limited. Waiting {retry_after} seconds...")
            time.sleep(retry_after)
            return True
        return False

    def extract_endpoint(
        self,
        endpoint_key: str,
        max_rows: int | None = None,
        filters: dict | None = None
    ) -> list[dict]:
        """
        Extract all data from a single endpoint with pagination.

        Args:
            endpoint_key: Key from ENDPOINTS dict
            max_rows: Optional limit on total rows to fetch (for testing)
            filters: Optional filters to apply to the query

        Returns:
            List of all records from the endpoint
        """
        endpoint_info = ENDPOINTS[endpoint_key]
        endpoint_path = endpoint_info["path"]

        self.logger.info(f"Starting extraction: {endpoint_key}")
        self.logger.info(f"Description: {endpoint_info['description']}")

        all_records = []
        start = 1
        total_count = None
        request_count = 0

        while True:
            # Determine rows to fetch this request
            rows_to_fetch = self.rows_per_request
            if max_rows is not None:
                remaining = max_rows - len(all_records)
                if remaining <= 0:
                    break
                rows_to_fetch = min(rows_to_fetch, remaining)

            # First request gets total count
            return_total = (total_count is None)

            try:
                result = self._make_request(
                    endpoint=endpoint_path,
                    start=start,
                    rows=rows_to_fetch,
                    return_total_count=return_total,
                    filters=filters
                )
                request_count += 1

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 429:
                    # Rate limited - wait and retry
                    retry_after = int(e.response.headers.get("Retry-After", 60))
                    self.logger.warning(f"Rate limited. Waiting {retry_after}s...")
                    time.sleep(retry_after)
                    continue
                else:
                    self.logger.error(f"HTTP error: {e}")
                    raise

            except requests.exceptions.RequestException as e:
                self.logger.error(f"Request failed: {e}")
                raise

            # Get total count from first response
            if total_count is None:
                total_count = result.get("totalrecordcount", 0)
                self.logger.info(f"Total records available: {total_count:,}")
                if max_rows:
                    self.logger.info(f"Limiting extraction to: {max_rows:,} rows")

            # Extract records from response (API uses "result" singular)
            records = result.get("result", [])
            if not records:
                self.logger.debug("No more records returned")
                break

            all_records.extend(records)

            # Progress logging
            effective_total = min(total_count, max_rows) if max_rows else total_count
            progress_pct = (len(all_records) / effective_total * 100) if effective_total > 0 else 100
            self.logger.info(
                f"Progress: {len(all_records):,}/{effective_total:,} records "
                f"({progress_pct:.1f}%) - Request #{request_count}"
            )

            # Check if we've fetched all records
            if len(all_records) >= total_count:
                break
            if max_rows and len(all_records) >= max_rows:
                break

            # Move to next page
            start += rows_to_fetch

            # Rate limiting delay
            if self.delay_seconds > 0:
                time.sleep(self.delay_seconds)

        self.logger.info(
            f"Extraction complete: {len(all_records):,} records in {request_count} requests"
        )
        return all_records

    def test_connection(self) -> bool:
        """Test API connection with a minimal request."""
        self.logger.info("Testing API connection...")

        try:
            result = self._make_request(
                endpoint="/inspections_classifications",
                start=1,
                rows=1,
                return_total_count=True
            )

            total = result.get("totalrecordcount", 0)
            records = result.get("result", [])

            self.logger.info(f"Connection successful!")
            self.logger.info(f"Total inspections_classifications records: {total:,}")

            if records:
                self.logger.info(f"Sample record fields: {list(records[0].keys())}")

            return True

        except requests.exceptions.HTTPError as e:
            self.logger.error(f"Authentication failed: {e}")
            if e.response.status_code == 401:
                self.logger.error("Check your FDA_API_USER and FDA_API_KEY credentials")
            return False

        except Exception as e:
            self.logger.error(f"Connection test failed: {e}")
            return False


def save_to_json(data: list[dict], filepath: Path, logger: logging.Logger) -> None:
    """Save data to JSON file."""
    logger.info(f"Saving JSON: {filepath}")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    file_size = filepath.stat().st_size / (1024 * 1024)
    logger.info(f"Saved {filepath.name} ({file_size:.2f} MB)")


def save_to_csv(data: list[dict], filepath: Path, logger: logging.Logger) -> None:
    """Save data to CSV file."""
    if not data:
        logger.warning(f"No data to save to {filepath}")
        return

    logger.info(f"Saving CSV: {filepath}")

    # Get all unique field names across all records
    fieldnames = set()
    for record in data:
        fieldnames.update(record.keys())
    fieldnames = sorted(fieldnames)

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data)

    file_size = filepath.stat().st_size / (1024 * 1024)
    logger.info(f"Saved {filepath.name} ({file_size:.2f} MB, {len(data):,} rows)")


def main():
    parser = argparse.ArgumentParser(
        description="Extract FDA Form 483 data from FDA Data Dashboard API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --test                    # Test with 10 rows per endpoint
  %(prog)s                           # Full extraction of all data
  %(prog)s --endpoint inspections_classifications  # Single endpoint
  %(prog)s --delay 2.0               # Slower requests (2 sec delay)
        """
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: fetch only 10 rows per endpoint to verify auth"
    )
    parser.add_argument(
        "--endpoint",
        choices=list(ENDPOINTS.keys()),
        help="Extract only a specific endpoint"
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=MAX_ROWS_PER_REQUEST,
        help=f"Rows per request (default: {MAX_ROWS_PER_REQUEST}, max: 5000)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Delay between requests in seconds (default: {DEFAULT_DELAY_SECONDS})"
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        help="Maximum total rows to fetch per endpoint (for partial extractions)"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help=f"Output directory for data files (default: {OUTPUT_DIR})"
    )

    args = parser.parse_args()

    # Set up logging
    logger = setup_logging()

    # Get credentials
    api_user = os.getenv("FDA_API_USER")
    api_key = os.getenv("FDA_API_KEY")

    if not api_user or not api_key:
        logger.error("Missing API credentials!")
        logger.error("Set FDA_API_USER and FDA_API_KEY environment variables")
        logger.error("Or copy .env.example to .env and fill in your credentials")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("FDA Data Dashboard API Extractor")
    logger.info("=" * 60)
    logger.info(f"API User: {api_user}")
    logger.info(f"Rows per request: {min(args.rows, 5000)}")
    logger.info(f"Delay between requests: {args.delay}s")

    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Initialize extractor
    extractor = FDADataExtractor(
        api_user=api_user,
        api_key=api_key,
        rows_per_request=min(args.rows, 5000),
        delay_seconds=args.delay,
        logger=logger
    )

    # Test connection first
    if not extractor.test_connection():
        logger.error("Connection test failed. Exiting.")
        sys.exit(1)

    # Determine what to extract
    if args.test:
        max_rows = 10
        logger.info("TEST MODE: Extracting 10 rows per endpoint")
    else:
        max_rows = args.max_rows

    endpoints_to_extract = (
        [args.endpoint] if args.endpoint else list(ENDPOINTS.keys())
    )

    # Extract each endpoint
    results = {}
    for endpoint_key in endpoints_to_extract:
        logger.info("-" * 60)

        try:
            data = extractor.extract_endpoint(
                endpoint_key=endpoint_key,
                max_rows=max_rows
            )
            results[endpoint_key] = data

            # Save files
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            # For test mode, include "test" in filename
            suffix = "_test" if args.test else ""

            json_path = args.output_dir / f"{endpoint_key}{suffix}.json"
            csv_path = args.output_dir / f"{endpoint_key}{suffix}.csv"

            save_to_json(data, json_path, logger)
            save_to_csv(data, csv_path, logger)

        except Exception as e:
            logger.error(f"Failed to extract {endpoint_key}: {e}")
            if not args.test:
                raise

    # Summary
    logger.info("=" * 60)
    logger.info("EXTRACTION SUMMARY")
    logger.info("=" * 60)
    for endpoint_key, data in results.items():
        logger.info(f"{endpoint_key}: {len(data):,} records")

    logger.info(f"Output directory: {args.output_dir.absolute()}")
    logger.info("Done!")

    return results


if __name__ == "__main__":
    main()
