# Samridh

An autonomous job-hunting and outreach agent. It discovers roles (job boards, ATS boards, YC / Hacker News hiring posts), filters and ranks them, fills application forms with Playwright, emails founders/hiring managers from your own template, verifies addresses, and tracks replies, rejections and bounces in a local dashboard.

## Setup
1. `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt && playwright install chromium`
2. Copy `.env.example` to `.env` and `profile.example.yaml` to `profile.yaml`; fill in your own details. Put your resume PDFs in `data/`.
3. `python -m jobagent daemon` (loops) and `python -m jobagent serve` (dashboard).

Nothing personal ships with this repo: credentials, resumes, the database and contact lists live in git-ignored local files. The agent only answers from your profile and resume and never invents facts.

## Data
`data/jobs.db` is a sanitised export: jobs, companies and outreach records (company, contact, role, status). Email subjects and bodies, form answers, resume paths and mailbox content are removed, and the owner's personal identifiers are scrubbed. The agent creates a fresh database on first run if you delete it.
