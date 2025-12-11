import os
from typing import List
from dataclasses import dataclass

@dataclass
class BybitConfig:
    api_key: str = os.getenv("BYBIT_API_KEY", "")
    api_secret: str = os.getenv("BYBIT_API_SECRET", "")
    testnet: bool = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
    
    @property
    def base_url(self) -> str:
        return "https://api-testnet.bybit.com" if self.testnet else "https://api.bybit.com"

@dataclass
class TradingConfig:
    symbols: List[str] = None
    position_size_usdt: float = 100.0
    leverage: int = 20
    margin_mode: str = "ISOLATED"
    
    def __post_init__(self):
        if self.symbols is None:
            symbols_str = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT")
            self.symbols = [s.strip() for s in symbols_str.split(",")]
        self.position_size_usdt = float(os.getenv("POSITION_SIZE_USDT", "100"))
        self.leverage = int(os.getenv("LEVERAGE", "20"))

@dataclass
class IndicatorConfig:
    # 4H Indicators
    ema_period_4h: int = 200
    st_period_4h: int = 10
    st_multiplier_4h: float = 3.0
    
    # 1H Indicators
    st_period_1h: int = 10
    st_multiplier_1h: float = 3.0
    
    def __post_init__(self):
        self.ema_period_4h = int(os.getenv("EMA_PERIOD_4H", "200"))
        self.st_period_4h = int(os.getenv("ST_PERIOD_4H", "10"))
        self.st_multiplier_4h = float(os.getenv("ST_MULTIPLIER_4H", "3.0"))
        self.st_period_1h = int(os.getenv("ST_PERIOD_1H", "10"))
        self.st_multiplier_1h = float(os.getenv("ST_MULTIPLIER_1H", "3.0"))

@dataclass
class BotConfig:
    # Bot settings
    check_interval_seconds: int = 300
    update_4h_interval_hours: int = 4
    trading_enabled: bool = True
    port: int = 10000
    
    # Sub-configs
    bybit: BybitConfig = None
    trading: TradingConfig = None
    indicators: IndicatorConfig = None
    
    def __post_init__(self):
        if self.bybit is None:
            self.bybit = BybitConfig()
        if self.trading is None:
            self.trading = TradingConfig()
        if self.indicators is None:
            self.indicators = IndicatorConfig()
        self.port = int(os.getenv("PORT", "10000"))
        self.check_interval_seconds = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
        self.update_4h_interval_hours = int(os.getenv("UPDATE_4H_HOURS", "4"))
    
    def validate(self):
        """Validate critical configuration"""
        if not self.bybit.api_key or not self.bybit.api_secret:
            raise ValueError("BYBIT_API_KEY and BYBIT_API_SECRET must be set!")
        
        if len(self.trading.symbols) == 0:
            raise ValueError("At least one trading symbol must be configured!")
        
        if self.trading.leverage < 1 or self.trading.leverage > 100:
            raise ValueError("Leverage must be between 1 and 100!")
        
        return True

# Global config instance
config = BotConfig()
