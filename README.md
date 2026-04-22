# JobSearchPipeline

Primary end-to-end job pipeline: scanner → researcher → preparer → networker, orchestrated by `run.py` for scheduled execution.

## Repository status

- **Lifecycle:** Active (system of record for job automation)
- **Owner system:** Unified Job Search Pipeline
- **Execution:** `python3 run.py --mode pipeline|morning|nightly|all`

## Scope

- `scanner.py`: ingest jobs (USAJobs, portals, jobspy), score, persist
- `researcher.py`: enrich high-fit jobs
- `preparer.py`: generate application materials
- `networker.py`: identify warm leads and draft outreach
- `digest.py`, `followups.py`, `tracker.py`: communication and tracking support

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install requests python-dotenv pandas beautifulsoup4 pytest
python3 run.py --mode pipeline
```

## Required environment variables (minimum)

- `EMAIL_USER` / `EMAIL_FROM` / `SENDER_EMAIL`
- `EMAIL_PASS` / `EMAIL_PASSWORD` / `SENDER_PASSWORD`
- `EMAIL_TO`
- `USAJOBS_API_KEY`
- `APIFY_API_TOKEN` (optional fallback paths exist)

## Testing

```bash
pytest -q tests/test_db.py tests/test_emailer.py tests/test_networker.py tests/test_preparer.py tests/test_scanner.py tests/test_scoring.py
```
