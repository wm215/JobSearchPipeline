# CLAUDE.md — JobSearchPipeline

## What This Is

Unified job search automation pipeline: scrapes job listings from USAJobs and Indeed, scores them against William's profile, generates research/cover letters, identifies warm leads, and emails a daily digest.

## Architecture

**Single entry point**: `run.py --mode <mode>`

| Mode | What it does |
|------|-------------|
| `pipeline` | Full 4-stage: scan -> research -> prepare -> network -> email digest |
| `digest` | Delegates to `~/daily_action_digest.py` (morning briefing email) |
| `followups` | Delegates to `~/follow_up_reminders.py` (follow-up reminder email) |
| `morning` | Runs digest + followups together |
| `nightly` | Delegates to `~/fully_automated_job_tracker.py` (Gmail -> Google Sheet) |
| `all` | pipeline + morning + nightly in sequence |

All modes accept `--dry-run`.

## Key Files

| File | Purpose |
|------|---------|
| `config.py` | All settings: locations, salary range, keywords, paths, thresholds |
| `scanner.py` | Stage 1: USAJobs API + Indeed/Apify scraping + saved-jobs snapshot import |
| `scoring.py` | Qualification-gated scoring engine (0-100, five dimensions) |
| `researcher.py` | Stage 2: generate talking points for top matches |
| `preparer.py` | Stage 3: generate cover letters |
| `networker.py` | Stage 4: identify warm leads from LinkedIn connections |
| `emailer.py` | Build and send HTML digest email |
| `db.py` | SQLite schema, migrations, CRUD helpers |
| `run.py` | Orchestrator + digest query helpers |

## Database

**Path**: `data/job_pipeline.db`

Tables: `jobs`, `job_research`, `job_applications`, `warm_leads`, `pipeline_runs`

## Target Geography (priority order)

1. Philadelphia / New Jersey / Delaware (score: 10 pts)
2. Washington DC (score: 8 pts)
3. Chicago (score: 7 pts, suburb rejection active)
4. Remote / Telework (score: 6 pts)
5. Multiple Locations / Location Negotiable (score: 4-5 pts)

## Scoring Dimensions (100 pts total)

- Domain (35 pts): HUD/housing tier 1, city/county tier 2, federal tier 3, adjacent tier 4
- Role (25 pts): strong/partial/weak title keyword match
- Skills (20 pts): core skill keyword count in description
- Salary (10 pts): based on advertised range ($100k+ = 10, missing = 5)
- Location (10 pts): target city matching (see above)

Decision: AUTO_APPLY >= 75 | REVIEW 55-74 | SKIP < 55

## USAJOBS Saved-Jobs Snapshot

Paste your USAJobs saved-jobs page text into `data/usajobs_saved_jobs_snapshot.txt`. Format:

```
Program Analyst GS-13
Accepting applications
Department of Housing and Urban Development
Washington, DC
Closes 5/01/2026
https://www.usajobs.gov/job/828364200
```

The URL line is optional but recommended (without it, digest links are non-clickable placeholders). GS grades in titles are parsed for salary data.

## Running Tests

```bash
cd ~/Documents/GitHub/JobSearchPipeline-private
python3 -m pytest tests/ -v
```

## Cron Schedule

```
7:30 AM daily     run.py --mode digest
8:00 AM daily     run.py --mode followups
8am-8pm/2h daily  run.py --mode pipeline
9:00 PM daily     run.py --mode nightly
```

## Common Commands

```bash
# Manual full pipeline run
python3 run.py --mode pipeline

# Check top matches
sqlite3 data/job_pipeline.db "SELECT title, company, match_score, location FROM jobs WHERE match_score >= 70 ORDER BY match_score DESC LIMIT 20;"

# Check by source
sqlite3 data/job_pipeline.db "SELECT source, COUNT(*), ROUND(AVG(match_score),1) FROM jobs GROUP BY source;"
```
