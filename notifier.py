from __future__ import annotations

import html
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests
from dotenv import load_dotenv

log = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    enabled: bool = True
    timeout_seconds: int = 10


class TelegramNotifier:
    def __init__(self, config: TelegramConfig):
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{self.config.bot_token}"

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        load_dotenv()
        bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
        enabled = os.getenv("TELEGRAM_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

        if not bot_token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

        return cls(TelegramConfig(bot_token=bot_token, chat_id=chat_id, enabled=enabled))

    def send_message(self, text: str, disable_notification: bool = False) -> bool:
        if not self.config.enabled:
            return False

        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": disable_notification,
        }

        try:
            response = requests.post(url, json=payload, timeout=self.config.timeout_seconds)
            response.raise_for_status()
            data = response.json()

            if not data.get("ok", False):
                log.error("Telegram API returned not ok: %s", data)
                return False

            return True

        except requests.RequestException as exc:
            log.exception("Failed to send Telegram message: %s", exc)
            return False

    def send_trade_opened(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        size_usd: float,
        score: Optional[float] = None,
        strategy: Optional[str] = None,
        conviction: Optional[str] = None,
        leverage: int = 1,
        paper: bool = True,
    ) -> bool:
        safe_symbol = html.escape(symbol)
        safe_strategy = html.escape(strategy) if strategy else "N/A"
        safe_direction = html.escape(direction.upper())
        safe_conviction = html.escape(conviction.upper()) if conviction else "N/A"

        score_line = f"\n<b>Score:</b> {score:.2f}" if score is not None else ""
        paper_line = "\n<b>Mode:</b> PAPER" if paper else ""

        message = (
            "🟢 <b>TRADE OPENED</b>\n"
            f"<b>Symbol:</b> {safe_symbol}\n"
            f"<b>Direction:</b> {safe_direction}\n"
            f"<b>Entry:</b> ${entry_price:.4f}\n"
            f"<b>Size:</b> ${size_usd:.2f}\n"
            f"<b>Leverage:</b> {leverage}x\n"
            f"<b>Conviction:</b> {safe_conviction}"
            f"{score_line}\n"
            f"<b>Strategy:</b> {safe_strategy}"
            f"{paper_line}"
        )
        return self.send_message(message)

    def send_trade_closed(
        self,
        symbol: str,
        direction: str,
        exit_price: float,
        pnl_usd: float,
        pnl_pct: float,
        close_reason: str,
        hold_minutes: Optional[float] = None,
        paper: bool = True,
    ) -> bool:
        safe_symbol = html.escape(symbol)
        safe_reason = html.escape(close_reason)
        safe_direction = html.escape(direction.upper())

        emoji = "✅" if pnl_usd >= 0 else "🔴"
        hold_line = f"\n<b>Held:</b> {hold_minutes:.1f} min" if hold_minutes is not None else ""
        paper_line = "\n<b>Mode:</b> PAPER" if paper else ""

        message = (
            f"{emoji} <b>TRADE CLOSED</b>\n"
            f"<b>Symbol:</b> {safe_symbol}\n"
            f"<b>Direction:</b> {safe_direction}\n"
            f"<b>Exit:</b> ${exit_price:.4f}\n"
            f"<b>PnL:</b> ${pnl_usd:.2f} ({pnl_pct:.2f}%)\n"
            f"<b>Reason:</b> {safe_reason}"
            f"{hold_line}"
            f"{paper_line}"
        )
        return self.send_message(message)

    def send_intraday_update(
        self,
        realized_pnl_today: float,
        open_count: int,
        closed_today: int,
        wins_today: int,
        losses_today: int,
    ) -> bool:
        now_label = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        win_rate = (wins_today / (wins_today + losses_today) * 100) if (wins_today + losses_today) > 0 else 0.0

        message = (
            f"📊 <b>INTRADAY UPDATE</b> ({now_label})\n"
            f"<b>Today's PnL:</b> ${realized_pnl_today:.2f}\n"
            f"<b>Open trades:</b> {open_count}\n"
            f"<b>Closed today:</b> {closed_today}\n"
            f"<b>Win/Loss:</b> {wins_today}/{losses_today} ({win_rate:.1f}%)"
        )
        return self.send_message(message)


def build_notifier_or_none() -> Optional[TelegramNotifier]:
    try:
        return TelegramNotifier.from_env()
    except Exception as exc:
        log.warning("Telegram notifier disabled: %s", exc)
        return None
