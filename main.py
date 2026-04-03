"""
HyperLiquid Perpetuals Trading Bot
====================================
Strategies: Momentum | Mean Reversion | Trend Following | BB Compression
"""
import html
import logging
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
    ],
)
logger = logging.getLogger("main")

import numpy as np
import pandas as pd

import database as db
from exchange import HyperLiquidClient
from risk import RiskManager
from notifier import TelegramNotifier, build_notifier_or_none
from strategies import MomentumStrategy, MeanReversionStrategy, TrendFollowingStrategy, BBCompressionStrategy

INTRADAY_UPDATE_SECONDS = int(float(os.getenv("INTRADAY_UPDATE_HOURS", "4")) * 3600)

ATR_PERIOD = 14
ATR_MULTIPLIER = 3.0   # reduced from 5.5 — tighter stops to cut losers faster
ATR_MIN_TRAIL_PCT = 1.0
ATR_MAX_TRAIL_PCT = 8.0  # capped at 8% — prevents blow-ups on volatile coins like TURBO


def _calc_atr_pct(df: pd.DataFrame, price: float, period: int = ATR_PERIOD) -> float:
    """Return ATR as a % of price. Used to set dynamic trailing stop distance."""
    if df is None or len(df) < period + 1:
        return 0.0
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    if pd.isna(atr) or price <= 0:
        return 0.0
    return (atr / price) * 100


def main():
    db.init_db()

    client = HyperLiquidClient()
    notifier: TelegramNotifier | None = build_notifier_or_none()

    max_positions = int(os.getenv("MAX_POSITIONS", "10"))
    trailing_stop_pct = float(os.getenv("TRAILING_STOP_PCT", "2.0"))
    scan_interval = int(os.getenv("SCAN_INTERVAL", "60"))
    min_volume = float(os.getenv("MIN_VOLUME_24H", "1000000"))
    paper_trading = os.getenv("PAPER_TRADING", "true").lower() == "true"

    risk = RiskManager(max_positions=max_positions, trailing_stop_pct=trailing_stop_pct)

    strategies = [
        MomentumStrategy(),
        MeanReversionStrategy(),
        TrendFollowingStrategy(),
        BBCompressionStrategy(),
    ]

    mode_str = "PAPER TRADING" if paper_trading else "LIVE TRADING"
    net_str = "TESTNET" if os.getenv("TESTNET", "false").lower() == "true" else "MAINNET"
    startup_msg = f"HyperLiquid Bot started — {mode_str} / {net_str}"
    logger.info(startup_msg)
    if notifier:
        notifier.send_message(f"ℹ️ {startup_msg}")

    scan_count = 0
    next_intraday_update = time.time() + INTRADAY_UPDATE_SECONDS

    while True:
        try:
            scan_count += 1
            logger.info(f"=== Scan #{scan_count} ===")

            # ── 1. Fetch liquid perps and current prices ───────────────────────
            coins = client.get_liquid_perps(min_volume_24h=min_volume)
            if not coins:
                logger.warning("No liquid perps found, retrying next cycle")
                time.sleep(scan_interval)
                continue
            logger.info(f"Scanning {len(coins)} liquid perps")

            current_prices = client.get_all_mids()

            # ── 2. Check and close trailing stops ─────────────────────────────
            stops_to_close = risk.update_trailing_stops(current_prices)
            for stop in stops_to_close:
                _close_trade(stop["trade_id"], stop["coin"], stop["reason"], client, notifier, current_prices, paper_trading, risk)

            # ── 3. Generate signals for each coin ─────────────────────────────
            composite_signals = []   # list of (signal, df) tuples
            for coin in coins:
                if coin not in current_prices:
                    continue
                df = client.get_candles(coin, interval="15m", lookback_hours=72)
                time.sleep(0.15)  # avoid 429 rate limiting from HyperLiquid API
                if df.empty or len(df) < 40:
                    continue

                coin_signals = []
                for strategy in strategies:
                    sig = strategy.generate_signal(coin, df)
                    if sig.direction != "none":
                        coin_signals.append(sig)

                if coin_signals:
                    composite = risk.aggregate_signals(coin_signals)
                    if composite:
                        # Trend filter: block trades that fight the 200-bar SMA direction.
                        # 200 bars of 15m data ≈ 50 hours of trend context.
                        if len(df) >= 200:
                            sma200 = df["close"].rolling(200).mean().iloc[-1]
                            cur_price = df["close"].iloc[-1]
                            if not pd.isna(sma200):
                                if composite.direction == "short" and cur_price > sma200:
                                    logger.debug(f"Trend filter: blocked short {coin} (price above 200-SMA)")
                                    composite = None
                                elif composite.direction == "long" and cur_price < sma200:
                                    logger.debug(f"Trend filter: blocked long {coin} (price below 200-SMA)")
                                    composite = None
                    if composite:
                        composite_signals.append((composite, df))

            logger.info(f"Active signals: {len(composite_signals)}")

            # ── 4. Execute trades ──────────────────────────────────────────────
            account_value = client.get_account_value()

            for signal, signal_df in composite_signals:
                coin = signal.coin
                can_open, reason = risk.can_open_position(coin)
                if not can_open:
                    logger.debug(f"Skip {coin}: {reason}")
                    continue

                price = current_prices.get(coin)
                if not price:
                    continue

                size_usd = risk.size_position(signal.conviction, account_value)
                if size_usd < 10:
                    logger.warning(f"Position size too small for {coin}: ${size_usd:.2f}")
                    continue

                result = client.open_position(
                    coin=coin,
                    direction=signal.direction,
                    size_usd=size_usd,
                    current_price=price,
                )
                if not result:
                    continue

                fill_price = result.get("fill_price", price)
                size_coin = result.get("size_coin", size_usd / fill_price)

                trade_id = db.open_trade(
                    coin=coin,
                    direction=signal.direction,
                    strategy=signal.strategy,
                    conviction=signal.conviction,
                    entry_price=fill_price,
                    size_usd=size_usd,
                    size_coin=size_coin,
                    leverage=client.leverage,
                    paper_trade=paper_trading,
                    order_id=result.get("order_id"),
                )

                # ATR-based trailing stop: 5.5x ATR as % of price
                atr_pct = _calc_atr_pct(signal_df, fill_price)
                atr_trail_pct = None
                if atr_pct > 0:
                    atr_trail_pct = max(ATR_MIN_TRAIL_PCT, min(ATR_MAX_TRAIL_PCT, atr_pct * ATR_MULTIPLIER))
                risk.init_trailing_stop(trade_id, coin, signal.direction, fill_price, atr_trail_pct)

                if notifier:
                    notifier.send_trade_opened(
                        symbol=coin,
                        direction=signal.direction,
                        entry_price=fill_price,
                        size_usd=size_usd,
                        score=signal.score,
                        strategy=signal.strategy,
                        conviction=signal.conviction,
                        leverage=client.leverage,
                        paper=paper_trading,
                    )

                logger.info(
                    f"Opened {signal.direction.upper()} {coin} | "
                    f"conviction={signal.conviction} | size=${size_usd:.2f} | "
                    f"score={signal.score:.3f} | "
                    f"trail={atr_trail_pct:.2f}% (ATR)" if atr_trail_pct else
                    f"Opened {signal.direction.upper()} {coin} | "
                    f"conviction={signal.conviction} | size=${size_usd:.2f} | "
                    f"score={signal.score:.3f} | trail={trailing_stop_pct:.2f}% (fixed)"
                )

            # ── 5. Intraday update (every N hours, same as SolanaBot) ──────────
            now = time.time()
            if now >= next_intraday_update:
                _send_intraday_update(notifier)
                next_intraday_update = now + INTRADAY_UPDATE_SECONDS

            # ── 6. Log periodic stats ──────────────────────────────────────────
            if scan_count % 10 == 0:
                stats = db.get_stats()
                logger.info(
                    f"Stats: open={stats.get('open_count', 0)} | "
                    f"wins={stats.get('wins', 0)} | losses={stats.get('losses', 0)} | "
                    f"PnL=${stats.get('total_pnl', 0):+.2f}"
                )

        except KeyboardInterrupt:
            logger.info("Shutting down...")
            if notifier:
                notifier.send_message("ℹ️ HyperLiquid Bot stopped by user")
            break
        except Exception as e:
            logger.exception(f"Unexpected error in main loop: {e}")
            if notifier:
                notifier.send_message(f"⚠️ <b>BOT ERROR</b>\n{html.escape(str(e))[:500]}")

        time.sleep(scan_interval)


def _close_trade(
    trade_id: int,
    coin: str,
    reason: str,
    client: HyperLiquidClient,
    notifier: TelegramNotifier | None,
    current_prices: dict,
    paper_trading: bool,
    risk: RiskManager | None = None,
):
    trade = db.get_trade(trade_id)
    if not trade or trade["status"] != "open":
        return

    price = current_prices.get(coin)
    if not price:
        price = client.get_mid_price(coin)
    if not price:
        logger.error(f"Cannot close {coin}: no price available")
        return

    result = client.close_position(
        coin=coin,
        direction=trade["direction"],
        size_coin=trade["size_coin"],
        current_price=price,
    )
    if not result:
        return

    fill_price = result.get("fill_price", price)
    closed = db.close_trade(trade_id, fill_price, reason)

    # Start cooldown to prevent immediate re-entry on this coin
    if risk is not None:
        risk.set_cooldown(coin)

    hold_minutes = None
    if closed.get("open_time"):
        try:
            open_dt = datetime.fromisoformat(closed["open_time"])
            hold_minutes = (datetime.now(timezone.utc) - open_dt.replace(tzinfo=timezone.utc)).total_seconds() / 60
        except Exception:
            pass

    if notifier:
        notifier.send_trade_closed(
            symbol=coin,
            direction=closed.get("direction", ""),
            exit_price=fill_price,
            pnl_usd=closed.get("pnl_usd", 0),
            pnl_pct=closed.get("pnl_pct", 0),
            close_reason=reason,
            hold_minutes=hold_minutes,
            paper=paper_trading,
        )

    logger.info(
        f"Closed {coin} | reason={reason} | "
        f"PnL=${closed.get('pnl_usd', 0):+.2f} ({closed.get('pnl_pct', 0):+.2f}%)"
    )


def _send_intraday_update(notifier: TelegramNotifier | None):
    if not notifier:
        return
    try:
        stats = db.get_daily_stats()
        notifier.send_intraday_update(
            realized_pnl_today=stats.get("pnl_today", 0) or 0,
            open_count=stats.get("open_count", 0) or 0,
            closed_today=(stats.get("wins", 0) or 0) + (stats.get("losses", 0) or 0),
            wins_today=stats.get("wins", 0) or 0,
            losses_today=stats.get("losses", 0) or 0,
        )
    except Exception as exc:
        logger.warning("Failed to send intraday update: %s", exc)


def _acquire_pid_lock(pid_file: str = "/tmp/hyperliquidbot.pid") -> None:
    if os.path.exists(pid_file):
        try:
            existing_pid = int(open(pid_file).read().strip())
            os.kill(existing_pid, 0)
            print(f"Bot already running (PID {existing_pid}). Exiting.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))
    import atexit
    atexit.register(os.remove, pid_file)


if __name__ == "__main__":
    _acquire_pid_lock()
    main()
