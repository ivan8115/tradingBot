"""
Central configuration loaded from .env (secrets) + config.yaml (strategy params).
Import `settings` anywhere in the codebase — it's a singleton.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-models (loaded from config.yaml sections)
# ---------------------------------------------------------------------------


class BacktestConfig(BaseModel):
    start_date: str = "2023-01-01"
    end_date: str = "2024-01-01"
    start_capital: float = 100_000.0


class UniverseConfig(BaseModel):
    watchlist: list[str] = ["SPY", "QQQ"]
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)


class RiskConfig(BaseModel):
    max_portfolio_risk_pct: float = 0.02
    max_drawdown_pct: float = 0.15
    max_single_position_pct: float = 0.10
    max_delta_exposure: int = 500
    daily_loss_limit_pct: float = 0.03
    position_sizing_method: Literal["kelly", "fixed_fraction", "percent_equity"] = "kelly"
    kelly_fraction: float = 0.25


class CSPConfig(BaseModel):
    target_delta: float = -0.28
    min_dte: int = 21
    max_dte: int = 45
    profit_target_pct: float = 0.50
    stop_loss_multiplier: float = 2.0
    min_premium: float = 1.00
    min_iv_rank: float = 50.0
    roll_when_dte: int = 7       # close/roll when DTE reaches this threshold


class CCConfig(BaseModel):
    target_delta: float = 0.30
    min_dte: int = 21
    max_dte: int = 45
    profit_target_pct: float = 0.50
    roll_when_dte: int = 7


class WheelStrategyConfig(BaseModel):
    enabled: bool = True
    symbols: list[str] = []
    csp: CSPConfig = Field(default_factory=CSPConfig)
    cc: CCConfig = Field(default_factory=CCConfig)


class MomentumStrategyConfig(BaseModel):
    enabled: bool = True
    symbols: list[str] = []
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    ema_short: int = 9
    ema_long: int = 21


class MeanReversionStrategyConfig(BaseModel):
    enabled: bool = False
    symbols: list[str] = []
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14


class BreakoutStrategyConfig(BaseModel):
    enabled: bool = False
    symbols: list[str] = []
    lookback_period: int = 20
    volume_confirmation_multiplier: float = 1.5


class StrategiesConfig(BaseModel):
    wheel: WheelStrategyConfig = Field(default_factory=WheelStrategyConfig)
    momentum: MomentumStrategyConfig = Field(default_factory=MomentumStrategyConfig)
    mean_reversion: MeanReversionStrategyConfig = Field(default_factory=MeanReversionStrategyConfig)
    breakout: BreakoutStrategyConfig = Field(default_factory=BreakoutStrategyConfig)


class IndicatorsConfig(BaseModel):
    bar_window_size: int = 200
    vwap_reset: str = "market_open"


class ExecutionConfig(BaseModel):
    default_order_type: Literal["market", "limit"] = "limit"
    limit_price_offset_pct: float = 0.001
    order_timeout_seconds: int = 30


class SchedulerConfig(BaseModel):
    pre_market_hour: int = 8
    pre_market_minute: int = 0
    options_check_interval_minutes: int = 15
    pre_close_buffer_minutes: int = 15


class WatchlistConfig(BaseModel):
    max_symbols: int = 15           # max Wheel candidates per day
    min_price: float = 10.0        # stock price floor
    max_price: float = 50.0        # stock price ceiling (100 shares = $5K max collateral for small accounts)
    min_options_volume: int = 200  # minimum daily options volume
    quiverquant_boost: bool = True  # weight candidates with recent congressional buys
    refresh_hour: int = 8          # pre-market scan time (ET)
    refresh_minute: int = 30


class MonitoringConfig(BaseModel):
    slack_alerts: bool = False
    email_alerts: bool = False
    alert_on: list[str] = ["large_loss", "drawdown_breach", "fill", "daily_summary"]


class SystemConfig(BaseModel):
    mode: Literal["paper", "live", "backtest"] = "paper"
    log_level: str = "INFO"
    db_path: str = "data/trading.db"
    timezone: str = "America/New_York"


# ---------------------------------------------------------------------------
# Top-level settings (secrets from .env, everything else from config.yaml)
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """
    Secrets come from .env / environment variables.
    The rest is loaded from config.yaml via `from_yaml()`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # From .env
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    slack_webhook_url: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    alert_email_to: str = ""
    quiverquant_api_key: str = ""

    # From config.yaml — populated by load()
    system: SystemConfig = Field(default_factory=SystemConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    strategies: StrategiesConfig = Field(default_factory=StrategiesConfig)
    indicators: IndicatorsConfig = Field(default_factory=IndicatorsConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    watchlist: WatchlistConfig = Field(default_factory=WatchlistConfig)

    @classmethod
    def load(cls, config_path: str = "config.yaml") -> "Settings":
        """Load .env secrets then overlay config.yaml."""
        instance = cls()  # loads .env
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                yaml_data = yaml.safe_load(f) or {}
            # Re-parse with yaml overlay
            instance = cls(**{**instance.model_dump(), **yaml_data})
        return instance


# Singleton — import this everywhere
settings = Settings.load()
