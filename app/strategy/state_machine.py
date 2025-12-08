from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class SymbolState:
    """
    State tracking for each trading symbol
    
    Thread Safety:
    - All state modifications should be done through methods
    - Use asyncio.Lock() in bot_controller for concurrent access
    """
    symbol: str
    
    # 4H Trend data
    trend_4h: Optional[str] = None
    ema200_4h: Optional[float] = None
    st_4h_direction: Optional[str] = None
    st_4h_prev_direction: Optional[str] = None
    st_4h_value: Optional[float] = None
    last_4h_update: Optional[datetime] = None
    
    # 1H Signal data
    st_1h_direction: Optional[str] = None
    st_1h_prev_direction: Optional[str] = None
    st_1h_value: Optional[float] = None
    
    # Position data
    position_side: Optional[str] = None
    position_size: Optional[float] = None
    entry_price: Optional[float] = None
    entry_time: Optional[datetime] = None
    
    # Stats
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    
    # Partial TP tracking
    partial_tp_done: bool = False
    tp_limit_order_id: Optional[str] = None
    
    # ===== FIX: Track last processed candle to prevent duplicate entries =====
    last_processed_1h_candle: Optional[int] = None  # Timestamp of last processed 1H candle
    last_processed_4h_candle: Optional[int] = None  # Timestamp of last processed 4H candle
    
    def update_trend_4h(self, trend: str, ema200: float, st_dir: str, st_val: float):
        """Update 4H trend information"""
        # ===== FIX 5.2: Initialize st_4h_prev_direction correctly =====
        # Save previous direction before updating
        # If this is the first update (st_4h_direction is None), set prev to current
        if self.st_4h_direction is None:
            self.st_4h_prev_direction = st_dir  # First update: prev = current
        else:
            self.st_4h_prev_direction = self.st_4h_direction
        
        self.trend_4h = trend
        self.ema200_4h = ema200
        self.st_4h_direction = st_dir
        self.st_4h_value = st_val
        self.last_4h_update = datetime.now()
    
    def update_1h_signal(self, st_dir: str, st_val: float):
        """Update 1H signal information"""
        self.st_1h_prev_direction = self.st_1h_direction
        self.st_1h_direction = st_dir
        self.st_1h_value = st_val
    
    def open_position(self, side: str, size: float, price: float):
        """
        Open new position
        
        Args:
            side: "LONG" or "SHORT"
            size: Position size in base currency
            price: Entry price
        """
        if side not in ["LONG", "SHORT"]:
            raise ValueError(f"Invalid side: {side}. Must be 'LONG' or 'SHORT'")
        
        if size <= 0:
            raise ValueError(f"Invalid size: {size}. Must be positive")
        
        if price <= 0:
            raise ValueError(f"Invalid price: {price}. Must be positive")
        
        self.position_side = side
        self.position_size = size
        self.entry_price = price
        self.entry_time = datetime.now()
        self.partial_tp_done = False
        self.tp_limit_order_id = None
    
    def close_position(self, exit_price: float, pnl: Optional[float] = None):
        """
        Close position and update stats
        
        Args:
            exit_price: Exit price
            pnl: Pre-calculated PnL (optional). If None, will be calculated.
        
        Note:
            ===== FIX 7.1: Accept pre-calculated PnL to avoid duplication =====
            If pnl is provided, it should be calculated using the same logic as
            BotController._calculate_pnl() to ensure consistency.
        """
        if not self.position_side or not self.entry_price:
            return
        
        if exit_price <= 0:
            raise ValueError(f"Invalid exit_price: {exit_price}. Must be positive")
        
        # ===== FIX 7.1: Use provided PnL or calculate manually =====
        if pnl is None:
            # Calculate PnL manually (fallback for backward compatibility)
            position_size = self.position_size or 0
            if self.position_side == "LONG":
                pnl = (exit_price - self.entry_price) * position_size
            else:
                pnl = (self.entry_price - exit_price) * position_size
        
        # Update stats
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
        self.total_pnl += pnl
        
        # Reset position
        self.reset_position()
    
    def reset_position(self):
        """
        Reset position state without updating stats
        Used when position is closed externally or state needs to be cleared
        
        ===== FIX: Added this method to properly reset all position-related state =====
        """
        self.position_side = None
        self.position_size = None
        self.entry_price = None
        self.entry_time = None
        self.partial_tp_done = False
        self.tp_limit_order_id = None
    
    def get_unrealized_pnl(self, current_price: float) -> float:
        """
        Calculate unrealized PnL
        
        Args:
            current_price: Current market price
            
        Returns:
            Unrealized PnL in USDT
        """
        if not self.position_side or not self.entry_price or not self.position_size:
            return 0.0
        
        if current_price <= 0:
            return 0.0
        
        if self.position_side == "LONG":
            return (current_price - self.entry_price) * self.position_size
        else:
            return (self.entry_price - current_price) * self.position_size
    
    def get_unrealized_pnl_percent(self, current_price: float) -> float:
        """
        Calculate unrealized PnL as percentage
        
        Args:
            current_price: Current market price
            
        Returns:
            Unrealized PnL percentage
        """
        if not self.position_side or not self.entry_price:
            return 0.0
        
        if current_price <= 0 or self.entry_price <= 0:
            return 0.0
        
        if self.position_side == "LONG":
            return ((current_price - self.entry_price) / self.entry_price) * 100
        else:
            return ((self.entry_price - current_price) / self.entry_price) * 100
    
    def has_position(self) -> bool:
        """Check if there's an open position"""
        return self.position_side is not None and self.position_size is not None and self.position_size > 0
    
    def get_status_dict(self) -> dict:
        """Get current state as dictionary"""
        win_rate = self.winning_trades / self.total_trades if self.total_trades > 0 else 0
        
        return {
            "symbol": self.symbol,
            "trend_4h": self.trend_4h,
            "ema200_4h": round(self.ema200_4h, 2) if self.ema200_4h else None,
            "st_4h": self.st_4h_direction,
            "st_4h_value": round(self.st_4h_value, 2) if self.st_4h_value else None,
            "st_4h_prev": self.st_4h_prev_direction,
            "st_1h": self.st_1h_direction,
            "st_1h_value": round(self.st_1h_value, 2) if self.st_1h_value else None,
            "st_1h_prev": self.st_1h_prev_direction,
            "is_contrarian": self._is_contrarian(),
            "position": self.position_side,
            "position_size": self.position_size,
            "entry_price": round(self.entry_price, 2) if self.entry_price else None,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "partial_tp_done": self.partial_tp_done,
            "total_trades": self.total_trades,
            "win_rate": round(win_rate * 100, 1),
            "total_pnl": round(self.total_pnl, 2)
        }
    
    def _is_contrarian(self) -> bool:
        """Check if current 1H shows contrarian signal"""
        if not self.trend_4h or not self.st_1h_direction:
            return False
        
        return (
            (self.trend_4h == "BULLISH" and self.st_1h_direction == "red") or
            (self.trend_4h == "BEARISH" and self.st_1h_direction == "green")
        )
    
    def __repr__(self) -> str:
        """String representation for debugging"""
        pos_str = f"{self.position_side} @ {self.entry_price}" if self.position_side else "None"
        return f"SymbolState({self.symbol}, trend={self.trend_4h}, position={pos_str})"
