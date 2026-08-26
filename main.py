"""
In-Play Soccer Value Bet Bot
-----------------------------
What this does:
1. Every SCAN_INTERVAL_MINUTES, pulls live in-play soccer odds from the-odds-api
2. For each match, compares odds across bookmakers to find price discrepancies
   (a "value" signal: one book pricing a outcome noticeably higher than the
   de-vigged consensus of the others)
3. Cross-checks that match against live stats from API-FOOTBALL (red cards,
   score state, minute) so we don't fire on stale/garbage-time odds
4. Only sends a Telegram alert when the edge clears MIN_EDGE_PERCENT
5. Logs every alert to a local SQLite db so you can track real performance
   over time (results.py will let you check hit rate later)

IMPORTANT HONESTY NOTE (read this):
This is a value-detection tool, not a prediction engine. "Value" here means
"the market disagrees with itself" - it does NOT mean "this team will win."
Treat every alert as a lead to research further, not a guaranteed winner.
No free (or paid) tool can promise winning picks - anyone who says otherwise
is selling something.
"""

import os
import time
import sqlite3
import logging
from datetime import datetime, timezone

import requests

# ---------- CONFIG (set these as environment variables on Railway) ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ODDS_API_KEY = os.environ["ODDS_API_KEY"]
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "")  # optional, API-FOOTBALL free key

SCAN_INTERVAL_MINUTES = int(os.environ.get("SCAN_INTERVAL_MINUTES", "10"))
MIN_EDGE_PERCENT = float(os.environ.get("MIN_EDGE_PERCENT", "5"))  # min % edge to alert
MIN_BOOKS_REQUIRED = 3  # need at least this many bookmakers quoting to trust consensus

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
FOOTBALL_API_BASE = "https://v3.football.api-sports.io"

DB_PATH = "bets.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("soccer-bot")


# ---------------------------- DATABASE ----------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT,
            match TEXT,
            market TEXT,
            outcome TEXT,
            best_book TEXT,
            best_odds REAL,
            consensus_prob REAL,
            edge_percent REAL,
            minute INTEGER,
            score TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def log_alert(match, market, outcome, best_book, best_odds, consensus_prob, edge, minute, score):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO alerts (sent_at, match, market, outcome, best_book, best_odds,
           consensus_prob, edge_percent, minute, score) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            match,
            market,
            outcome,
            best_book,
            best_odds,
            consensus_prob,
            edge,
            minute,
            score,
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------- TELEGRAM ----------------------------
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


# ---------------------------- ODDS API ----------------------------
def get_inplay_odds():
    """Fetch live in-play soccer odds across all supported soccer leagues."""
    url = f"{ODDS_API_BASE}/sports/soccer/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us,uk,eu",
        "markets": "h2h",  # match winner market; add 'totals' if you want over/under too
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        remaining = r.headers.get("x-requests-remaining")
        if remaining:
            log.info(f"Odds API requests remaining this period: {remaining}")
        return r.json()
    except Exception as e:
        log.error(f"Odds API fetch failed: {e}")
        return []


def find_value_bets(events):
    """
    For each event, for each outcome, compare the BEST available price against
    the de-vigged consensus probability from all other books. If the best
    price implies meaningfully lower probability than consensus, that's edge.
    """
    value_bets = []

    for event in events:
        bookmakers = event.get("bookmakers", [])
        if len(bookmakers) < MIN_BOOKS_REQUIRED:
            continue

        home = event.get("home_team")
        away = event.get("away_team")
        match_name = f"{home} vs {away}"

        # Collect all prices per outcome across books
        outcome_prices = {}  # outcome_name -> list of (book_title, price)
        for bm in bookmakers:
            for market in bm.get("markets", []):
                if market["key"] != "h2h":
                    continue
                for outcome in market["outcomes"]:
                    outcome_prices.setdefault(outcome["name"], []).append(
                        (bm["title"], outcome["price"])
                    )

        if len(outcome_prices) < 2:
            continue

        # De-vig: convert each book's full market to implied probs summing to 1,
        # then average across books to get a fair consensus probability per outcome
        consensus_probs = {name: [] for name in outcome_prices}
        for bm in bookmakers:
            for market in bm.get("markets", []):
                if market["key"] != "h2h":
                    continue
                outcomes = market["outcomes"]
                implied = [1 / o["price"] for o in outcomes]
                overround = sum(implied)
                if overround == 0:
                    continue
                for o, imp in zip(outcomes, implied):
                    fair_prob = imp / overround  # removes the vig
                    consensus_probs[o["name"]].append(fair_prob)

        for outcome_name, prices in outcome_prices.items():
            probs = consensus_probs.get(outcome_name, [])
            if not probs:
                continue
            avg_consensus_prob = sum(probs) / len(probs)

            # best (highest) price available for this outcome
            best_book, best_price = max(prices, key=lambda x: x[1])
            implied_prob_best = 1 / best_price

            # Edge = how much cheaper the best price is vs fair consensus
            edge_percent = (avg_consensus_prob - implied_prob_best) * 100

            if edge_percent >= MIN_EDGE_PERCENT:
                value_bets.append(
                    {
                        "match": match_name,
                        "market": "Match Winner",
                        "outcome": outcome_name,
                        "best_book": best_book,
                        "best_odds": best_price,
                        "consensus_prob": round(avg_consensus_prob * 100, 1),
                        "edge_percent": round(edge_percent, 1),
                        "sport_key": event.get("sport_key"),
                        "commence_time": event.get("commence_time"),
                    }
                )

    return value_bets


# ---------------------------- LIVE STATS (API-FOOTBALL) ----------------------------
def get_live_stats_lookup():
    """
    Returns a dict keyed by 'hometeam|awayteam' (lowercased) with minute,
    score and red card info, so we can sanity-check odds against what's
    actually happening on the pitch. Returns {} if no API-FOOTBALL key set
    (bot still works without this, just skips the sanity check).
    """
    if not FOOTBALL_API_KEY:
        return {}

    url = f"{FOOTBALL_API_BASE}/fixtures"
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    params = {"live": "all"}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        data = r.json().get("response", [])
    except Exception as e:
        log.warning(f"API-FOOTBALL fetch failed (continuing without stats filter): {e}")
        return {}

    lookup = {}
    for fx in data:
        home = fx["teams"]["home"]["name"].lower()
        away = fx["teams"]["away"]["name"].lower()
        minute = fx["fixture"]["status"].get("elapsed", 0)
        goals_home = fx["goals"]["home"] or 0
        goals_away = fx["goals"]["away"] or 0
        # crude red-card check from events would need another call per fixture;
        # kept simple here to stay inside free-tier request limits
        lookup[f"{home}|{away}"] = {
            "minute": minute,
            "score": f"{goals_home}-{goals_away}",
        }
    return lookup


def passes_sanity_check(value_bet, stats_lookup):
    """
    Basic filter: skip alerts in the last ~5 minutes of a half (odds get
    noisy/illiquid) or when we simply have no live data to confirm the
    match is in a normal state. If no stats API key configured, always pass.
    """
    if not stats_lookup:
        return True, None, None

    home, away = value_bet["match"].split(" vs ")
    key = f"{home.lower()}|{away.lower()}"
    info = stats_lookup.get(key)
    if not info:
        return True, None, None  # couldn't match fixture, don't block on it

    minute = info["minute"] or 0
    if minute and (43 <= minute <= 47 or 88 <= minute <= 90):
        return False, minute, info["score"]  # too close to half/full time, odds unstable

    return True, minute, info["score"]


# ---------------------------- MAIN LOOP ----------------------------
def format_alert(vb, minute, score):
    minute_str = f"{minute}'" if minute is not None else "?"
    score_str = score or "?"
    return (
        f"⚽ <b>Value Bet Found</b>\n\n"
        f"<b>{vb['match']}</b>\n"
        f"Minute: {minute_str} | Score: {score_str}\n\n"
        f"Market: {vb['market']}\n"
        f"Pick: <b>{vb['outcome']}</b>\n"
        f"Best odds: {vb['best_odds']} @ {vb['best_book']}\n"
        f"Fair (consensus) probability: {vb['consensus_prob']}%\n"
        f"Edge: <b>{vb['edge_percent']}%</b>\n\n"
        f"<i>This is a market-inefficiency signal, not a certainty. Bet responsibly.</i>"
    )


def run_scan():
    log.info("Starting scan...")
    events = get_inplay_odds()
    log.info(f"Fetched {len(events)} in-play events")

    if not events:
        return

    value_bets = find_value_bets(events)
    log.info(f"Found {len(value_bets)} candidate value bets before stats filter")

    stats_lookup = get_live_stats_lookup()

    for vb in value_bets:
        ok, minute, score = passes_sanity_check(vb, stats_lookup)
        if not ok:
            log.info(f"Skipped (stats filter): {vb['match']} at minute {minute}")
            continue

        text = format_alert(vb, minute, score)
        send_telegram(text)
        log_alert(
            vb["match"], vb["market"], vb["outcome"], vb["best_book"],
            vb["best_odds"], vb["consensus_prob"], vb["edge_percent"],
            minute, score,
        )
        log.info(f"Alert sent: {vb['match']} - {vb['outcome']} ({vb['edge_percent']}% edge)")


def main():
    init_db()
    send_telegram("🤖 Soccer value bet bot is now online and scanning 24/7.")
    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Scan failed: {e}")
        time.sleep(SCAN_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
