"""
Mean Reversion Strategy
-----------------------
Uses Bollinger Bands + RSI extremes.
Long:  Price closes below lower BB and RSI < 31 (oversold)
Short: Price closes above upper BB and RSI > 69 (overbought)
Conviction scales with distance from band and RSI extreme.
Tighter RSI thresholds (69/31) reduce noise by only firing on truly extreme reads.
"""
import logging
import pandas as pd
import numpy as np

from .base import BaseStrategy, Signal

logger = logging.getLogger(__name__)

BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"

    def generate_signal(self, coin: str, df: pd.DataFrame) -> Signal:
        if len(df) < BB_PERIOD + RSI_PERIOD + 5:
            return self._no_signal(coin)

        try:
            close = df["close"]
            sma = close.rolling(BB_PERIOD).mean()
            std = close.rolling(BB_PERIOD).std()
            upper_bb = sma + BB_STD * std
            lower_bb = sma - BB_STD * std
            rsi = _rsi(close, RSI_PERIOD)

            cur_price = close.iloc[-1]
            cur_upper = upper_bb.iloc[-1]
            cur_lower = lower_bb.iloc[-1]
            cur_sma = sma.iloc[-1]
            cur_rsi = rsi.iloc[-1]
            band_width = cur_upper - cur_lower

            if pd.isna(cur_upper) or pd.isna(cur_lower) or band_width == 0:
                return self._no_signal(coin)

            # Long: price below lower band and RSI deeply oversold (31)
            if cur_price < cur_lower and cur_rsi < 31:
                # Score: how far below lower band + how oversold
                band_pct = (cur_lower - cur_price) / band_width  # 0..∞ (>0 means below)
                rsi_pct = (31 - cur_rsi) / 31  # 0..1
                score = min(1.0, band_pct * 0.6 + rsi_pct * 0.4)
                return Signal(
                    coin=coin,
                    direction="long",
                    conviction=self._conviction_from_score(score),
                    strategy=self.name,
                    score=score,
                    metadata={
                        "rsi": round(cur_rsi, 2),
                        "price_vs_lower_bb": round((cur_price - cur_lower) / cur_price * 100, 2),
                    },
                )

            # Short: price above upper band and RSI deeply overbought (69)
            if cur_price > cur_upper and cur_rsi > 69:
                band_pct = (cur_price - cur_upper) / band_width
                rsi_pct = (cur_rsi - 69) / 31
                score = min(1.0, band_pct * 0.6 + rsi_pct * 0.4)
                return Signal(
                    coin=coin,
                    direction="short",
                    conviction=self._conviction_from_score(score),
                    strategy=self.name,
                    score=-score,
                    metadata={
                        "rsi": round(cur_rsi, 2),
                        "price_vs_upper_bb": round((cur_price - cur_upper) / cur_price * 100, 2),
                    },
                )

        except Exception as e:
            logger.warning(f"[mean_reversion] Error on {coin}: {e}")

        return self._no_signal(coin)
