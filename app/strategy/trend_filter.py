from typing import Literal

TrendType = Literal["BULLISH", "BEARISH", "NEUTRAL"]

class TrendFilter:
    """
    4H Trend Filter using EMA 200 + SuperTrend
    
    Trend Detection Rules:
    - BULLISH: close > EMA200 AND SuperTrend green
    - BEARISH: close < EMA200 AND SuperTrend red
    - NEUTRAL: otherwise (including when close == EMA200, or when price/ST conditions don't match)
    
    Note:
        When close == EMA200, the trend is classified as NEUTRAL regardless of SuperTrend direction.
        This is intentional to avoid false signals when price is exactly at the EMA200 level.
    """
    
    def __init__(self, ema_period: int = 200):
        self.ema_period = ema_period
    
    def detect_trend(
        self,
        close: float,
        ema200: float,
        st_direction: str
    ) -> TrendType:
        """
        Detect trend based on price position and indicators
        
        Args:
            close: Current close price
            ema200: EMA 200 value
            st_direction: SuperTrend direction ("green" or "red")
        
        Returns:
            Trend type: "BULLISH", "BEARISH", or "NEUTRAL"
        """
        if close > ema200 and st_direction == "green":
            return "BULLISH"
        elif close < ema200 and st_direction == "red":
            return "BEARISH"
        else:
            return "NEUTRAL"
