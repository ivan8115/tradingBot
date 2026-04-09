"""
AlertManager — sends notifications for critical trading events.
Supports Slack webhooks and email (SMTP).
"""

from __future__ import annotations

import smtplib
import urllib.request
import json
from email.mime.text import MIMEText
from enum import Enum
from typing import Optional

from loguru import logger

from core.config import settings


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertManager:
    """
    Sends alerts to configured channels (Slack, email).
    Non-blocking: failures are logged but do not raise.
    """

    def __init__(self) -> None:
        self._slack_enabled = settings.monitoring.slack_alerts and bool(settings.slack_webhook_url)
        self._email_enabled = settings.monitoring.email_alerts and bool(settings.smtp_user)
        self._alert_on = set(settings.monitoring.alert_on)

    def alert(
        self,
        event_type: str,
        message: str,
        level: AlertLevel = AlertLevel.INFO,
        data: Optional[dict] = None,
    ) -> None:
        """Send an alert if the event_type is in the configured alert list."""
        if event_type not in self._alert_on and "all" not in self._alert_on:
            return

        full_message = f"[{level.value.upper()}] {event_type}: {message}"
        if data:
            full_message += f"\n{json.dumps(data, indent=2, default=str)}"

        logger.info(f"ALERT | {full_message}")

        if self._slack_enabled:
            self._send_slack(full_message, level)
        if self._email_enabled:
            self._send_email(f"TradingBot Alert: {event_type}", full_message)

    def fill_alert(self, symbol: str, side: str, qty: int, price: float, strategy: str) -> None:
        self.alert(
            event_type="fill",
            message=f"{side.upper()} {qty}× {symbol} @ ${price:.2f} [{strategy}]",
            level=AlertLevel.INFO,
            data={"symbol": symbol, "side": side, "qty": qty, "price": price},
        )

    def drawdown_alert(self, drawdown_pct: float, threshold_pct: float) -> None:
        self.alert(
            event_type="drawdown_breach",
            message=f"Portfolio drawdown {drawdown_pct:.1f}% breached threshold {threshold_pct:.1f}%",
            level=AlertLevel.CRITICAL,
            data={"drawdown_pct": drawdown_pct, "threshold_pct": threshold_pct},
        )

    def daily_summary_alert(self, summary: dict) -> None:
        msg = (
            f"Daily P&L: ${summary.get('realized_pnl', 0):+,.2f} | "
            f"Total: ${summary.get('total_value', 0):,.2f} | "
            f"Drawdown: {summary.get('drawdown_pct', 0):.1f}%"
        )
        self.alert(event_type="daily_summary", message=msg, data=summary)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _send_slack(self, message: str, level: AlertLevel) -> None:
        emoji = {"info": "📊", "warning": "⚠️", "critical": "🚨"}.get(level.value, "📌")
        payload = json.dumps({"text": f"{emoji} {message}"}).encode("utf-8")
        try:
            req = urllib.request.Request(
                settings.slack_webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status != 200:
                    logger.warning(f"Slack alert failed: HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"Slack alert error: {e}")

    def _send_email(self, subject: str, body: str) -> None:
        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = settings.smtp_user
            msg["To"] = settings.alert_email_to

            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_pass)
                server.sendmail(settings.smtp_user, [settings.alert_email_to], msg.as_string())
        except Exception as e:
            logger.warning(f"Email alert error: {e}")


# Singleton
alerter = AlertManager()
