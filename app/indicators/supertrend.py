import pandas as pd
import numpy as np
from typing import Tuple, List, Optional


def rma(series: pd.Series, period: int) -> pd.Series:
    """
    RMA (Wilder) - exact TradingView implementation
    
    rma[i] = (rma[i-1] * (period - 1) + series[i]) / period
    """
    rma_result = series.copy().astype(float)
    rma_result.iloc[:] = np.nan

    if len(series) < period:
        return rma_result

    # First value = SMA
    first_val = series.iloc[:period].mean()
    rma_result.iloc[period - 1] = first_val

    alpha = 1.0 / period
    for i in range(period, len(series)):
        rma_result.iloc[i] = alpha * series.iloc[i] + (1 - alpha) * rma_result.iloc[i - 1]

    return rma_result


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
        # Check if OHLC values are numeric
        try:
            float(candle[1])  # open
            float(candle[2])  # high
            float(candle[3])  # low
            float(candle[4])  # close
        except (ValueError, TypeError, IndexError):
            return False
    
    return True


def calculate_supertrend(
    candles: List[list],
    period: int = 10,
    multiplier: float = 3.0
) -> Tuple[str, float]:
    """
    SuperTrend - exact TradingView implementation (classic ATR with RMA/Wilder)
    
    Args:
        candles: List of OHLCV candles [timestamp, open, high, low, close, volume, turnover]
                 Can be in any order (will be sorted chronologically)
        period: ATR period (default 10)
        multiplier: ATR multiplier (default 3.0)
    
    Returns:
        Tuple of:
        - direction: "green" (uptrend) / "red" (downtrend)
        - value: SuperTrend line value on the last candle
    
    Raises:
        ValueError: If candles are invalid or insufficient
    """
    # Validate input
    if not validate_candles(candles):
        raise ValueError("Invalid candle data format")
    
    if len(candles) < period + 1:
        raise ValueError(f"Need at least {period + 1} candles for SuperTrend calculation, got {len(candles)}")
    
    if period < 1:
        raise ValueError(f"Period must be >= 1, got {period}")
    
    if multiplier <= 0:
        raise ValueError(f"Multiplier must be > 0, got {multiplier}")
    
    # Create DataFrame
    df = pd.DataFrame(candles, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'
    ][:len(candles[0])])  # Handle variable column count
    
    # Sort by timestamp (chronological order - oldest first)
    df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    # Convert to float
    df['high'] = pd.to_numeric(df['high'], errors='coerce').astype(float)
    df['low'] = pd.to_numeric(df['low'], errors='coerce').astype(float)
    df['close'] = pd.to_numeric(df['close'], errors='coerce').astype(float)
    
    # Check for NaN values after conversion
    if df[['high', 'low', 'close']].isna().any().any():
        raise ValueError("Candle data contains invalid numeric values")
    
    # ATR calculation with RMA (TradingView style)
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = rma(tr, period)
    
    # hl2 and basic bands
    hl2 = (df['high'] + df['low']) / 2.0
    basic_upper_band = hl2 + multiplier * atr
    basic_lower_band = hl2 - multiplier * atr
    
    # Initialize series
    final_upper_band = pd.Series(index=df.index, dtype=float)
    final_lower_band = pd.Series(index=df.index, dtype=float)
    supertrend = pd.Series(index=df.index, dtype=float)
    trend = pd.Series(index=df.index, dtype=int)  # 1 = UP, -1 = DOWN
    
    for i in range(len(df)):
        if i == 0:
            final_upper_band.iloc[i] = basic_upper_band.iloc[i]
            final_lower_band.iloc[i] = basic_lower_band.iloc[i]
            trend.iloc[i] = 1
            supertrend.iloc[i] = final_lower_band.iloc[i]
            continue
        
        # Final upper band
        prev_final_upper = final_upper_band.iloc[i - 1]
        prev_close = df['close'].iloc[i - 1]
        bu = basic_upper_band.iloc[i]
        
        if np.isnan(prev_final_upper):
            final_upper_band.iloc[i] = bu
        else:
            if bu < prev_final_upper or prev_close > prev_final_upper:
                final_upper_band.iloc[i] = bu
            else:
                final_upper_band.iloc[i] = prev_final_upper
        
        # Final lower band
        prev_final_lower = final_lower_band.iloc[i - 1]
        bl = basic_lower_band.iloc[i]
        
        if np.isnan(prev_final_lower):
            final_lower_band.iloc[i] = bl
        else:
            if bl > prev_final_lower or prev_close < prev_final_lower:
                final_lower_band.iloc[i] = bl
            else:
                final_lower_band.iloc[i] = prev_final_lower
        
        # Trend decision
        curr_close = df['close'].iloc[i]
        
        if curr_close > final_upper_band.iloc[i - 1]:
            trend.iloc[i] = 1
        elif curr_close < final_lower_band.iloc[i - 1]:
            trend.iloc[i] = -1
        else:
            trend.iloc[i] = trend.iloc[i - 1]
            
            # Band locking (TradingView behavior)
            if trend.iloc[i] == 1 and final_lower_band.iloc[i] < final_lower_band.iloc[i - 1]:
                final_lower_band.iloc[i] = final_lower_band.iloc[i - 1]
            if trend.iloc[i] == -1 and final_upper_band.iloc[i] > final_upper_band.iloc[i - 1]:
                final_upper_band.iloc[i] = final_upper_band.iloc[i - 1]
        
        # SuperTrend value
        if trend.iloc[i] == 1:
            supertrend.iloc[i] = final_lower_band.iloc[i]
        else:
            supertrend.iloc[i] = final_upper_band.iloc[i]
    
    last_trend = trend.iloc[-1]
    last_value = float(supertrend.iloc[-1])
    
    # Validate output
    if np.isnan(last_value):
        raise ValueError("SuperTrend calculation resulted in NaN")
    
    direction = "green" if last_trend == 1 else "red"
    return direction, last_value


def calculate_supertrend_series(
    candles: List[list],
    period: int = 10,
    multiplier: float = 3.0
) -> Tuple[List[str], List[float]]:
    """
    Calculate SuperTrend for all candles (for charting)
    
    Returns:
        Tuple of:
        - directions: List of "green"/"red" for each candle
        - values: List of SuperTrend values for each candle
    """
    if not validate_candles(candles):
        raise ValueError("Invalid candle data format")
    
    if len(candles) < period + 1:
        return [], []
    
    # Similar calculation as above but return full series
    df = pd.DataFrame(candles, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'
    ][:len(candles[0])])
    
    df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    df['high'] = pd.to_numeric(df['high'], errors='coerce').astype(float)
    df['low'] = pd.to_numeric(df['low'], errors='coerce').astype(float)
    df['close'] = pd.to_numeric(df['close'], errors='coerce').astype(float)
    
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = rma(tr, period)
    
    hl2 = (df['high'] + df['low']) / 2.0
    basic_upper_band = hl2 + multiplier * atr
    basic_lower_band = hl2 - multiplier * atr
    
    final_upper_band = pd.Series(index=df.index, dtype=float)
    final_lower_band = pd.Series(index=df.index, dtype=float)
    supertrend = pd.Series(index=df.index, dtype=float)
    trend = pd.Series(index=df.index, dtype=int)
    
    for i in range(len(df)):
        if i == 0:
            final_upper_band.iloc[i] = basic_upper_band.iloc[i]
            final_lower_band.iloc[i] = basic_lower_band.iloc[i]
            trend.iloc[i] = 1
            supertrend.iloc[i] = final_lower_band.iloc[i]
            continue
        
        prev_final_upper = final_upper_band.iloc[i - 1]
        prev_close = df['close'].iloc[i - 1]
        bu = basic_upper_band.iloc[i]
        
        if np.isnan(prev_final_upper):
            final_upper_band.iloc[i] = bu
        else:
            if bu < prev_final_upper or prev_close > prev_final_upper:
                final_upper_band.iloc[i] = bu
            else:
                final_upper_band.iloc[i] = prev_final_upper
        
        prev_final_lower = final_lower_band.iloc[i - 1]
        bl = basic_lower_band.iloc[i]
        
        if np.isnan(prev_final_lower):
            final_lower_band.iloc[i] = bl
        else:
            if bl > prev_final_lower or prev_close < prev_final_lower:
                final_lower_band.iloc[i] = bl
            else:
                final_lower_band.iloc[i] = prev_final_lower
        
        curr_close = df['close'].iloc[i]
        
        if curr_close > final_upper_band.iloc[i - 1]:
            trend.iloc[i] = 1
        elif curr_close < final_lower_band.iloc[i - 1]:
            trend.iloc[i] = -1
        else:
            trend.iloc[i] = trend.iloc[i - 1]
            
            if trend.iloc[i] == 1 and final_lower_band.iloc[i] < final_lower_band.iloc[i - 1]:
                final_lower_band.iloc[i] = final_lower_band.iloc[i - 1]
            if trend.iloc[i] == -1 and final_upper_band.iloc[i] > final_upper_band.iloc[i - 1]:
                final_upper_band.iloc[i] = final_upper_band.iloc[i - 1]
        
        if trend.iloc[i] == 1:
            supertrend.iloc[i] = final_lower_band.iloc[i]
        else:
            supertrend.iloc[i] = final_upper_band.iloc[i]
    
    directions = ["green" if t == 1 else "red" for t in trend]
    values = [float(v) if not np.isnan(v) else None for v in supertrend]
    
    return directions, values
