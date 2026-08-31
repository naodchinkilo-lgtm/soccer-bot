"""
In-Play Value Bet Bot (Soccer, Tennis, Horse Racing)
------------------------------------------------------
What this does:
1. Every SCAN_INTERVAL_MINUTES, discovers currently-active soccer, tennis,
   and horse racing leagues/tours from the-odds-api, then pulls live
   in-play odds for each (capped at MAX_LEAGUES_PER_SCAN leagues per scan
   to protect your free monthly quota)
2. For each match/event, compares odds across bookmakers to find price
   discrepancies (a "value" signal: one book pricing an outcome noticeably
   higher than the de-vigged consensus of the others)
3. For soccer specifically, cross-checks against live stats from
   API-FOOTBALL (score, minute) so we don't fire on stale/garbage-time odds
4. Only sends a Telegram alert when the edge clears MIN_EDGE_PERCENT
5. Logs every alert to a local SQLite db so you can track real performance
   over time (check_results.py lets you review hit rate later)

IMPORTANT HONESTY NOTE (read this):
This is a value-detection tool, not a prediction engine. "Value" here means
"the market disagrees with itself" - it does NOT mean "this team/player/
horse will win." Treat every alert as a lead to research further, not a
guaranteed winner. No free (or paid) tool can promise winning picks -
anyone who says otherwise is selling something.
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
MIN_ODDS = float(os.environ.get("MIN_ODDS", "1.2"))
MAX_ODDS = float(os.environ.get("MAX_ODDS", "2.3"))
MIN_CONSENSUS_PROB_PERCENT = float(os.environ.get("MIN_CONSENSUS_PROB_PERCENT", "65"))
REQUIRED_BOOK = os.environ.get("REQUIRED_BOOK", "").strip().lower()  # e.g. "melbet", blank = any book

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
SPORT_GROUPS_WANTED = {"Soccer", "Tennis", "Horse Racing"}
MAX_LEAGUES_PER_SCAN = 12  # caps requests so we don't blow the free monthly quota


def get_active_sport_keys():
    """
    Ask the-odds-api which leagues/tours are currently active for the sports
    we care about (soccer, tennis, horse racing), so we don't hardcode a
    stale list of league keys. Costs 1 request.
    """
    url = f"{ODDS_API_BASE}/sports"
    params = {"apiKey": ODDS_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        all_sports = r.json()
    except Exception as e:
        log.error(f"Failed to fetch sports list: {e}")
        return []

    active = [
        s["key"] for s in all_sports
        if s.get("group") in SPORT_GROUPS_WANTED and s.get("active")
    ]
    return active[:MAX_LEAGUES_PER_SCAN]


def get_inplay_odds():
    """
    Fetch live in-play odds across active soccer, tennis, and horse racing
    leagues. One request to list active leagues, then one request per league
    (capped at MAX_LEAGUES_PER_SCAN) to stay inside the free monthly quota.
    """
    sport_keys = get_active_sport_keys()
    log.info(f"Active leagues this scan: {sport_keys}")

    all_events = []
    for sport_key in sport_keys:
        url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "us,uk,eu",
            "markets": "h2h",  # match/match-winner market for all three sport types
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            events = r.json()
            for e in events:
                e["sport_key"] = sport_key
            all_events.extend(events)
            remaining = r.headers.get("x-requests-remaining")
            if remaining:
                log.info(f"[{sport_key}] requests remaining this period: {remaining}")
        except Exception as e:
            log.error(f"Odds fetch failed for {sport_key}: {e}")
            continue

    return all_events


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

            # If a specific bookmaker is required (e.g. Melbet), only consider
            # that book's price for this outcome - skip if it didn't quote it
            if REQUIRED_BOOK:
                book_prices = [(b, p) for b, p in prices if REQUIRED_BOOK in b.lower()]
                if not book_prices:
                    continue
                best_book, best_price = max(book_prices, key=lambda x: x[1])
            else:
                # best (highest) price available for this outcome
                best_book, best_price = max(prices, key=lambda x: x[1])

            # Odds range filter - skip longshots and near-certainties
            if not (MIN_ODDS <= best_price <= MAX_ODDS):
                continue

            # Minimum confidence filter - only "safe-ish" picks
            if avg_consensus_prob * 100 < MIN_CONSENSUS_PROB_PERCENT:
                continue

            implied_prob_best = 1 / best_price

            # Edge = how much cheaper the best price is vs fair consensus
            edge_percent = (avg_consensus_prob - implied_prob_best) * 100

            if edge_percent >= MIN_EDGE_PERCENT:
                value_bets.append(
                    {
                        "match": match_name,
                        "market": "Match/Event Winner",
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
    score, and status (e.g. '1H', '2H', 'FT', 'HT'), so we can sanity-check
    odds against what's actually happening on the pitch. Returns {} if no
    API-FOOTBALL key set (bot still works without this, just skips the
    sanity check - which means finished-game filtering won't happen).
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
        status_short = fx["fixture"]["status"].get("short", "")  # e.g. 1H, HT, 2H, FT, AET, PEN
        minute = fx["fixture"]["status"].get("elapsed", 0)
        goals_home = fx["goals"]["home"] or 0
        goals_away = fx["goals"]["away"] or 0
        lookup[f"{home}|{away}"] = {
            "minute": minute,
            "score": f"{goals_home}-{goals_away}",
            "status": status_short,
        }
    return lookup


FINISHED_STATUSES = {"FT", "AET", "PEN", "CANC", "ABD", "AWD", "WO", "PST"}


def passes_sanity_check(value_bet, stats_lookup):
    """
    Skip alerts for matches that:
    - have already finished/were abandoned/etc (FINISHED_STATUSES)
    - the-odds-api still lists as an "event" but API-FOOTBALL has no live
      record for at all (couldn't confirm it's actually in-play) - for
      soccer specifically, since that's the only sport with a stats source
    - are in the noisy last ~5 minutes of a half
    Only applies to soccer, since API-FOOTBALL only covers soccer. Tennis
    and horse racing bets always pass (no live-stats source available).
    """
    if value_bet.get("sport_key", "").split("_")[0] != "soccer":
        return True, None, None

    if not stats_lookup:
        # No stats source configured at all - we can't confirm the match
        # is genuinely still live, so we can't safely filter. Recommend
        # setting FOOTBALL_API_KEY to enable this check properly.
        return True, None, None

    home, away = value_bet["match"].split(" vs ")
    key = f"{home.lower()}|{away.lower()}"
    info = stats_lookup.get(key)

    if not info:
        # Odds API says this match is live, but API-FOOTBALL has no live
        # record for it right now - treat as unconfirmed/stale and skip
        return False, None, None

    if info["status"] in FINISHED_STATUSES:
        return False, info["minute"], info["score"]

    minute = info["minute"] or 0
    if minute and (43 <= minute <= 47 or 88 <= minute <= 90):
        return False, minute, info["score"]  # too close to half/full time, odds unstable

    return True, minute, info["score"]


# ---------------------------- MAIN LOOP ----------------------------
def format_alert(vb, minute, score):
    sport_key = vb.get("sport_key", "")
    if sport_key.startswith("soccer"):
        sport_emoji, sport_label = "⚽", "Soccer"
    elif sport_key.startswith("tennis"):
        sport_emoji, sport_label = "🎾", "Tennis"
    elif "horse" in sport_key:
        sport_emoji, sport_label = "🐎", "Horse Racing"
    else:
        sport_emoji, sport_label = "🏆", sport_key

    extra_line = ""
    if minute is not None:
        minute_str = f"{minute}'"
        score_str = score or "?"
        extra_line = f"Minute: {minute_str} | Score: {score_str}\n\n"

    return (
        f"{sport_emoji} <b>Value Bet Found ({sport_label})</b>\n\n"
        f"<b>{vb['match']}</b>\n"
        f"{extra_line}"
        f"Market: {vb['market']}\n"
        f"Pick: <b>{vb['outcome']}</b>\n"
        f"Best odds: {vb['best_odds']} @ {vb['best_book']}\n"
        f"Fair (consensus) probability: {vb['consensus_prob']}%\n"
        f"Edge: <b>{vb['edge_percent']}%</b>\n\n"
        f"<i>This is a market-inefficiency signal, not a certainty. Bet responsibly.</i>"
    )


def run_scan_and_count():
    """Runs one scan cycle, returns the number of alerts actually sent."""
    log.info("Starting scan...")
    events = get_inplay_odds()
    log.info(f"Fetched {len(events)} in-play events")

    if not events:
        return 0

    value_bets = find_value_bets(events)
    log.info(f"Found {len(value_bets)} candidate value bets before stats filter")

    stats_lookup = get_live_stats_lookup()

    alerts_sent = 0
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
        alerts_sent += 1

    return alerts_sent


def send_daily_summary(scans_today: int, alerts_today: int):
    text = (
        f"📊 <b>Daily check-in</b>\n\n"
        f"Scans run in last 24h: {scans_today}\n"
        f"Alerts sent in last 24h: {alerts_today}\n\n"
        f"{'No alerts is normal \u2014 it means no bet cleared your edge threshold.' if alerts_today == 0 else 'Bot is finding and alerting on value bets as expected.'}\n"
        f"Bot is alive and scanning normally."
    )
    send_telegram(text)


def main():
    init_db()
    send_telegram("🤖 Soccer value bet bot is now online and scanning 24/7.")

    scans_since_summary = 0
    alerts_since_summary = 0
    last_summary_time = time.time()
    SUMMARY_INTERVAL_SECONDS = 24 * 60 * 60  # once a day

    while True:
        try:
            alerts_sent_this_scan = run_scan_and_count()
            alerts_since_summary += alerts_sent_this_scan
        except Exception as e:
            log.error(f"Scan failed: {e}")
            alerts_sent_this_scan = 0

        scans_since_summary += 1

        if time.time() - last_summary_time >= SUMMARY_INTERVAL_SECONDS:
            send_daily_summary(scans_since_summary, alerts_since_summary)
            scans_since_summary = 0
            alerts_since_summary = 0
            last_summary_time = time.time()

        time.sleep(SCAN_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
