"""
Daily Performance Reporter
Sends a summary + tweak suggestions for each bot via their own Telegram token.
"""
import sqlite3
import requests
from datetime import datetime, timedelta, timezone

# ── Bot configs ────────────────────────────────────────────────────────────────
BOTS = {
    "domain": {
        "token":   "8692938051:AAFNvwUvjx03wAroQA6uyYWLAE0Ru9fx7KA",
        "chat_id": "315184635",
        "db":      "/home/olie/bots/DomainBot/domainbot.db",
    },
    "solana": {
        "token":   "8711935976:AAFB7jbKXgM-Ub-gUN12KaNTddfTWHDJavs",
        "chat_id": "315184635",
        "db":      "/home/olie/bots/SolanaBot/solana_bot_v1.db",
    },
    "hyperliquid": {
        "token":   "8662430721:AAHjg09KWFYB1gQL9TmGF4CfoOwY95Yfwl0",
        "chat_id": "315184635",
        "db":      "/home/olie/bots/HyperLiquidBot/trades.db",
    },
}

DAY_AGO = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
NOW_LABEL = datetime.now().strftime("%d %b %Y %H:%M")


def send(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    if not r.json().get("ok"):
        print(f"Telegram error: {r.json()}")


def db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ── Domain Bot ─────────────────────────────────────────────────────────────────
def domain_report():
    cfg = BOTS["domain"]
    conn = db(cfg["db"])

    # Alerts today
    today = datetime.now().strftime("%Y-%m-%d")
    alerts_today = conn.execute(
        "SELECT domain, score, tld, reason FROM alert_log WHERE alerted_at >= ? ORDER BY score DESC",
        (today,)
    ).fetchall()

    # All-time stats
    total_alerts = conn.execute("SELECT COUNT(*) as n FROM alert_log").fetchone()["n"]
    top_tlds = conn.execute(
        "SELECT tld, COUNT(*) as n FROM alert_log GROUP BY tld ORDER BY n DESC LIMIT 3"
    ).fetchall()
    avg_score = conn.execute("SELECT AVG(score) as a FROM alert_log").fetchone()["a"] or 0

    # Tune log — last 7 days
    tune_rows = conn.execute(
        "SELECT logged_date, alerts_sent, threshold FROM tune_log ORDER BY logged_date DESC LIMIT 7"
    ).fetchall()
    conn.close()

    alerts_count = len(alerts_today)
    threshold = tune_rows[0]["threshold"] if tune_rows else "N/A"
    avg_daily = sum(r["alerts_sent"] for r in tune_rows) / max(len(tune_rows), 1)

    # Build domain list
    domain_lines = ""
    for a in alerts_today[:5]:
        domain_lines += f"  • <code>{a['domain']}</code> (score {a['score']}, .{a['tld']})\n"
    if not domain_lines:
        domain_lines = "  • None today\n"

    # Tweak suggestions
    tweaks = []
    if avg_daily == 0:
        tweaks.append("🔧 Zero alerts recently — threshold may be too high, consider lowering it in config")
    elif avg_daily > 15:
        tweaks.append("🔧 High alert volume — consider raising threshold to reduce noise")
    if alerts_count == 0:
        tweaks.append("🔧 No domains found today — check sources are still active")
    if avg_score > 0 and avg_score < 70:
        tweaks.append("🔧 Average domain score is low — review scoring weights in scorer.py")

    if not tweaks:
        tweaks.append("✅ Bot looks healthy — no changes needed")

    top_tld_str = ", ".join(f".{r['tld']} ({r['n']})" for r in top_tlds) or "N/A"

    msg = (
        f"🌐 <b>DOMAIN BOT — Daily Report</b>\n"
        f"<i>{NOW_LABEL}</i>\n\n"
        f"<b>Today's Alerts:</b> {alerts_count}\n"
        f"<b>Current Threshold:</b> {threshold}\n"
        f"<b>7-Day Avg Alerts/Day:</b> {avg_daily:.1f}\n"
        f"<b>All-Time Alerts:</b> {total_alerts}\n"
        f"<b>All-Time Avg Score:</b> {avg_score:.0f}\n"
        f"<b>Top TLDs:</b> {top_tld_str}\n\n"
        f"<b>Top Domains Today:</b>\n{domain_lines}\n"
        f"<b>💡 Suggestions:</b>\n" + "\n".join(tweaks)
    )
    send(cfg["token"], cfg["chat_id"], msg)
    print("✓ Domain report sent")


# ── Solana Bot ─────────────────────────────────────────────────────────────────
def solana_report():
    cfg = BOTS["solana"]
    conn = db(cfg["db"])

    day_ago_ms = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)

    # All-time closed
    all_closed = conn.execute(
        "SELECT pnl_usd, pnl_pct, exit_reason, setup_type, confidence_bucket "
        "FROM paper_trades WHERE status='closed'"
    ).fetchall()

    # Last 24h closed
    recent = conn.execute(
        "SELECT pnl_usd, pnl_pct, exit_reason, setup_type, confidence_bucket "
        "FROM paper_trades WHERE status='closed' AND exit_ts >= ?",
        (day_ago_ms,)
    ).fetchall()

    open_count = conn.execute("SELECT COUNT(*) as n FROM paper_trades WHERE status='open'").fetchone()["n"]

    # By setup type
    setup_stats = conn.execute(
        "SELECT setup_type, COUNT(*) as n, SUM(pnl_usd) as total_pnl, "
        "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) as wins "
        "FROM paper_trades WHERE status='closed' GROUP BY setup_type ORDER BY total_pnl DESC"
    ).fetchall()

    # Exit reason breakdown (all-time)
    exit_reasons = conn.execute(
        "SELECT exit_reason, COUNT(*) as n FROM paper_trades WHERE status='closed' "
        "GROUP BY exit_reason ORDER BY n DESC LIMIT 5"
    ).fetchall()

    conn.close()

    def stats(rows):
        if not rows:
            return 0, 0, 0, 0, 0
        wins = sum(1 for r in rows if (r["pnl_usd"] or 0) > 0)
        losses = len(rows) - wins
        total_pnl = sum(r["pnl_usd"] or 0 for r in rows)
        avg_pnl = total_pnl / len(rows)
        win_rate = wins / len(rows) * 100
        return len(rows), wins, losses, total_pnl, win_rate

    total, wins, losses, total_pnl, win_rate = stats(all_closed)
    r_total, r_wins, r_losses, r_pnl, r_wr = stats(recent)

    # Setup breakdown
    setup_lines = ""
    for s in setup_stats:
        s_wr = s["wins"] / s["n"] * 100 if s["n"] else 0
        setup_lines += f"  • <b>{s['setup_type']}</b>: {s['n']} trades, ${s['total_pnl']:.2f} PnL, {s_wr:.0f}% WR\n"
    if not setup_lines:
        setup_lines = "  • No data yet\n"

    # Exit reason lines
    reason_lines = ""
    for r in exit_reasons:
        reason_lines += f"  • {r['exit_reason']}: {r['n']}\n"

    # Tweaks
    tweaks = []
    hard_stops = sum(r["n"] for r in exit_reasons if r["exit_reason"] == "hard_stop")
    time_stops = sum(r["n"] for r in exit_reasons if "time_stop" in (r["exit_reason"] or ""))
    trailing = sum(r["n"] for r in exit_reasons if r["exit_reason"] == "trailing_stop")

    if total > 0:
        if win_rate < 35:
            tweaks.append("🔧 Win rate below 35% — entry threshold may be too low, consider raising entry_threshold in settings.yaml")
        if hard_stops > total * 0.4:
            tweaks.append("🔧 >40% of exits are hard stops — stop_loss_pct may be too tight")
        if time_stops > total * 0.3:
            tweaks.append("🔧 >30% time-stop exits — tokens aren't moving, consider tightening liquidity/volume filters")
        if trailing > 0 and win_rate > 55:
            tweaks.append("✅ Trailing stops firing on winners — take_profit_pct and trailing_distance_pct look well-tuned")
        if r_total == 0:
            tweaks.append("🔧 No trades closed in last 24h — check bot is running")
    if not tweaks:
        tweaks.append("✅ Bot performing well — no changes needed")

    msg = (
        f"🟣 <b>SOLANA BOT — Daily Report</b>\n"
        f"<i>{NOW_LABEL}</i>\n\n"
        f"<b>── Last 24h ──</b>\n"
        f"Closed: {r_total} | Wins: {r_wins} | Losses: {r_losses}\n"
        f"PnL: <b>${r_pnl:.2f}</b> | Win Rate: {r_wr:.1f}%\n\n"
        f"<b>── All-Time ──</b>\n"
        f"Closed: {total} | Wins: {wins} | Losses: {losses}\n"
        f"Total PnL: <b>${total_pnl:.2f}</b> | Win Rate: {win_rate:.1f}%\n"
        f"Open Positions: {open_count}\n\n"
        f"<b>By Setup Type:</b>\n{setup_lines}\n"
        f"<b>Exit Reasons:</b>\n{reason_lines}\n"
        f"<b>💡 Suggestions:</b>\n" + "\n".join(tweaks)
    )
    send(cfg["token"], cfg["chat_id"], msg)
    print("✓ Solana report sent")


# ── HyperLiquid Bot ─────────────────────────────────────────────────────────────
def hyperliquid_report():
    cfg = BOTS["hyperliquid"]
    conn = db(cfg["db"])

    today = datetime.now().strftime("%Y-%m-%d")

    # All-time closed
    all_closed = conn.execute(
        "SELECT pnl_usd, pnl_pct, direction, strategy, conviction, close_reason, "
        "open_time, close_time FROM trades WHERE status='closed'"
    ).fetchall()

    # Last 24h
    recent = conn.execute(
        "SELECT pnl_usd, pnl_pct, direction, strategy, conviction, close_reason "
        "FROM trades WHERE status='closed' AND close_time >= ?",
        (DAY_AGO,)
    ).fetchall()

    open_trades = conn.execute(
        "SELECT coin, direction, conviction, entry_price FROM trades WHERE status='open'"
    ).fetchall()

    # By strategy
    strat_stats = conn.execute(
        "SELECT strategy, COUNT(*) as n, SUM(pnl_usd) as total_pnl, "
        "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) as wins "
        "FROM trades WHERE status='closed' GROUP BY strategy ORDER BY total_pnl DESC"
    ).fetchall()

    # By direction
    dir_stats = conn.execute(
        "SELECT direction, COUNT(*) as n, SUM(pnl_usd) as total_pnl, "
        "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) as wins "
        "FROM trades WHERE status='closed' GROUP BY direction"
    ).fetchall()

    # Exit reasons
    exit_reasons = conn.execute(
        "SELECT close_reason, COUNT(*) as n FROM trades WHERE status='closed' "
        "GROUP BY close_reason ORDER BY n DESC"
    ).fetchall()

    conn.close()

    def stats(rows):
        if not rows:
            return 0, 0, 0, 0, 0
        wins = sum(1 for r in rows if (r["pnl_usd"] or 0) > 0)
        total_pnl = sum(r["pnl_usd"] or 0 for r in rows)
        win_rate = wins / len(rows) * 100
        return len(rows), wins, len(rows) - wins, total_pnl, win_rate

    total, wins, losses, total_pnl, win_rate = stats(all_closed)
    r_total, r_wins, r_losses, r_pnl, r_wr = stats(recent)

    # Strategy lines
    strat_lines = ""
    for s in strat_stats:
        s_wr = s["wins"] / s["n"] * 100 if s["n"] else 0
        strat_lines += f"  • <b>{s['strategy']}</b>: {s['n']} trades, ${s['total_pnl']:.2f}, {s_wr:.0f}% WR\n"
    if not strat_lines:
        strat_lines = "  • No closed trades yet\n"

    # Direction lines
    dir_lines = ""
    for d in dir_stats:
        d_wr = d["wins"] / d["n"] * 100 if d["n"] else 0
        dir_lines += f"  • {d['direction'].upper()}: {d['n']} trades, ${d['total_pnl']:.2f}, {d_wr:.0f}% WR\n"
    if not dir_lines:
        dir_lines = "  • No data yet\n"

    # Open positions
    open_lines = ""
    for t in open_trades[:5]:
        open_lines += f"  • {t['direction'].upper()} <b>{t['coin']}</b> @ ${t['entry_price']:.4f} [{t['conviction']}]\n"
    if not open_lines:
        open_lines = "  • None\n"

    # Tweaks
    tweaks = []
    trailing_hits = sum(r["n"] for r in exit_reasons if r["close_reason"] == "trailing_stop")

    if total == 0:
        tweaks.append("🔧 No closed trades yet — bot may need more time or signal thresholds could be relaxed")
    else:
        if win_rate < 40:
            tweaks.append("🔧 Win rate below 40% — consider raising signal score threshold from 0.3 in risk.py")
        if win_rate > 60:
            tweaks.append("✅ Win rate strong — consider increasing CONVICTION_HIGH_PCT for bigger winners")
        if trailing_hits > total * 0.5:
            tweaks.append("✅ Trailing stops locking in profits well — TRAILING_STOP_PCT is well calibrated")
        elif trailing_hits == 0 and total > 5:
            tweaks.append("🔧 Trailing stops never firing — TRAILING_STOP_PCT may be too tight or TP targets not being reached")

        # Check strategy performance
        worst = min(strat_stats, key=lambda s: s["total_pnl"], default=None)
        best = max(strat_stats, key=lambda s: s["total_pnl"], default=None)
        if worst and best and worst["strategy"] != best["strategy"]:
            tweaks.append(f"🔧 <b>{worst['strategy']}</b> is weakest strategy — consider reducing its weight in .env")
            tweaks.append(f"✅ <b>{best['strategy']}</b> is strongest — consider increasing its weight")

        if r_total == 0 and total > 0:
            tweaks.append("🔧 No trades closed in 24h — check bot is running: <code>systemctl status hyperliquidbot</code>")

    if not tweaks:
        tweaks.append("✅ Bot performing well — no changes needed")

    msg = (
        f"⚡ <b>HYPERLIQUID BOT — Daily Report</b>\n"
        f"<i>{NOW_LABEL}</i>\n\n"
        f"<b>── Last 24h ──</b>\n"
        f"Closed: {r_total} | Wins: {r_wins} | Losses: {r_losses}\n"
        f"PnL: <b>${r_pnl:.2f}</b> | Win Rate: {r_wr:.1f}%\n\n"
        f"<b>── All-Time ──</b>\n"
        f"Closed: {total} | Wins: {wins} | Losses: {losses}\n"
        f"Total PnL: <b>${total_pnl:.2f}</b> | Win Rate: {win_rate:.1f}%\n"
        f"Open Positions: {len(open_trades)}\n\n"
        f"<b>By Strategy:</b>\n{strat_lines}\n"
        f"<b>Long vs Short:</b>\n{dir_lines}\n"
        f"<b>Open Now:</b>\n{open_lines}\n"
        f"<b>💡 Suggestions:</b>\n" + "\n".join(tweaks)
    )
    send(cfg["token"], cfg["chat_id"], msg)
    print("✓ HyperLiquid report sent")


if __name__ == "__main__":
    print(f"Sending daily reports at {NOW_LABEL}...")
    domain_report()
    solana_report()
    hyperliquid_report()
    print("All reports sent.")
