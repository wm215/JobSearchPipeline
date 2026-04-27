# CLAUDE.md — JobSearchPipeline

## What This Is

Unified job search automation pipeline: scrapes job listings from USAJobs and Indeed, scores them against William's profile, generates research/cover letters, identifies warm leads, and emails a daily digest.

## Architecture

**Single entry point**: `run.py --mode <mode>`

| Mode | What it does |
|------|-------------|
| `pipeline` | Full 4-stage: scan -> research -> prepare -> network -> email digest |
| `digest` | Calls `digest.py` — ADHD-optimized morning briefing email (Gmail + Sheets) |
| `followups` | Calls `followups.py` — follow-up reminder email (Sheets) |
| `morning` | Runs digest + followups together |
| `nightly` | Calls `tracker.py` — Gmail email extraction -> Google Sheet sync |
| `all` | pipeline + morning + nightly in sequence |

All modes accept `--dry-run`.

## Key Files

| File | Purpose |
|------|---------|
| `config.py` | All settings: locations, salary range, keywords, paths, thresholds |
| `scanner.py` | Stage 1: USAJobs API + Indeed/LinkedIn (python-jobspy) + Philadelphia portals + saved-jobs snapshot import |
| `scoring.py` | Qualification-gated scoring engine (0-100) with LinkedIn bonuses |
| `researcher.py` | Stage 2: generate talking points for top matches |
| `preparer.py` | Stage 3: generate cover letters |
| `networker.py` | Stage 4: identify warm leads from LinkedIn connections |
| `emailer.py` | Build and send HTML digest email |
| `db.py` | SQLite schema, migrations, CRUD helpers |
| `run.py` | Orchestrator + digest query helpers |
| `digest.py` | Morning action digest: starred emails, interviews, job updates, follow-ups |
| `followups.py` | Follow-up reminders: scans Google Sheet for 14+ day old applications |
| `tracker.py` | Gmail -> Google Sheet: extracts job applications from emails using OpenAI + regex |
| `sheets_helper.py` | Shared Google Sheets auth + CRUD helper |

## Database

**Path**: `data/job_pipeline.db`

Tables: `jobs`, `job_research`, `job_applications`, `warm_leads`, `pipeline_runs`

## Target Geography (priority order)

1. Philadelphia / New Jersey / Delaware (score: 10 pts)
2. Washington DC (score: 8 pts)
3. Chicago (score: 7 pts, suburb rejection active)
4. Remote / Telework (score: 6 pts)
5. Multiple Locations / Location Negotiable (score: 4-5 pts)

## Scoring Dimensions (100 pts max, base + bonus)

**Base dimensions (100 pts):**
- Domain (35 pts): HUD/housing tier 1, city/county tier 2, federal tier 3, adjacent tier 4
- Role (25 pts): strong/partial/weak title keyword match
- Skills (20 pts): core skill keyword count in description
- Salary (10 pts): based on advertised range ($100k+ = 10, missing = 5)
- Location (10 pts): target city matching (see above)

**LinkedIn bonus (up to +30 pts, capped so total never exceeds 100):**
- Connections (+10 max): HUD=10, City of Philly=8, CHA=7, PHA=7, Fannie=6, etc.
- Endorsements (+10 max): REO=8, HUD=8, Foreclosure=7, Property Mgmt=7, etc.
- Application history (+10 max): CHA=10, HUD=10, PHA=9, Fannie=9, etc.

Decision: TOP_MATCH >= 75 | REVIEW 55-74 | SKIP < 55

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
