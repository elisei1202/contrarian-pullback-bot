import pandas as pd
import numpy as np
from typing import List, Optional


def validate_candles(candles: List[list]) -> bool:
    """
    Validate candle data format
    
    Args:
        candles: List of candles
        
    Returns:
        True if valid, False otherwise
    """
    if not candles:
        return False
    
    for candle in candles:
        if not isinstance(candle, (list, tuple)):
            return False
        if len(candle) < 5:
            return False
        try:
            float(candle[4])  # close price
        except (ValueError, TypeError, IndexError):
            return False
    
    return True


def calculate_ema(candles: List[list], period: int = 200) -> float:
    """
    Calculate EMA (Exponential Moving Average) - exact TradingView implementation
    
    Formula: EMA = (Close - EMA_prev) * (2 / (period + 1)) + EMA_prev
    This is equivalent to pandas ewm(span=period, adjust=False)
    
    Args:
        candles: List of OHLCV candles from Bybit
                 Can be in any order (will be sorted chronologically)
        period: EMA period (default 200)
    
    Returns:
        EMA value (float) for the most recent candle
    
    Raises:
        ValueError: If candles are invalid or insufficient
    """
    # Validate input
    if not validate_candles(candles):
        raise ValueError("Invalid candle data format")
    
    if len(candles) < period:
        raise ValueError(f"Need at least {period} candles for EMA{period}, got {len(candles)}")
    
    if period < 1:
        raise ValueError(f"Period must be >= 1, got {period}")
    
    # Create DataFrame
    columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover']
    df = pd.DataFrame(candles, columns=columns[:len(candles[0])])
    
    # Sort by timestamp (chronological order - oldest first)
    df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    # Convert close to float
    closes = pd.to_numeric(df['close'], errors='coerce').astype(float)
    
    # Check for NaN values
    if closes.isna().any():
        raise ValueError("Candle data contains invalid close prices")
    
    # TradingView EMA formula: ewm(span=period, adjust=False)
    ema = closes.ewm(span=period, adjust=False).mean()
    
    result = float(ema.iloc[-1])
    
    # Validate output
    if np.isnan(result):
        raise ValueError("EMA calculation resulted in NaN")
    
    return result


def calculate_ema_series(candles: List[list], period: int = 200) -> List[Optional[float]]:
    """
    Calculate EMA for all candles (for charting)
    
    Args:
        candles: List of OHLCV candles
        period: EMA period
    
    Returns:
        List of EMA values for each candle (None for insufficient data points)
    """
    if not validate_candles(candles):
        raise ValueError("Invalid candle data format")
    
    if len(candles) == 0:
        return []
    
    columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover']
    df = pd.DataFrame(candles, columns=columns[:len(candles[0])])
    
    df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    closes = pd.to_numeric(df['close'], errors='coerce').astype(float)
    
    ema = closes.ewm(span=period, adjust=False).mean()
    
    # Return None for first (period-1) values where EMA is not reliable
    result = []
    for i, v in enumerate(ema):
        if i < period - 1:
            result.append(None)
        elif np.isnan(v):
            result.append(None)
        else:
            result.append(float(v))
    
    return result


def calculate_sma(candles: List[list], period: int = 20) -> float:
    """
    Calculate SMA (Simple Moving Average)
    
    Args:
        candles: List of OHLCV candles
        period: SMA period
    
    Returns:
        SMA value for the most recent candle
    """
    if not validate_candles(candles):
        raise ValueError("Invalid candle data format")
    
    if len(candles) < period:
        raise ValueError(f"Need at least {period} candles for SMA{period}, got {len(candles)}")
    
    columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover']
    df = pd.DataFrame(candles, columns=columns[:len(candles[0])])
    
    df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    closes = pd.to_numeric(df['close'], errors='coerce').astype(float)
    
    sma = closes.rolling(window=period).mean()
    
    result = float(sma.iloc[-1])
    
    if np.isnan(result):
        raise ValueError("SMA calculation resulted in NaN")
    
    return result
