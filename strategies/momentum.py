"""
Momentum Strategy
-----------------
Long:  RSI turning up from oversold (40-55 zone) + positive Rate-of-Change
Short: RSI turning down from overbought (45-60 zone) + negative Rate-of-Change
Conviction scales with RSI position and ROC magnitude.
"""
import logging
import pandas as pd
import numpy as np

from .base import BaseStrategy, Signal

logger = logging.getLogger(__name__)

RSI_PERIOD = 14
ROC_PERIOD = 10


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _roc(series: pd.Series, period: int) -> pd.Series:
    return (series - series.shift(period)) / series.shift(period) * 100


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def generate_signal(self, coin: str, df: pd.DataFrame) -> Signal:
        if len(df) < max(RSI_PERIOD, ROC_PERIOD) + 5:
            return self._no_signal(coin)

        try:
            close = df["close"]
            rsi = _rsi(close, RSI_PERIOD)
            roc = _roc(close, ROC_PERIOD)

            cur_rsi = rsi.iloc[-1]
            cur_roc = roc.iloc[-1]
            prev_rsi = rsi.iloc[-2]

            if pd.isna(cur_rsi) or pd.isna(cur_roc):
                return self._no_signal(coin)

            rsi_rising = cur_rsi > prev_rsi
            rsi_falling = cur_rsi < prev_rsi

            # Normalize ROC to -1..1 (cap at ±5%)
            roc_norm = max(-1.0, min(1.0, cur_roc / 5.0))

            # Long: RSI in 40-65 and rising, positive ROC
            if 40 <= cur_rsi <= 65 and rsi_rising and cur_roc > 0.5:
                score = ((cur_rsi - 40) / 25) * 0.5 + roc_norm * 0.5
                score = min(score, 1.0)
                return Signal(
                    coin=coin,
                    direction="long",
                    conviction=self._conviction_from_score(score),
                    strategy=self.name,
                    score=score,
                    metadata={"rsi": round(cur_rsi, 2), "roc": round(cur_roc, 2)},
                )

            # Short: RSI in 35-60 and falling, negative ROC
            if 35 <= cur_rsi <= 60 and rsi_falling and cur_roc < -0.5:
                score = ((60 - cur_rsi) / 25) * 0.5 + abs(roc_norm) * 0.5
                score = min(score, 1.0)
                return Signal(
                    coin=coin,
                    direction="short",
                    conviction=self._conviction_from_score(score),
                    strategy=self.name,
                    score=-score,
                    metadata={"rsi": round(cur_rsi, 2), "roc": round(cur_roc, 2)},
                )

        except Exception as e:
            logger.warning(f"[momentum] Error on {coin}: {e}")

        return self._no_signal(coin)
