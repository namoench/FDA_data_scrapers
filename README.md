# FDA Data Scrapers

A collection of Python scripts for extracting FDA inspection, enforcement, and compliance data from various public sources.

## Data Sources

| Script | Source | Auth Required | Description |
|--------|--------|---------------|-------------|
| `fda_data_extractor.py` | FDA Data Dashboard API | Yes | Form 483 inspection classifications and citations |
| `scrape_import_refusals.py` | FDA Data Dashboard API | Yes | Import refusal records |
| `scrape_fei_registry.py` | openFDA API | No | Establishment registrations (FEI numbers) |
| `scrape_recalls.py` | openFDA API | No | Drug, device, and food enforcement/recall data |
| `scrape_warning_letters.py` | FDA Website | No | Warning letter metadata |
| `scrape_selenium.py` | FDA FOIA Reading Room | No | Form 483 PDFs and metadata |

## Installation

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/FDA_Data_Scrapers.git
cd FDA_Data_Scrapers

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# For Playwright-based scrapers, also run:
playwright install firefox
```

## Configuration

Some scripts require FDA Data Dashboard API credentials:

```bash
# Copy the example environment file
cp .env.example .env

# Edit .env with your credentials
# Request API access at: https://datadashboard.fda.gov/
```

## Usage

### FDA Data Dashboard API (requires credentials)

```bash
# Extract Form 483 inspection data
python fda_data_extractor.py

# Test mode (fetch 10 records to verify auth)
python fda_data_extractor.py --test

# Extract import refusals
python scrape_import_refusals.py
```

### openFDA API (no auth required)

```bash
# Extract recall/enforcement data
python scrape_recalls.py

# Extract FEI establishment registry
python scrape_fei_registry.py
python scrape_fei_registry.py --test  # Limited test run
```

### Web Scrapers (no auth required)

```bash
# Scrape warning letters metadata
python scrape_warning_letters.py
python scrape_warning_letters.py --test --visible  # Test with visible browser

# Scrape FOIA Reading Room (Form 483 PDFs)
python scrape_selenium.py
python scrape_selenium.py --test  # First 2 pages only
```

### PDF Downloads

```bash
# Download Form 483 PDFs (requires metadata from scrape_selenium.py)
python download_pdfs.py
python download_pdfs.py --test  # Download 5 PDFs only

# Download warning letter HTML pages
python download_warning_letters.py
```

### Data Matching

```bash
# Match PDF metadata to API inspection records
python match_records.py
```

## Output

All data is saved to the `data/` directory (gitignored):

```
data/
├── inspections_classifications.json
├── inspections_citations.json
├── import_refusals/
├── fei_registry/
├── recalls/
├── warning_letters/
├── 483_pdfs/
└── matched_records.csv
```

## Rate Limiting

All scripts include rate limiting to be respectful of FDA servers:
- API scripts: 1-2 second delays between requests
- Web scrapers: 2+ second delays between page loads
- Automatic retry with exponential backoff on errors

## License

MIT License - See LICENSE file for details.

## Disclaimer

This project is not affiliated with the FDA. Data is scraped from publicly available sources for research and educational purposes. Always verify data accuracy against official FDA sources for any compliance or regulatory decisions.
