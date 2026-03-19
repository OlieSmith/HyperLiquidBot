from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class Signal:
    coin: str
    direction: str       # 'long', 'short', 'none'
    conviction: str      # 'low', 'medium', 'high'
    strategy: str
    score: float = 0.0   # raw signal strength -1.0 to 1.0
    metadata: dict = field(default_factory=dict)


class BaseStrategy(ABC):
    name: str = "base"

    @abstractmethod
    def generate_signal(self, coin: str, df: pd.DataFrame) -> Signal:
        """Generate a trading signal from OHLCV data."""
        ...

    def _no_signal(self, coin: str) -> Signal:
        return Signal(coin=coin, direction="none", conviction="low", strategy=self.name, score=0.0)

    def _conviction_from_score(self, score: float) -> str:
        abs_score = abs(score)
        if abs_score >= 0.7:
            return "high"
        elif abs_score >= 0.4:
            return "medium"
        return "low"
