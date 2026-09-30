# Niche Scout Pipeline

A real, running system that pulls free public trend data every day and
builds a history — not Claude browsing the web on request. This is the
data layer under the Nova skill.

## What it does

Once a day (06:00 UTC / 07:00 Algiers), GitHub's own servers run
`niche_scan.py`, which pulls:

1. **Google Trends** (via `pytrends`) — interest over time + rising related
   searches for your seed keywords, US + AU.
2. **TikTok Creative Center** — the public hashtag trend leaderboard
   (undocumented API, no login).
3. **Pinterest Trends** — trend data per keyword (undocumented API, no
   login).
4. **Amazon Movers & Shakers** — fastest-rising Children's Books titles
   right now (plain page scrape).

Every result is committed into `data/YYYY-MM-DD.json` (dated history) and
`data/latest.json` (most recent). Nova reads this file directly instead of
re-searching from scratch every time you ask — that's the difference
between a system and a one-off Claude answer.

## Honest reliability notes — read this before judging a "failed" run

Google Trends, TikTok, and Pinterest have **no official free API**. This
script uses the same undocumented endpoints and libraries that free/open
scrapers use. That means:

- **Google Trends**: usually reliable, occasionally rate-limited on shared
  IPs. Has retry logic built in.
- **TikTok Creative Center**: most likely to need a fix after the first
  real runs — it's an internal API that can change without notice.
- **Pinterest Trends**: same caveat as TikTok.
- **Amazon Movers & Shakers**: Amazon actively blocks datacenter IPs (which
  is what GitHub's servers are). This one may fail more than it succeeds.
  If it does, that's expected, not broken — Nova can still check Movers &
  Shakers live via Claude in Chrome when you ask, same as before.

Every source is wrapped so **one failing never breaks the others** — a
failed source shows up as `"status": "error"` with the real HTTP
status/response in the same JSON file, so it can be fixed from a real
error, not guessed at. This is exactly how the Amazon Ads pipeline's bugs
got fixed last time — expect 1-2 sources to need a real tweak once you see
actual GitHub Actions logs, same process, not a sign anything's wrong with
the approach.

## Setup (no credentials needed — this is public data only)

1. Create a new GitHub repo, e.g. `niche-scout-pipeline`. **Public is fine**
   — nothing in here is a credential or private sales number, it's public
   trend data plus your seed keyword list. (Say the word if you'd rather
   keep it private — that needs a small extra piece, same as the Ads live
   connector, since a private repo's raw files aren't fetchable without a
   token.)
2. Upload these files/folders exactly as they are:
   - `niche_scan.py`
   - `requirements.txt`
   - `config.json`
   - `.github/workflows/niche-scan.yml` (create the folders on GitHub's
     web upload — drag the whole `.github` folder in, don't flatten it)
3. Go to the repo's **Actions** tab → you should see "Niche Scan" listed →
   click **Run workflow** to trigger the first run manually instead of
   waiting for 06:00 UTC.
4. Check the run's log. If everything works, `data/latest.json` appears in
   the repo. If a source shows `"status": "error"`, paste that section back
   to Claude — it has the real cause already, no re-diagnosing from
   scratch.

## Editing what it tracks

Add or remove keywords/hashtags/categories in `config.json` any time —
no code change needed, just edit that file on GitHub and the next run
picks it up.
