from typing import Optional, Literal

SignalType = Literal["LONG", "SHORT"]


class ContrarianEntry:
    """
    Contrarian Entry Strategy
    
    Enter when 1H shows OPPOSITE direction of 4H trend:
    - Trend BULLISH + ST 1H red = LONG (buy the dip)
    - Trend BEARISH + ST 1H green = SHORT (sell the rip)
    
    Exit when:
    - 4H SuperTrend flips direction (trend reversal)
    - OR current 4H SuperTrend is opposite to position (immediate exit)
    
    This catches pullbacks at the best price!
    """
    
    @staticmethod
    def check_entry_signal(
        trend_4h: str,
        st_1h_direction: str
    ) -> Optional[SignalType]:
        """
        Check if conditions are met for entry
        
        Args:
            trend_4h: Trend on 4H ("BULLISH", "BEARISH", "NEUTRAL")
            st_1h_direction: SuperTrend direction on 1H ("green" or "red")
        
        Returns:
            "LONG" if should enter long
            "SHORT" if should enter short
            None if no signal
        """
        if not trend_4h or not st_1h_direction:
            return None
        
        # LONG when 1H shows RED (opposite of bullish trend)
        # This means price is pulling back in an uptrend - buy the dip
        if trend_4h == "BULLISH" and st_1h_direction == "red":
            return "LONG"
        
        # SHORT when 1H shows GREEN (opposite of bearish trend)
        # This means price is bouncing in a downtrend - sell the rip
        elif trend_4h == "BEARISH" and st_1h_direction == "green":
            return "SHORT"
        
        return None
    
    @staticmethod
    def check_exit_signal(
        position_side: str,
        st_4h_direction: str,
        st_4h_prev_direction: str,
        trend_4h: str
    ) -> bool:
        """
        Check if should exit position
        
        Exit conditions:
        1. 4H SuperTrend flips (trend reversal detected by comparing prev vs current)
        2. Current 4H SuperTrend is opposite to position (immediate exit regardless of flip)
        
        Args:
            position_side: Current position ("LONG" or "SHORT")
            st_4h_direction: Current 4H ST direction ("green" or "red")
            st_4h_prev_direction: Previous 4H ST direction ("green" or "red")
            trend_4h: Current 4H trend ("BULLISH", "BEARISH", "NEUTRAL")
        
        Returns:
            True if should exit, False otherwise
        """
        if not position_side or not st_4h_direction:
            return False
        
        # ===== FIX: Check 1 - Immediate exit if ST is opposite to position =====
        # This catches cases where we missed the flip or position was opened manually
        if position_side == "LONG" and st_4h_direction == "red":
            return True
        
        if position_side == "SHORT" and st_4h_direction == "green":
            return True
        
        # ===== Check 2 - Traditional flip detection =====
        # Only check if we have previous direction
        if not st_4h_prev_direction:
            return False
        
        if position_side == "LONG":
            # Exit when 4H ST flips from green to red (trend reversal)
            flip_to_bearish = (st_4h_prev_direction == "green" and 
                              st_4h_direction == "red")
            return flip_to_bearish
        
        elif position_side == "SHORT":
            # Exit when 4H ST flips from red to green (trend reversal)
            flip_to_bullish = (st_4h_prev_direction == "red" and 
                              st_4h_direction == "green")
            return flip_to_bullish
        
        return False
    
    @staticmethod
    def should_place_tp(
        position_side: str,
        trend_4h: str,
        st_4h_direction: str
    ) -> bool:
        """
        Check if TP order should be placed
        
        TP is only placed when trend is still valid for position
        
        Args:
            position_side: Current position ("LONG" or "SHORT")
            trend_4h: Current 4H trend
            st_4h_direction: Current 4H ST direction
        
        Returns:
            True if TP should be placed, False otherwise
        """
        if not position_side:
            return False
        
        # For LONG: place TP if trend is still bullish
        if position_side == "LONG":
            return trend_4h == "BULLISH" and st_4h_direction == "green"
        
        # For SHORT: place TP if trend is still bearish
        if position_side == "SHORT":
            return trend_4h == "BEARISH" and st_4h_direction == "red"
        
        return False
    
    @staticmethod
    def get_signal_strength(
        trend_4h: str,
        st_1h_direction: str,
        ema200_4h: float,
        close_price: float
    ) -> int:
        """
        Calculate signal strength (1-3)
        
        Strong signal (3): Contrarian + price far from EMA
        Medium signal (2): Contrarian + price near EMA
        Weak signal (1): Contrarian but conflicting factors
        
        Args:
            trend_4h: 4H trend
            st_1h_direction: 1H SuperTrend direction
            ema200_4h: EMA 200 value on 4H
            close_price: Current close price
        
        Returns:
            Signal strength 1-3, or 0 if no signal
        """
        # Check if there's a basic signal first
        signal = ContrarianEntry.check_entry_signal(trend_4h, st_1h_direction)
        if not signal:
            return 0
        
        if not ema200_4h or ema200_4h <= 0:
            return 1
        
        # Calculate distance from EMA as percentage
        ema_distance_pct = abs((close_price - ema200_4h) / ema200_4h) * 100
        
        # For LONG signals (bullish trend):
        # - Strong: price well above EMA (>5%) - trend very established
        # - Medium: price near EMA (2-5%)
        # - Weak: price barely above EMA (<2%)
        
        # For SHORT signals (bearish trend):
        # - Strong: price well below EMA (>5%)
        # - Medium: price near EMA (2-5%)
        # - Weak: price barely below EMA (<2%)
        
        if ema_distance_pct >= 5:
            return 3
        elif ema_distance_pct >= 2:
            return 2
        else:
            return 1
