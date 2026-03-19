"""
Risk Manager
------------
- Aggregates signals from multiple strategies (weighted vote)
- Sizes positions by conviction (2% / 5% / 10% of portfolio)
- Enforces max concurrent positions
- Manages trailing stops
"""
import logging
import os
from typing import Optional
from collections import defaultdict

from strategies.base import Signal
import database as db

logger = logging.getLogger(__name__)

CONVICTION_PCT = {
    "low":    float(os.getenv("CONVICTION_LOW_PCT",    "2.0")),
    "medium": float(os.getenv("CONVICTION_MEDIUM_PCT", "5.0")),
    "high":   float(os.getenv("CONVICTION_HIGH_PCT",  "10.0")),
}

STRATEGY_WEIGHTS = {
    "momentum":       float(os.getenv("MOMENTUM_WEIGHT",       "1.0")),
    "mean_reversion": float(os.getenv("MEAN_REVERSION_WEIGHT", "1.0")),
    "trend_following": float(os.getenv("TREND_FOLLOWING_WEIGHT", "1.0")),
}


class RiskManager:
    def __init__(self, max_positions: int, trailing_stop_pct: float):
        self.max_positions = max_positions
        self.trailing_stop_pct = trailing_stop_pct

    # ─── Signal Aggregation ───────────────────────────────────────────────────

    def aggregate_signals(self, signals: list[Signal]) -> Optional[Signal]:
        """
        Combine signals from multiple strategies for the same coin.
        Returns a composite signal or None if no consensus.
        """
        if not signals:
            return None

        coin = signals[0].coin
        long_score = 0.0
        short_score = 0.0
        total_weight = 0.0

        for sig in signals:
            w = STRATEGY_WEIGHTS.get(sig.strategy, 1.0)
            total_weight += w
            if sig.direction == "long":
                long_score += sig.score * w
            elif sig.direction == "short":
                short_score += abs(sig.score) * w

        if total_weight == 0:
            return None

        long_score /= total_weight
        short_score /= total_weight

        # Need at least 0.3 normalized score to act
        if long_score > short_score and long_score >= 0.3:
            conviction = self._score_to_conviction(long_score)
            return Signal(
                coin=coin,
                direction="long",
                conviction=conviction,
                strategy="combined",
                score=long_score,
                metadata={"long_score": long_score, "short_score": short_score},
            )
        elif short_score > long_score and short_score >= 0.3:
            conviction = self._score_to_conviction(short_score)
            return Signal(
                coin=coin,
                direction="short",
                conviction=conviction,
                strategy="combined",
                score=-short_score,
                metadata={"long_score": long_score, "short_score": short_score},
            )

        return None

    def _score_to_conviction(self, score: float) -> str:
        if score >= 0.7:
            return "high"
        elif score >= 0.4:
            return "medium"
        return "low"

    # ─── Position Sizing ──────────────────────────────────────────────────────

    def size_position(self, conviction: str, account_value: float) -> float:
        """Return USD position size for given conviction level."""
        pct = CONVICTION_PCT.get(conviction, CONVICTION_PCT["low"]) / 100
        return account_value * pct

    # ─── Position Checks ──────────────────────────────────────────────────────

    def can_open_position(self, coin: str) -> tuple[bool, str]:
        """Check if we can open a new position."""
        open_trades = db.get_open_trades()

        if len(open_trades) >= self.max_positions:
            return False, f"Max positions reached ({self.max_positions})"

        existing_coins = {t["coin"] for t in open_trades}
        if coin in existing_coins:
            return False, f"Already have open position in {coin}"

        return True, "ok"

    # ─── Trailing Stops ───────────────────────────────────────────────────────

    def init_trailing_stop(self, trade_id: int, coin: str, direction: str, entry_price: float):
        if direction == "long":
            hwm = entry_price
            stop = entry_price * (1 - self.trailing_stop_pct / 100)
        else:
            hwm = entry_price
            stop = entry_price * (1 + self.trailing_stop_pct / 100)

        db.upsert_trailing_stop(trade_id, coin, direction, entry_price, self.trailing_stop_pct, hwm, stop)
        logger.info(f"Trailing stop initialized: {coin} {direction} stop=${stop:.4f}")

    def update_trailing_stops(self, current_prices: dict[str, float]) -> list[dict]:
        """
        Update all trailing stops with current prices.
        Returns list of trades that should be closed (stop hit).
        """
        stops = db.get_trailing_stops()
        to_close = []

        for ts in stops:
            coin = ts["coin"]
            price = current_prices.get(coin)
            if price is None:
                continue

            direction = ts["direction"]
            hwm = ts["high_water_mark"]
            trail_pct = ts["trail_pct"]
            trade_id = ts["trade_id"]

            if direction == "long":
                new_hwm = max(hwm, price)
                stop_price = new_hwm * (1 - trail_pct / 100)
                if price <= stop_price:
                    logger.info(f"Trailing stop hit: {coin} LONG price={price:.4f} stop={stop_price:.4f}")
                    to_close.append({"trade_id": trade_id, "coin": coin, "reason": "trailing_stop"})
                else:
                    db.upsert_trailing_stop(
                        trade_id, coin, direction, ts["entry_price"], trail_pct, new_hwm, stop_price
                    )
            else:  # short
                new_hwm = min(hwm, price)
                stop_price = new_hwm * (1 + trail_pct / 100)
                if price >= stop_price:
                    logger.info(f"Trailing stop hit: {coin} SHORT price={price:.4f} stop={stop_price:.4f}")
                    to_close.append({"trade_id": trade_id, "coin": coin, "reason": "trailing_stop"})
                else:
                    db.upsert_trailing_stop(
                        trade_id, coin, direction, ts["entry_price"], trail_pct, new_hwm, stop_price
                    )

        return to_close
