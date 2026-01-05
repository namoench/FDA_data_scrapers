#!/usr/bin/env python3
"""
Download FDA 483 PDFs from the Reading Room metadata.

Downloads PDFs organized by FEI number with proper naming,
rate limiting, and failure handling.
"""

import json
import csv
import re
import time
import logging
import sys
import argparse
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# Configuration
OUTPUT_DIR = Path(__file__).parent / "data"
PDF_DIR = OUTPUT_DIR / "483_pdfs"
METADATA_FILE = OUTPUT_DIR / "reading_room_483_only.json"
MANIFEST_FILE = OUTPUT_DIR / "pdf_manifest.csv"
FAILED_FILE = OUTPUT_DIR / "pdf_download_failures.json"

DEFAULT_DELAY = 1.5  # Seconds between downloads
MAX_RETRIES = 3
TIMEOUT = 60

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def sanitize_filename(name: str, max_length: int = 50) -> str:
    """Sanitize a string for use as a filename."""
    # Remove/replace invalid characters
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', '_', name)
    name = re.sub(r'[^\w\-_.]', '', name)
    # Truncate if too long
    if len(name) > max_length:
        name = name[:max_length]
    return name.strip('_.')


def download_pdf(record: dict, session: requests.Session, delay: float = DEFAULT_DELAY) -> dict:
    """
    Download a single PDF file.

    Returns dict with download status and file path.
    """
    pdf_url = record.get("pdf_url")
    if not pdf_url:
        return {"success": False, "error": "No PDF URL", "record": record}

    fei = record.get("fei_number", "unknown")
    company = sanitize_filename(record.get("company_name", "unknown"))
    record_date = record.get("record_date", "").replace("/", "-")
    record_type = record.get("record_type", "483")

    # Create FEI directory
    fei_dir = PDF_DIR / str(fei)
    fei_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename: date_company_type.pdf
    filename = f"{record_date}_{company}_{record_type}.pdf"
    filepath = fei_dir / filename

    # Skip if already downloaded
    if filepath.exists() and filepath.stat().st_size > 0:
        logger.debug(f"Skipping existing: {filepath}")
        return {
            "success": True,
            "skipped": True,
            "filepath": str(filepath),
            "record": record
        }

    # Download with retries
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(delay)  # Rate limiting

            response = session.get(pdf_url, timeout=TIMEOUT, stream=True)

            if response.status_code == 429:
                # Rate limited
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning(f"Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after)
                continue

            response.raise_for_status()

            # Verify it's a PDF
            content_type = response.headers.get("Content-Type", "")
            if "pdf" not in content_type.lower() and not pdf_url.endswith(".pdf"):
                logger.warning(f"Unexpected content type for {pdf_url}: {content_type}")

            # Save file
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            file_size = filepath.stat().st_size
            logger.info(f"Downloaded: {filepath.name} ({file_size:,} bytes)")

            return {
                "success": True,
                "filepath": str(filepath),
                "size": file_size,
                "record": record
            }

        except requests.exceptions.RequestException as e:
            logger.warning(f"Attempt {attempt + 1}/{MAX_RETRIES} failed for {pdf_url}: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(5 * (attempt + 1))  # Exponential backoff
            continue

    return {
        "success": False,
        "error": f"Failed after {MAX_RETRIES} attempts",
        "record": record
    }


def download_all_pdfs(
    records: list,
    delay: float = DEFAULT_DELAY,
    max_downloads: int = None,
    resume: bool = True
) -> tuple[list, list]:
    """
    Download all PDFs from the records list.

    Args:
        records: List of record dicts with pdf_url field
        delay: Delay between downloads
        max_downloads: Limit downloads (for testing)
        resume: Skip already downloaded files

    Returns:
        Tuple of (successful downloads, failed downloads)
    """
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    # Filter records with PDF URLs
    records_with_urls = [r for r in records if r.get("pdf_url")]
    logger.info(f"Records with PDF URLs: {len(records_with_urls)}")

    if max_downloads:
        records_with_urls = records_with_urls[:max_downloads]
        logger.info(f"Limiting to {max_downloads} downloads")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/pdf,*/*",
    })

    successful = []
    failed = []

    logger.info(f"Starting download of {len(records_with_urls)} PDFs...")

    for i, record in enumerate(records_with_urls, 1):
        logger.info(f"Processing {i}/{len(records_with_urls)}: {record.get('company_name', 'Unknown')[:40]}")

        result = download_pdf(record, session, delay)

        if result.get("success"):
            successful.append(result)
        else:
            failed.append(result)

        # Progress update every 50 downloads
        if i % 50 == 0:
            logger.info(f"Progress: {i}/{len(records_with_urls)} ({len(successful)} OK, {len(failed)} failed)")

    return successful, failed


def save_manifest(results: list, filepath: Path):
    """Save download manifest to CSV."""
    if not results:
        logger.warning("No results to save to manifest")
        return

    fieldnames = [
        "fei_number", "company_name", "record_date", "record_type",
        "pdf_url", "local_filepath", "file_size", "download_status"
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for result in results:
            record = result.get("record", {})
            row = {
                "fei_number": record.get("fei_number"),
                "company_name": record.get("company_name"),
                "record_date": record.get("record_date"),
                "record_type": record.get("record_type"),
                "pdf_url": record.get("pdf_url"),
                "local_filepath": result.get("filepath"),
                "file_size": result.get("size"),
                "download_status": "success" if result.get("success") else "failed"
            }
            writer.writerow(row)

    logger.info(f"Saved manifest to: {filepath}")


def main():
    parser = argparse.ArgumentParser(description="Download FDA 483 PDFs")
    parser.add_argument("--test", action="store_true", help="Test mode: download only 5 PDFs")
    parser.add_argument("--max", type=int, help="Maximum PDFs to download")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"Delay between downloads (default: {DEFAULT_DELAY}s)")
    parser.add_argument("--metadata", type=Path, default=METADATA_FILE, help="Path to metadata JSON file")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("FDA 483 PDF Downloader")
    logger.info("=" * 60)

    # Load metadata
    if not args.metadata.exists():
        logger.error(f"Metadata file not found: {args.metadata}")
        logger.error("Run scrape_selenium.py first to get the metadata")
        sys.exit(1)

    with open(args.metadata, "r", encoding="utf-8") as f:
        records = json.load(f)

    logger.info(f"Loaded {len(records)} records from {args.metadata}")

    # Determine download limit
    max_downloads = 5 if args.test else args.max

    # Download PDFs
    successful, failed = download_all_pdfs(
        records=records,
        delay=args.delay,
        max_downloads=max_downloads
    )

    # Save manifest
    all_results = successful + failed
    save_manifest(all_results, MANIFEST_FILE)

    # Save failures for retry
    if failed:
        with open(FAILED_FILE, "w", encoding="utf-8") as f:
            json.dump([r["record"] for r in failed], f, indent=2)
        logger.info(f"Saved {len(failed)} failures to: {FAILED_FILE}")

    # Summary
    logger.info("=" * 60)
    logger.info("DOWNLOAD SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total processed: {len(all_results)}")
    logger.info(f"Successful: {len(successful)}")
    logger.info(f"Skipped (existing): {sum(1 for r in successful if r.get('skipped'))}")
    logger.info(f"Failed: {len(failed)}")
    logger.info(f"PDF directory: {PDF_DIR.absolute()}")
    logger.info(f"Manifest: {MANIFEST_FILE.absolute()}")


if __name__ == "__main__":
    main()
