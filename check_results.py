"""
Run this anytime to see all alerts the bot has sent, so you can manually
track whether the picks are actually performing over time. This is the
single most important habit for not fooling yourself about whether the
bot works: log everything, review honestly, adjust MIN_EDGE_PERCENT
based on real results, not vibes.

Usage: python check_results.py
"""
import sqlite3

conn = sqlite3.connect("bets.db")
cur = conn.execute(
    "SELECT sent_at, match, outcome, best_odds, edge_percent, minute, score FROM alerts ORDER BY sent_at DESC"
)
rows = cur.fetchall()
conn.close()

if not rows:
    print("No alerts logged yet.")
else:
    print(f"{'Sent At':<20} {'Match':<35} {'Pick':<20} {'Odds':<6} {'Edge%':<6} {'Min':<4} {'Score'}")
    print("-" * 110)
    for r in rows:
        sent_at, match, outcome, odds, edge, minute, score = r
        print(f"{sent_at[:19]:<20} {match[:34]:<35} {outcome[:19]:<20} {odds:<6} {edge:<6} {minute or '-':<4} {score or '-'}")
    print(f"\nTotal alerts logged: {len(rows)}")
