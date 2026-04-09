"""Custom exception hierarchy for the trading bot."""


class TradingBotError(Exception):
    """Base exception for all trading bot errors."""


class ConfigurationError(TradingBotError):
    """Raised when configuration is missing or invalid."""


class BrokerError(TradingBotError):
    """Raised when brokerage API calls fail."""


class OrderError(BrokerError):
    """Raised when order submission or management fails."""


class InsufficientFundsError(OrderError):
    """Raised when account lacks funds for an order."""


class MarketDataError(TradingBotError):
    """Raised when market data cannot be fetched or parsed."""


class StrategyError(TradingBotError):
    """Raised when a strategy encounters an unrecoverable error."""


class RiskLimitBreached(TradingBotError):
    """Raised when a risk limit is breached and trading must halt."""


class BacktestError(TradingBotError):
    """Raised during backtesting when data or logic fails."""
