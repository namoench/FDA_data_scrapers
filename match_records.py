#!/usr/bin/env python3
"""
Match FDA 483 PDFs to API inspection data.

Joins the Reading Room PDF metadata with the Data Dashboard API inspection
records using FEI Number and date proximity matching.
"""

import json
import csv
import logging
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

# Configuration
OUTPUT_DIR = Path(__file__).parent / "data"
PDF_METADATA_FILE = OUTPUT_DIR / "reading_room_483_only.json"
INSPECTIONS_FILE = OUTPUT_DIR / "inspections_classifications.json"
CITATIONS_FILE = OUTPUT_DIR / "inspections_citations.json"
MATCHED_OUTPUT = OUTPUT_DIR / "matched_records.csv"
UNMATCHED_OUTPUT = OUTPUT_DIR / "unmatched_pdfs.csv"

# Matching parameters
DATE_TOLERANCE_DAYS = 30  # Allow dates to differ by up to 30 days

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_date(date_str: str) -> datetime | None:
    """Parse various date formats to datetime."""
    if not date_str:
        return None

    formats = [
        "%m/%d/%Y",      # 09/23/2025
        "%Y-%m-%d",      # 2025-09-23
        "%m-%d-%Y",      # 09-23-2025
        "%Y/%m/%d",      # 2025/09/23
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue

    # Try ISO format with time
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        pass

    return None


def load_pdf_metadata(filepath: Path) -> list:
    """Load PDF metadata from JSON."""
    if not filepath.exists():
        logger.error(f"PDF metadata file not found: {filepath}")
        return []

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    logger.info(f"Loaded {len(data)} PDF records from {filepath.name}")
    return data


def load_api_data(inspections_file: Path, citations_file: Path) -> tuple[list, list]:
    """Load inspection and citation data from API JSON files."""
    inspections = []
    citations = []

    if inspections_file.exists():
        with open(inspections_file, "r", encoding="utf-8") as f:
            inspections = json.load(f)
        logger.info(f"Loaded {len(inspections)} inspections from {inspections_file.name}")
    else:
        logger.warning(f"Inspections file not found: {inspections_file}")

    if citations_file.exists():
        with open(citations_file, "r", encoding="utf-8") as f:
            citations = json.load(f)
        logger.info(f"Loaded {len(citations)} citations from {citations_file.name}")
    else:
        logger.warning(f"Citations file not found: {citations_file}")

    return inspections, citations


def build_inspection_index(inspections: list) -> dict:
    """
    Build an index of inspections by FEI number.

    Returns dict: {fei_number: [list of inspections]}
    """
    index = defaultdict(list)

    for insp in inspections:
        fei = str(insp.get("FEINumber", "")).strip()
        if fei:
            index[fei].append(insp)

    logger.info(f"Indexed {len(index)} unique FEI numbers")
    return dict(index)


def find_matching_inspection(
    pdf_record: dict,
    inspection_index: dict,
    date_tolerance_days: int = DATE_TOLERANCE_DAYS
) -> dict | None:
    """
    Find the best matching inspection for a PDF record.

    Matches on FEI number first, then finds closest date.
    """
    pdf_fei = str(pdf_record.get("fei_number", "")).strip()
    pdf_date = parse_date(pdf_record.get("record_date", ""))

    if not pdf_fei:
        return None

    # Get inspections for this FEI
    fei_inspections = inspection_index.get(pdf_fei, [])
    if not fei_inspections:
        return None

    if not pdf_date:
        # No date to match, return most recent inspection
        return fei_inspections[0]

    # Find closest date match
    best_match = None
    best_delta = timedelta(days=999999)

    for insp in fei_inspections:
        insp_date = parse_date(insp.get("InspectionEndDate", ""))
        if not insp_date:
            continue

        delta = abs(pdf_date - insp_date)
        if delta < best_delta:
            best_delta = delta
            best_match = insp

    # Check if within tolerance
    if best_match and best_delta <= timedelta(days=date_tolerance_days):
        return best_match

    # If no match within tolerance, still return best match but flag it
    if best_match:
        best_match = dict(best_match)  # Copy to avoid modifying original
        best_match["_date_delta_days"] = best_delta.days
        best_match["_fuzzy_match"] = True
        return best_match

    return None


def match_all_records(
    pdf_records: list,
    inspections: list,
    date_tolerance_days: int = DATE_TOLERANCE_DAYS
) -> tuple[list, list]:
    """
    Match all PDF records to inspections.

    Returns:
        Tuple of (matched records, unmatched records)
    """
    inspection_index = build_inspection_index(inspections)

    matched = []
    unmatched = []

    for pdf in pdf_records:
        match = find_matching_inspection(pdf, inspection_index, date_tolerance_days)

        if match:
            combined = {
                # PDF metadata
                "pdf_record_date": pdf.get("record_date"),
                "pdf_company_name": pdf.get("company_name"),
                "pdf_fei_number": pdf.get("fei_number"),
                "pdf_record_type": pdf.get("record_type"),
                "pdf_url": pdf.get("pdf_url"),
                "pdf_media_id": pdf.get("pdf_media_id"),
                "pdf_state": pdf.get("state"),
                "pdf_country": pdf.get("country"),
                "pdf_establishment_type": pdf.get("establishment_type"),
                # API inspection data
                "api_inspection_id": match.get("InspectionID"),
                "api_fei_number": match.get("FEINumber"),
                "api_legal_name": match.get("LegalName"),
                "api_inspection_end_date": match.get("InspectionEndDate"),
                "api_fiscal_year": match.get("FiscalYear"),
                "api_classification": match.get("Classification"),
                "api_classification_code": match.get("ClassificationCode"),
                "api_project_area": match.get("ProjectArea"),
                "api_product_type": match.get("ProductType"),
                "api_posted_citations": match.get("PostedCitations"),
                "api_firm_profile": match.get("FirmProfile"),
                # Match quality
                "match_type": "fuzzy" if match.get("_fuzzy_match") else "exact",
                "date_delta_days": match.get("_date_delta_days", 0),
            }
            matched.append(combined)
        else:
            unmatched.append(pdf)

    logger.info(f"Matched: {len(matched)}, Unmatched: {len(unmatched)}")
    return matched, unmatched


def save_results(matched: list, unmatched: list, matched_path: Path, unmatched_path: Path):
    """Save matched and unmatched records to CSV."""
    # Save matched records
    if matched:
        fieldnames = list(matched[0].keys())
        with open(matched_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(matched)
        logger.info(f"Saved {len(matched)} matched records to: {matched_path}")
    else:
        logger.warning("No matched records to save")

    # Save unmatched records
    if unmatched:
        fieldnames = list(unmatched[0].keys())
        with open(unmatched_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(unmatched)
        logger.info(f"Saved {len(unmatched)} unmatched records to: {unmatched_path}")


def main():
    parser = argparse.ArgumentParser(description="Match FDA 483 PDFs to API inspection data")
    parser.add_argument("--pdf-metadata", type=Path, default=PDF_METADATA_FILE,
                        help="Path to PDF metadata JSON")
    parser.add_argument("--inspections", type=Path, default=INSPECTIONS_FILE,
                        help="Path to inspections JSON from API")
    parser.add_argument("--citations", type=Path, default=CITATIONS_FILE,
                        help="Path to citations JSON from API")
    parser.add_argument("--tolerance", type=int, default=DATE_TOLERANCE_DAYS,
                        help=f"Date tolerance in days (default: {DATE_TOLERANCE_DAYS})")
    parser.add_argument("--output", type=Path, default=MATCHED_OUTPUT,
                        help="Output path for matched records CSV")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("FDA 483 PDF to API Inspection Matcher")
    logger.info("=" * 60)

    # Load data
    pdf_records = load_pdf_metadata(args.pdf_metadata)
    inspections, citations = load_api_data(args.inspections, args.citations)

    if not pdf_records:
        logger.error("No PDF records to match. Run scrape_selenium.py first.")
        sys.exit(1)

    if not inspections:
        logger.error("No inspection data to match against. Run fda_data_extractor.py first.")
        sys.exit(1)

    # Match records
    logger.info(f"Matching with {args.tolerance} day tolerance...")
    matched, unmatched = match_all_records(
        pdf_records=pdf_records,
        inspections=inspections,
        date_tolerance_days=args.tolerance
    )

    # Save results
    save_results(matched, unmatched, args.output, UNMATCHED_OUTPUT)

    # Statistics
    logger.info("=" * 60)
    logger.info("MATCHING SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total PDF records: {len(pdf_records)}")
    logger.info(f"Matched to inspections: {len(matched)} ({100*len(matched)/len(pdf_records):.1f}%)")
    logger.info(f"Unmatched: {len(unmatched)} ({100*len(unmatched)/len(pdf_records):.1f}%)")

    if matched:
        exact_matches = sum(1 for m in matched if m["match_type"] == "exact")
        fuzzy_matches = sum(1 for m in matched if m["match_type"] == "fuzzy")
        logger.info(f"  Exact matches: {exact_matches}")
        logger.info(f"  Fuzzy matches: {fuzzy_matches}")

    logger.info(f"Output: {args.output.absolute()}")


if __name__ == "__main__":
    main()
