"""
BB Compression Strategy
-----------------------
Identifies Bollinger Band squeezes followed by breakouts.
When BB width is at a historical low (bands compressing), price often precedes
a sharp directional move. This strategy fires when a compression breaks out.

Long:  Price closes above upper BB while bands are compressed
Short: Price closes below lower BB while bands are compressed

Complementary to MeanReversionStrategy — mean reversion fades extremes, this
strategy *follows* breakouts from compressed ranges.
"""
import logging
import pandas as pd
import numpy as np

from .base import BaseStrategy, Signal

logger = logging.getLogger(__name__)

BB_PERIOD = 20
BB_STD = 2.0
COMPRESSION_LOOKBACK = 50   # bars to compute BB width percentile against
COMPRESSION_THRESHOLD = 0.25  # bands must be in bottom 25th percentile width


class BBCompressionStrategy(BaseStrategy):
    name = "bb_compression"

    def generate_signal(self, coin: str, df: pd.DataFrame) -> Signal:
        if len(df) < BB_PERIOD + COMPRESSION_LOOKBACK + 5:
            return self._no_signal(coin)

        try:
            close = df["close"]
            sma = close.rolling(BB_PERIOD).mean()
            std = close.rolling(BB_PERIOD).std()
            upper_bb = sma + BB_STD * std
            lower_bb = sma - BB_STD * std

            # Normalised BB width (so it's comparable across different price levels)
            bb_width = (upper_bb - lower_bb) / sma

            cur_price = close.iloc[-1]
            cur_upper = upper_bb.iloc[-1]
            cur_lower = lower_bb.iloc[-1]
            cur_width = bb_width.iloc[-1]

            if pd.isna(cur_width) or pd.isna(cur_upper) or pd.isna(cur_lower):
                return self._no_signal(coin)

            # Percentile rank of current width among recent bars (0 = tightest ever)
            recent_widths = bb_width.dropna().iloc[-COMPRESSION_LOOKBACK:]
            if len(recent_widths) < COMPRESSION_LOOKBACK // 2:
                return self._no_signal(coin)

            width_percentile = (recent_widths < cur_width).mean()

            # Only act when bands are genuinely compressed
            if width_percentile > COMPRESSION_THRESHOLD:
                return self._no_signal(coin)

            # Compression tightness score (0 = average, 1 = tightest possible)
            compression_score = 1.0 - (width_percentile / COMPRESSION_THRESHOLD)

            # Long: price breaks above upper band out of compression
            if cur_price > cur_upper:
                breakout_pct = (cur_price - cur_upper) / (cur_upper * 0.005)  # per 0.5%
                score = min(1.0, compression_score * 0.7 + min(1.0, breakout_pct) * 0.3)
                return Signal(
                    coin=coin,
                    direction="long",
                    conviction=self._conviction_from_score(score),
                    strategy=self.name,
                    score=score,
                    metadata={
                        "bb_width_percentile": round(width_percentile, 3),
                        "bb_width": round(float(cur_width), 6),
                    },
                )

            # Short: price breaks below lower band out of compression
            if cur_price < cur_lower:
                breakout_pct = (cur_lower - cur_price) / (cur_lower * 0.005)
                score = min(1.0, compression_score * 0.7 + min(1.0, breakout_pct) * 0.3)
                return Signal(
                    coin=coin,
                    direction="short",
                    conviction=self._conviction_from_score(score),
                    strategy=self.name,
                    score=-score,
                    metadata={
                        "bb_width_percentile": round(width_percentile, 3),
                        "bb_width": round(float(cur_width), 6),
                    },
                )

        except Exception as e:
            logger.warning(f"[bb_compression] Error on {coin}: {e}")

        return self._no_signal(coin)
