"""
Trend Following Strategy
------------------------
Uses EMA crossover (fast/slow) + MACD confirmation.
Long:  Fast EMA crosses above slow EMA, MACD histogram turning positive
Short: Fast EMA crosses below slow EMA, MACD histogram turning negative
Conviction scales with EMA separation and MACD strength.
"""
import logging
import pandas as pd
import numpy as np

from .base import BaseStrategy, Signal

logger = logging.getLogger(__name__)

FAST_EMA = 9
SLOW_EMA = 21
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9


class TrendFollowingStrategy(BaseStrategy):
    name = "trend_following"

    def generate_signal(self, coin: str, df: pd.DataFrame) -> Signal:
        if len(df) < MACD_SLOW + MACD_SIGNAL + 5:
            return self._no_signal(coin)

        try:
            close = df["close"]

            fast_ema = close.ewm(span=FAST_EMA, adjust=False).mean()
            slow_ema = close.ewm(span=SLOW_EMA, adjust=False).mean()
            macd_line = close.ewm(span=MACD_FAST, adjust=False).mean() - \
                        close.ewm(span=MACD_SLOW, adjust=False).mean()
            signal_line = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
            histogram = macd_line - signal_line

            cur_fast = fast_ema.iloc[-1]
            cur_slow = slow_ema.iloc[-1]
            prev_fast = fast_ema.iloc[-2]
            prev_slow = slow_ema.iloc[-2]
            cur_hist = histogram.iloc[-1]
            prev_hist = histogram.iloc[-2]
            cur_price = close.iloc[-1]

            ema_spread_pct = (cur_fast - cur_slow) / cur_slow * 100

            # Normalize histogram by price
            hist_norm = cur_hist / cur_price * 100

            # Long: fast EMA above slow EMA (or just crossed), MACD histogram positive and growing
            if cur_fast > cur_slow and cur_hist > 0 and cur_hist > prev_hist:
                # Crossed recently if prev was below
                just_crossed = prev_fast <= prev_slow
                spread_score = min(1.0, abs(ema_spread_pct) / 3.0)
                hist_score = min(1.0, abs(hist_norm) * 10)
                score = spread_score * 0.5 + hist_score * 0.5
                if just_crossed:
                    score = min(1.0, score * 1.3)  # boost for fresh cross
                return Signal(
                    coin=coin,
                    direction="long",
                    conviction=self._conviction_from_score(score),
                    strategy=self.name,
                    score=score,
                    metadata={
                        "ema_spread_pct": round(ema_spread_pct, 3),
                        "macd_hist": round(cur_hist, 6),
                        "just_crossed": just_crossed,
                    },
                )

            # Short: fast EMA below slow EMA, MACD histogram negative and falling
            if cur_fast < cur_slow and cur_hist < 0 and cur_hist < prev_hist:
                just_crossed = prev_fast >= prev_slow
                spread_score = min(1.0, abs(ema_spread_pct) / 3.0)
                hist_score = min(1.0, abs(hist_norm) * 10)
                score = spread_score * 0.5 + hist_score * 0.5
                if just_crossed:
                    score = min(1.0, score * 1.3)
                return Signal(
                    coin=coin,
                    direction="short",
                    conviction=self._conviction_from_score(score),
                    strategy=self.name,
                    score=-score,
                    metadata={
                        "ema_spread_pct": round(ema_spread_pct, 3),
                        "macd_hist": round(cur_hist, 6),
                        "just_crossed": just_crossed,
                    },
                )

        except Exception as e:
            logger.warning(f"[trend_following] Error on {coin}: {e}")

        return self._no_signal(coin)
