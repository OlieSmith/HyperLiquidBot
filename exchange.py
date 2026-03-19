import logging
import os
import time
from typing import Optional
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

MAINNET_URL = "https://api.hyperliquid.xyz"
TESTNET_URL = "https://api.hyperliquid-testnet.xyz"


class HyperLiquidClient:
    """Wraps the HyperLiquid SDK for market data and order execution."""

    def __init__(self):
        self.testnet = os.getenv("TESTNET", "false").lower() == "true"
        self.paper_trading = os.getenv("PAPER_TRADING", "true").lower() == "true"
        self.base_url = TESTNET_URL if self.testnet else MAINNET_URL
        self.wallet_address = os.getenv("HYPERLIQUID_WALLET_ADDRESS", "")
        self.private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY", "")
        self.leverage = int(os.getenv("LEVERAGE", "2"))

        from hyperliquid.info import Info
        self.info = Info(self.base_url, skip_ws=True)

        self.exchange = None
        self.account = None
        if not self.paper_trading:
            self._init_exchange()

        # Cache meta (asset decimals etc.)
        self._meta = None
        self._meta_ts = 0

        mode = "PAPER" if self.paper_trading else "LIVE"
        net = "TESTNET" if self.testnet else "MAINNET"
        logger.info(f"HyperLiquid client ready: {mode} / {net}")

    def _init_exchange(self):
        if not self.private_key or not self.wallet_address:
            raise ValueError("HYPERLIQUID_PRIVATE_KEY and HYPERLIQUID_WALLET_ADDRESS must be set for live trading")
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        self.account = Account.from_key(self.private_key)
        self.exchange = Exchange(self.account, self.base_url)

    # ─── Market Data ──────────────────────────────────────────────────────────

    def get_meta(self) -> dict:
        now = time.time()
        if self._meta is None or now - self._meta_ts > 300:
            self._meta = self.info.meta()
            self._meta_ts = now
        return self._meta

    def get_all_mids(self) -> dict[str, float]:
        raw = self.info.all_mids()
        return {k: float(v) for k, v in raw.items()}

    def get_liquid_perps(self, min_volume_24h: float = 1_000_000) -> list[str]:
        """Return coins with sufficient 24h volume."""
        meta = self.get_meta()
        universe = meta.get("universe", [])
        mids = self.get_all_mids()

        liquid = []
        for asset in universe:
            coin = asset["name"]
            if coin not in mids:
                continue
            # Use markPx * dayNtlVlm if available, else include by default
            liquid.append(coin)

        # Filter by volume using assetCtxs if available
        try:
            ctxs = self.info.meta_and_asset_ctxs()
            asset_meta = ctxs[0].get("universe", [])
            asset_ctxs = ctxs[1]
            filtered = []
            for i, ctx in enumerate(asset_ctxs):
                if i >= len(asset_meta):
                    break
                coin = asset_meta[i]["name"]
                day_volume = float(ctx.get("dayNtlVlm", 0))
                if day_volume >= min_volume_24h:
                    filtered.append(coin)
            return filtered if filtered else liquid
        except Exception:
            return liquid

    def get_sz_decimals(self, coin: str) -> int:
        meta = self.get_meta()
        for asset in meta.get("universe", []):
            if asset["name"] == coin:
                return int(asset.get("szDecimals", 4))
        return 4

    def get_candles(self, coin: str, interval: str = "15m", lookback_hours: int = 48) -> pd.DataFrame:
        """Fetch OHLCV candles and return as DataFrame."""
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - lookback_hours * 3600 * 1000
        try:
            candles = self.info.candles_snapshot(coin, interval, start_ms, end_ms)
            if not candles:
                return pd.DataFrame()
            df = pd.DataFrame(candles)
            df = df.rename(columns={"t": "time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["time"] = pd.to_datetime(df["time"], unit="ms")
            df = df.sort_values("time").reset_index(drop=True)
            return df
        except Exception as e:
            logger.warning(f"Failed to fetch candles for {coin}: {e}")
            return pd.DataFrame()

    def get_account_value(self) -> float:
        """Return total account value in USD."""
        if self.paper_trading:
            # Paper trading: return from env or default
            return float(os.getenv("PAPER_BALANCE", "10000"))
        try:
            state = self.info.user_state(self.wallet_address)
            return float(state["marginSummary"]["accountValue"])
        except Exception as e:
            logger.error(f"Failed to get account value: {e}")
            return 0.0

    def get_open_positions(self) -> list[dict]:
        """Return current open positions from the exchange."""
        if self.paper_trading:
            return []
        try:
            state = self.info.user_state(self.wallet_address)
            positions = []
            for pos in state.get("assetPositions", []):
                p = pos.get("position", {})
                szi = float(p.get("szi", 0))
                if szi != 0:
                    positions.append({
                        "coin": p["coin"],
                        "size": abs(szi),
                        "direction": "long" if szi > 0 else "short",
                        "entry_price": float(p.get("entryPx", 0)),
                        "unrealized_pnl": float(p.get("unrealizedPnl", 0)),
                    })
            return positions
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

    def get_mid_price(self, coin: str) -> Optional[float]:
        mids = self.get_all_mids()
        return mids.get(coin)

    # ─── Order Execution ──────────────────────────────────────────────────────

    def set_leverage(self, coin: str, leverage: int):
        if self.paper_trading:
            return
        try:
            self.exchange.update_leverage(leverage, coin, is_cross=False)
        except Exception as e:
            logger.warning(f"Failed to set leverage for {coin}: {e}")

    def open_position(
        self,
        coin: str,
        direction: str,
        size_usd: float,
        current_price: float,
    ) -> Optional[dict]:
        """Open a position. Returns order result dict or None on failure."""
        is_buy = direction == "long"
        sz_decimals = self.get_sz_decimals(coin)
        size_coin = round(size_usd / current_price, sz_decimals)

        if size_coin <= 0:
            logger.warning(f"Computed size_coin={size_coin} for {coin}, skipping")
            return None

        if self.paper_trading:
            logger.info(f"[PAPER] OPEN {direction.upper()} {coin}: {size_coin} @ ${current_price:.4f} (${size_usd:.2f})")
            return {
                "status": "ok",
                "order_id": f"paper_{coin}_{int(time.time())}",
                "size_coin": size_coin,
                "fill_price": current_price,
            }

        try:
            self.set_leverage(coin, self.leverage)
            result = self.exchange.market_open(coin, is_buy, size_coin, slippage=0.01)
            logger.info(f"OPEN {direction.upper()} {coin}: {size_coin} result={result}")
            fill_price = self._extract_fill_price(result, current_price)
            return {
                "status": "ok",
                "order_id": str(result),
                "size_coin": size_coin,
                "fill_price": fill_price,
            }
        except Exception as e:
            logger.error(f"Failed to open {direction} {coin}: {e}")
            return None

    def close_position(
        self,
        coin: str,
        direction: str,
        size_coin: float,
        current_price: float,
    ) -> Optional[dict]:
        """Close a position. Returns fill price or None on failure."""
        if self.paper_trading:
            logger.info(f"[PAPER] CLOSE {direction.upper()} {coin}: {size_coin} @ ${current_price:.4f}")
            return {"status": "ok", "fill_price": current_price}

        try:
            sz_decimals = self.get_sz_decimals(coin)
            size_coin = round(size_coin, sz_decimals)
            result = self.exchange.market_close(coin, sz=size_coin, slippage=0.01)
            logger.info(f"CLOSE {coin}: result={result}")
            fill_price = self._extract_fill_price(result, current_price)
            return {"status": "ok", "fill_price": fill_price}
        except Exception as e:
            logger.error(f"Failed to close {coin}: {e}")
            return None

    def _extract_fill_price(self, result, fallback: float) -> float:
        try:
            if isinstance(result, dict):
                statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                if statuses and "filled" in statuses[0]:
                    return float(statuses[0]["filled"].get("avgPx", fallback))
        except Exception:
            pass
        return fallback
