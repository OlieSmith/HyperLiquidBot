import logging
import os
import asyncio
from typing import Optional

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        self._bot = None

        if not self.enabled:
            logger.warning("Telegram not configured — notifications disabled")

    def _get_bot(self):
        if self._bot is None and self.enabled:
            try:
                from telegram import Bot
                self._bot = Bot(token=self.token)
            except Exception as e:
                logger.error(f"Failed to create Telegram bot: {e}")
                self.enabled = False
        return self._bot

    def _send(self, text: str):
        if not self.enabled:
            return
        bot = self._get_bot()
        if not bot:
            return
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
            ))
            loop.close()
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    def trade_opened(self, trade: dict):
        direction_emoji = "📈" if trade["direction"] == "long" else "📉"
        paper = " [PAPER]" if trade.get("paper_trade") else ""
        msg = (
            f"{direction_emoji} <b>TRADE OPENED{paper}</b>\n"
            f"Coin: <b>{trade['coin']}</b>\n"
            f"Direction: {trade['direction'].upper()}\n"
            f"Strategy: {trade['strategy']}\n"
            f"Conviction: {trade['conviction'].upper()}\n"
            f"Entry: ${trade['entry_price']:,.4f}\n"
            f"Size: ${trade['size_usd']:,.2f} ({trade['size_coin']:.4f} coins)\n"
            f"Leverage: {trade['leverage']}x"
        )
        logger.info(f"[NOTIFIER] Trade opened: {trade['coin']} {trade['direction']}")
        self._send(msg)

    def trade_closed(self, trade: dict):
        pnl = trade.get("pnl_usd", 0)
        pnl_pct = trade.get("pnl_pct", 0)
        emoji = "✅" if pnl > 0 else "❌"
        paper = " [PAPER]" if trade.get("paper_trade") else ""
        msg = (
            f"{emoji} <b>TRADE CLOSED{paper}</b>\n"
            f"Coin: <b>{trade['coin']}</b>\n"
            f"Direction: {trade['direction'].upper()}\n"
            f"Entry: ${trade['entry_price']:,.4f} → Exit: ${trade['exit_price']:,.4f}\n"
            f"PnL: <b>${pnl:+,.2f} ({pnl_pct:+.2f}%)</b>\n"
            f"Reason: {trade.get('close_reason', 'unknown')}"
        )
        logger.info(f"[NOTIFIER] Trade closed: {trade['coin']} PnL=${pnl:+.2f}")
        self._send(msg)

    def error(self, msg: str):
        text = f"⚠️ <b>BOT ERROR</b>\n{msg}"
        logger.error(f"[NOTIFIER] {msg}")
        self._send(text)

    def info(self, msg: str):
        text = f"ℹ️ {msg}"
        logger.info(f"[NOTIFIER] {msg}")
        self._send(text)

    def stats(self, stats: dict):
        wins = stats.get("wins", 0) or 0
        losses = stats.get("losses", 0) or 0
        total_pnl = stats.get("total_pnl", 0) or 0
        avg_pct = stats.get("avg_pnl_pct", 0) or 0
        open_count = stats.get("open_count", 0) or 0
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

        msg = (
            f"📊 <b>BOT STATS</b>\n"
            f"Open positions: {open_count}\n"
            f"Win/Loss: {wins}/{losses} ({win_rate:.1f}%)\n"
            f"Total PnL: <b>${total_pnl:+,.2f}</b>\n"
            f"Avg PnL: {avg_pct:+.2f}%"
        )
        self._send(msg)
