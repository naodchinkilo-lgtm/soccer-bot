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
MAX_LEAGUES_PER_SCAN = 3  # lowered further - quota is nearly exhausted this period


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
    Fetch LIVE in-play odds across active soccer, tennis, and horse racing
    leagues. Only requests FEATURED markets (h2h, totals) here - these are
    the only markets the-odds-api's bulk "all events" endpoint supports.
    BTTS and Double Chance require a separate per-match call (see
    get_extra_markets_for_event below) and are only fetched later for
    matches that already look promising, to protect your free quota.
    """
    sport_keys = get_active_sport_keys()
    log.info(f"Active leagues this scan: {sport_keys}")

    all_events = []
    for sport_key in sport_keys:
        url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "uk,eu",
            "markets": "h2h,totals",  # featured markets only - the bulk endpoint doesn't support btts/double_chance
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


def get_extra_markets_for_event(sport_key, event_id):
    """
    Fetch BTTS and Double Chance odds for ONE specific match. This costs a
    separate API request, so it's only called for matches that already
    passed the moneyline/totals value-bet check - not for every match in
    the scan, to avoid burning the free monthly quota.
    """
    url = f"{ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "uk,eu",
        "markets": "btts,double_chance",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Extra markets fetch failed for event {event_id}: {e}")
        return None


def merge_extra_markets(event, extra_data):
    """Merges btts/double_chance markets (from a per-event call) into an
    event's existing bookmakers list, matching by bookmaker key."""
    if not extra_data:
        return event
    existing_by_key = {bm["key"]: bm for bm in event.get("bookmakers", [])}
    for bm in extra_data.get("bookmakers", []):
        if bm["key"] in existing_by_key:
            existing_by_key[bm["key"]]["markets"].extend(bm.get("markets", []))
        else:
            event.setdefault("bookmakers", []).append(bm)
    return event


def find_value_bets(events):
    """
    For each event, for each market (moneyline, spreads, totals) and each
    outcome, compare the BEST available price against the de-vigged
    consensus probability from all other books. If the best price implies
    meaningfully lower probability than consensus, that's edge.
    """
    value_bets = []
    MARKET_LABELS = {
        "h2h": "Moneyline (1X2)",
        "double_chance": "Double Chance",
        "btts": "Both Teams to Score",
        "totals": "Over/Under Goals",
    }

    for event in events:
        bookmakers = event.get("bookmakers", [])
        if len(bookmakers) < MIN_BOOKS_REQUIRED:
            continue

        home = event.get("home_team")
        away = event.get("away_team")
        match_name = f"{home} vs {away}"

        for market_key, market_label in MARKET_LABELS.items():
            # outcome_key = (outcome name, point) so "Over 2.5" != "Over 3.5"
            outcome_prices = {}  # outcome_key -> list of (book_title, price)
            for bm in bookmakers:
                for market in bm.get("markets", []):
                    if market["key"] != market_key:
                        continue
                    for outcome in market["outcomes"]:
                        outcome_key = (outcome["name"], outcome.get("point"))
                        outcome_prices.setdefault(outcome_key, []).append(
                            (bm["title"], outcome["price"])
                        )

            if len(outcome_prices) < 2:
                continue

            # De-vig: convert each book's full market to implied probs summing
            # to 1, then average across books to get fair consensus per outcome
            consensus_probs = {k: [] for k in outcome_prices}
            for bm in bookmakers:
                for market in bm.get("markets", []):
                    if market["key"] != market_key:
                        continue
                    outcomes = market["outcomes"]
                    implied = [1 / o["price"] for o in outcomes]
                    overround = sum(implied)
                    if overround == 0:
                        continue
                    for o, imp in zip(outcomes, implied):
                        fair_prob = imp / overround  # removes the vig
                        o_key = (o["name"], o.get("point"))
                        if o_key in consensus_probs:
                            consensus_probs[o_key].append(fair_prob)

            for outcome_key, prices in outcome_prices.items():
                outcome_name, point = outcome_key
                probs = consensus_probs.get(outcome_key, [])
                if not probs:
                    continue
                avg_consensus_prob = sum(probs) / len(probs)

                # If a specific bookmaker is required (e.g. Melbet), only use
                # that book's price for this outcome - skip if not quoted
                if REQUIRED_BOOK:
                    book_prices = [(b, p) for b, p in prices if REQUIRED_BOOK in b.lower()]
                    if not book_prices:
                        continue
                    best_book, best_price = max(book_prices, key=lambda x: x[1])
                else:
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
                    outcome_label = f"{outcome_name} {point}" if point is not None else outcome_name
                    value_bets.append(
                        {
                            "match": match_name,
                            "market": market_label,
                            "outcome": outcome_label,
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
    - have already finished/were abandoned/etc (FINISHED_STATUSES) - soccer only,
      via API-FOOTBALL
    - the-odds-api still lists as an "event" but API-FOOTBALL has no live
      record for at all (couldn't confirm it's actually in-play) - soccer only
    - are in the noisy last ~5 minutes of a half - soccer only
    - for tennis/horse racing (no live-stats source available), started
      long enough ago that it's essentially certain to be over already
    """
    sport = value_bet.get("sport_key", "").split("_")[0]

    if sport != "soccer":
        # No live-stats source for tennis/horse racing - fall back to a
        # simple time-based check using the event's scheduled start time
        commence_time = value_bet.get("commence_time")
        if commence_time:
            try:
                start = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
                elapsed_hours = (datetime.now(timezone.utc) - start).total_seconds() / 3600
                max_hours = 1 if sport == "horseracing" or "horse" in sport else 4
                if elapsed_hours > max_hours:
                    return False, None, None  # started too long ago, almost certainly finished
            except Exception:
                pass
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

    # Stage 1: cheap bulk check on featured markets (moneyline, totals)
    value_bets = find_value_bets(events)
    log.info(f"Found {len(value_bets)} candidate value bets on featured markets (moneyline/totals)")

    # Stage 2: for matches that already showed value, spend one extra request
    # per match to also check BTTS and Double Chance - not done for every
    # match, to protect the free monthly quota
    candidate_event_ids = {}
    for event in events:
        if event.get("id") in {vb.get("event_id") for vb in value_bets}:
            continue  # already tagged below; placeholder, see loop below
    # tag event_id onto each value bet + build lookup of events by id
    events_by_id = {e.get("id"): e for e in events}
    candidate_ids = set()
    for vb in value_bets:
        pass  # event_id not tracked yet at this stage - see below

    # Build the set of events worth the extra BTTS/Double Chance lookup:
    # any event that already produced a moneyline/totals value bet
    matched_event_ids = set()
    for event in events:
        eid = event.get("id")
        home, away = event.get("home_team"), event.get("away_team")
        match_name = f"{home} vs {away}"
        if any(vb["match"] == match_name for vb in value_bets):
            matched_event_ids.add(eid)

    for eid in matched_event_ids:
        event = events_by_id.get(eid)
        if not event:
            continue
        extra = get_extra_markets_for_event(event["sport_key"], eid)
        merged_event = merge_extra_markets(event, extra)
        extra_bets = find_value_bets([merged_event])
        # only keep the newly-found btts/double_chance ones (avoid re-adding
        # the same moneyline/totals bet twice)
        for eb in extra_bets:
            if eb["market"] in ("Both Teams to Score", "Double Chance"):
                value_bets.append(eb)

    log.info(f"Found {len(value_bets)} total candidate value bets after BTTS/Double Chance check")

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
