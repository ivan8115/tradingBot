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
    max_single_position_pct: float = 0.20
    max_delta_exposure: int = 500
    daily_loss_limit_pct: float = 0.03
    position_sizing_method: Literal["kelly", "fixed_fraction", "percent_equity"] = "kelly"
    kelly_fraction: float = 0.25
    max_weekly_momentum_trades: int = 10


class CSPConfig(BaseModel):
    target_delta: float = -0.28
    min_dte: int = 21
    max_dte: int = 45
    profit_target_pct: float = 0.50
    stop_loss_multiplier: float = 2.0
    min_premium: float = 1.00
    min_iv_rank: float = 50.0
    roll_when_dte: int = 7       # close/roll when DTE reaches this threshold
    pain_threshold_default: float = 0.85  # Close if underlying < strike × this value
    mark_stop_multiplier: float = 3.0  # Close if mark reaches 3× credit (IV spike stop)


class CCConfig(BaseModel):
    target_delta: float = 0.30
    min_dte: int = 21
    max_dte: int = 45
    profit_target_pct: float = 0.50
    roll_when_dte: int = 7
    stock_stop_loss_pct: float = 0.90


class WheelSymbolOverride(BaseModel):
    pain_threshold: float | None = None


class WheelStrategyConfig(BaseModel):
    enabled: bool = True
    symbols: list[str] = []
    csp: CSPConfig = Field(default_factory=CSPConfig)
    cc: CCConfig = Field(default_factory=CCConfig)
    symbol_overrides: dict[str, WheelSymbolOverride] = Field(default_factory=dict)


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


class SwingStrategyConfig(BaseModel):
    enabled: bool = True
    symbols: list[str] = []
    stage_requirement: int = 2        # Minervini Stage 2 minimum
    atr_stop_mult: float = 2.0
    atr_target_mult: float = 4.0
    max_hold_bars: int = 30           # force-exit stale positions
    min_bars_for_entry: int = 50


class StrategiesConfig(BaseModel):
    wheel: WheelStrategyConfig = Field(default_factory=WheelStrategyConfig)
    momentum: MomentumStrategyConfig = Field(default_factory=MomentumStrategyConfig)
    mean_reversion: MeanReversionStrategyConfig = Field(default_factory=MeanReversionStrategyConfig)
    breakout: BreakoutStrategyConfig = Field(default_factory=BreakoutStrategyConfig)
    swing: SwingStrategyConfig = Field(default_factory=SwingStrategyConfig)


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
    min_options_volume: int = 200  # kept for future real options volume check
    min_stock_volume: int = 500_000  # proxy for liquidity: require 500K+ daily shares traded
    quiverquant_boost: bool = True  # weight candidates with recent congressional buys
    refresh_hour: int = 8          # pre-market scan time (ET)
    refresh_minute: int = 30


class MonitoringConfig(BaseModel):
    slack_alerts: bool = False
    email_alerts: bool = False
    alert_on: list[str] = ["large_loss", "drawdown_breach", "fill", "daily_summary"]


class PerplexityConfig(BaseModel):
    enabled: bool = True
    timeout_seconds: int = 15
    max_symbols_per_call: int = 10


class GuardrailsConfig(BaseModel):
    max_open_positions: int = 6
    max_new_trades_per_week: int = 3
    max_position_pct: float = 0.20
    max_total_deployed_pct: float = 0.80


class ClaudeConfig(BaseModel):
    enabled: bool = True
    opus_model: str = "claude-opus-4-7"
    sonnet_model: str = "claude-sonnet-4-6"
    haiku_model: str = "claude-haiku-4-5-20251001"
    max_tokens_signal: int = 1024
    max_tokens_briefing: int = 2048
    max_tokens_review: int = 4096
    signal_eval_timeout_seconds: int = 10
    briefing_timeout_seconds: int = 30
    max_signal_evals_per_day: int = 50   # was 20 — 15 symbols hit 20 evals in < 20 min


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
    claude_api_key: str = ""
    perplexity_api_key: str = ""

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
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    perplexity: PerplexityConfig = Field(default_factory=PerplexityConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)

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
