"""
Niche Scout Pipeline — Houari's real-data niche-spotting system.

Runs on a schedule via GitHub Actions (see .github/workflows/niche-scan.yml),
pulls free public trend data from 4 sources, and commits it into data/ so a
real history builds up over time. No credentials, no logins, no personal API
keys required anywhere in this script — every source here is public data.

Design principle: NEVER crash the whole run because one source failed. Every
source is wrapped so a failure is recorded as data (status: "error", with the
real HTTP status + a snippet of the response body) rather than stopping the
other sources from completing. That failure record is exactly what lets a
human (or a future Claude session) fix the real cause instead of guessing.

Sources and their real-world reliability (be honest with yourself here):
  - Google Trends (pytrends): well-established library, can get rate-limited
    on shared CI IPs. Retries with backoff built in.
  - TikTok Creative Center: undocumented internal API, no official support.
    Most likely source to need adjustment after the first real runs.
  - Pinterest Trends: also an internal/undocumented API. Same caveat.
  - Amazon Movers & Shakers: plain HTML scrape. Amazon actively blocks
    datacenter IPs (which is what GitHub Actions runners are), so this one
    may fail more often than it succeeds. If it does, that's expected —
    Nova falls back to checking it live via Claude in Chrome instead.
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
CONFIG_PATH = os.path.join(HERE, "config.json")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 1. Google Trends (pytrends)
# ---------------------------------------------------------------------------

def scan_google_trends(cfg):
    result = {"source": "google_trends", "fetched_at": now_iso(), "status": "ok", "data": []}
    try:
        from pytrends.request import TrendReq
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"pytrends not installed / import failed: {e}"
        return result

    keywords = cfg["google_trends"]["keywords"]
    timeframe = cfg["google_trends"]["timeframe"]
    geo_list = cfg["google_trends"]["geo_list"]

    for geo in geo_list:
        try:
            pytrends = TrendReq(hl="en-US", tz=0)
        except Exception as e:
            result.setdefault("partial_errors", []).append(
                {"geo": geo, "stage": "TrendReq init", "error": str(e)}
            )
            continue
        # pytrends allows max 5 keywords per payload
        for i in range(0, len(keywords), 5):
            batch = keywords[i : i + 5]
            attempt = 0
            while attempt < 3:
                try:
                    pytrends.build_payload(batch, timeframe=timeframe, geo=geo)
                    interest = pytrends.interest_over_time()
                    for kw in batch:
                        if kw not in interest.columns:
                            continue
                        series = interest[kw]
                        if series.empty:
                            continue
                        first_half = series.iloc[: len(series) // 2].mean()
                        second_half = series.iloc[len(series) // 2 :].mean()
                        trend_direction = "rising" if second_half > first_half * 1.15 else (
                            "falling" if second_half < first_half * 0.85 else "flat"
                        )
                        result["data"].append(
                            {
                                "geo": geo,
                                "keyword": kw,
                                "latest_value": int(series.iloc[-1]),
                                "avg_first_half": round(float(first_half), 1),
                                "avg_second_half": round(float(second_half), 1),
                                "trend_direction": trend_direction,
                            }
                        )
                    try:
                        related = pytrends.related_queries()
                        for kw in batch:
                            r = related.get(kw)
                            if not r or r.get("rising") is None or r["rising"].empty:
                                continue
                            rising_terms = r["rising"].head(5).to_dict("records")
                            result["data"].append(
                                {
                                    "geo": geo,
                                    "keyword": kw,
                                    "rising_related_queries": rising_terms,
                                }
                            )
                    except Exception:
                        pass  # related queries are a bonus, not critical
                    break
                except Exception as e:
                    attempt += 1
                    if attempt >= 3:
                        result.setdefault("partial_errors", []).append(
                            {"geo": geo, "batch": batch, "error": str(e)}
                        )
                    else:
                        time.sleep(5 * attempt)
            time.sleep(2)  # be polite between batches
    if result.get("partial_errors") and not result["data"]:
        result["status"] = "error"
        result["error"] = "all batches failed, see partial_errors"
    return result


# ---------------------------------------------------------------------------
# 2. TikTok Creative Center — public trend leaderboard (undocumented API)
# ---------------------------------------------------------------------------

def scan_tiktok_creative_center(cfg):
    result = {"source": "tiktok_creative_center", "fetched_at": now_iso(), "status": "ok", "data": []}
    tcfg = cfg["tiktok_creative_center"]
    base = "https://ads.tiktok.com/creative_radar_api/v1/popular_trend/hashtag/list"
    headers = dict(BROWSER_HEADERS)
    headers["Referer"] = "https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en"

    any_ok = False
    for country in tcfg["country_codes"]:
        params = {
            "page": 1,
            "limit": 50,
            "period": tcfg["period_days"],
            "order_by": "popularity",
            "country_code": country,
        }
        try:
            resp = requests.get(base, params=params, headers=headers, timeout=20)
            if resp.status_code != 200:
                result.setdefault("partial_errors", []).append(
                    {
                        "country": country,
                        "http_status": resp.status_code,
                        "body_snippet": resp.text[:500],
                    }
                )
                continue
            payload = resp.json()
            hashtags = (
                payload.get("data", {}).get("list")
                or payload.get("data", {}).get("hashtags")
                or []
            )
            for h in hashtags:
                result["data"].append(
                    {
                        "country": country,
                        "hashtag": h.get("hashtag_name") or h.get("name"),
                        "rank": h.get("rank"),
                        "trend": h.get("trend"),  # some responses mark rising/flat
                        "raw": h,
                    }
                )
            any_ok = True
        except Exception as e:
            result.setdefault("partial_errors", []).append(
                {"country": country, "error": str(e), "trace": traceback.format_exc()[:800]}
            )

    if not any_ok:
        result["status"] = "error"
        result["error"] = (
            "TikTok Creative Center endpoint did not return usable data for any country. "
            "This is an undocumented API — check partial_errors for the real HTTP status/body. "
            "Common cause: TikTok changed the endpoint or now requires a session token this "
            "script doesn't have. Fix from the real error, don't guess."
        )
    return result


# ---------------------------------------------------------------------------
# 3. Pinterest Trends — public trend page (undocumented API)
# ---------------------------------------------------------------------------

def scan_pinterest_trends(cfg):
    result = {"source": "pinterest_trends", "fetched_at": now_iso(), "status": "ok", "data": []}
    headers = dict(BROWSER_HEADERS)
    any_ok = False

    for kw in cfg["pinterest_trends"]["keywords"]:
        try:
            resp = requests.get(
                "https://trends.pinterest.com/api/trends/search",
                params={"term": kw, "region": "US"},
                headers=headers,
                timeout=20,
            )
            if resp.status_code != 200:
                result.setdefault("partial_errors", []).append(
                    {"keyword": kw, "http_status": resp.status_code, "body_snippet": resp.text[:500]}
                )
                continue
            payload = resp.json()
            result["data"].append({"keyword": kw, "raw": payload})
            any_ok = True
        except Exception as e:
            result.setdefault("partial_errors", []).append(
                {"keyword": kw, "error": str(e), "trace": traceback.format_exc()[:500]}
            )
        time.sleep(1)

    if not any_ok:
        result["status"] = "error"
        result["error"] = (
            "Pinterest Trends endpoint guess did not work for any keyword. This is an "
            "undocumented API endpoint (a best-effort guess, not confirmed) — check "
            "partial_errors for the real response and fix the URL/params from that, or "
            "swap this for a Claude-in-Chrome manual check if it never works."
        )
    return result


# ---------------------------------------------------------------------------
# 4. Amazon Movers & Shakers — plain HTML scrape (highest block risk)
# ---------------------------------------------------------------------------

def scan_amazon_movers_shakers(cfg):
    result = {"source": "amazon_movers_shakers", "fetched_at": now_iso(), "status": "ok", "data": []}
    headers = dict(BROWSER_HEADERS)
    any_ok = False

    try:
        from bs4 import BeautifulSoup
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"beautifulsoup4 not installed: {e}"
        return result

    for label, url in cfg["amazon_movers_shakers"]["categories"].items():
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code != 200:
                result.setdefault("partial_errors", []).append(
                    {"category": label, "http_status": resp.status_code, "body_snippet": resp.text[:500]}
                )
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select("#zg-ordered-list li") or soup.select("[data-testid='p13n-sc-uncoverable-faceout']")
            if not items:
                # structure guess failed — record page length so we can diagnose without re-fetching
                result.setdefault("partial_errors", []).append(
                    {
                        "category": label,
                        "http_status": 200,
                        "note": "page loaded but no items matched known selectors — Amazon likely changed markup or served a block/captcha page",
                        "body_snippet": resp.text[:500],
                    }
                )
                continue
            for item in items[:30]:
                title_el = item.select_one("img")
                title = title_el.get("alt") if title_el else (item.get_text(strip=True)[:150])
                link_el = item.select_one("a")
                link = link_el.get("href") if link_el else None
                result["data"].append({"category": label, "title": title, "link": link})
            any_ok = True
        except Exception as e:
            result.setdefault("partial_errors", []).append(
                {"category": label, "error": str(e), "trace": traceback.format_exc()[:500]}
            )
        time.sleep(2)

    if not any_ok:
        result["status"] = "error"
        result["error"] = (
            "Amazon scrape returned nothing usable for any category. Amazon actively blocks "
            "scraping from datacenter IPs (which GitHub Actions runners are) — this is the "
            "most likely source to fail here. If it consistently fails, drop it from the "
            "automated pipeline and have Nova check Movers & Shakers live via Claude in "
            "Chrome instead when asked, rather than fighting Amazon's bot detection for free."
        )
    return result


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()
    os.makedirs(DATA_DIR, exist_ok=True)

    scans = {
        "google_trends": scan_google_trends,
        "tiktok_creative_center": scan_tiktok_creative_center,
        "pinterest_trends": scan_pinterest_trends,
        "amazon_movers_shakers": scan_amazon_movers_shakers,
    }

    run_record = {"run_at": now_iso(), "sources": {}}
    exit_code = 0

    for name, fn in scans.items():
        print(f"--- running {name} ---", flush=True)
        try:
            out = fn(cfg)
        except Exception as e:
            out = {
                "source": name,
                "fetched_at": now_iso(),
                "status": "error",
                "error": f"unhandled exception: {e}",
                "trace": traceback.format_exc()[:1000],
            }
        print(f"    status: {out.get('status')}  rows: {len(out.get('data', []))}", flush=True)
        if out.get("status") == "error":
            exit_code = 1  # signal partial failure to the workflow, but we already wrote data below
        run_record["sources"][name] = out

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dated_path = os.path.join(DATA_DIR, f"{date_str}.json")
    latest_path = os.path.join(DATA_DIR, "latest.json")

    with open(dated_path, "w") as f:
        json.dump(run_record, f, indent=2, default=str)
    with open(latest_path, "w") as f:
        json.dump(run_record, f, indent=2, default=str)

    print(f"Wrote {dated_path} and {latest_path}")
    # Never fail the whole workflow run just because one source errored —
    # the point is to keep the daily history unbroken. A source-level error
    # is visible in the committed JSON itself.
    sys.exit(0)


if __name__ == "__main__":
    main()
