import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, List
from decimal import Decimal, ROUND_DOWN

from app.config import config
from app.strategy import TrendFilter, ContrarianEntry, SymbolState
from app.exchange import BybitClient, BybitWebSocket
from app.indicators import calculate_supertrend, calculate_ema

logger = logging.getLogger(__name__)


class BotController:
    """
    Main bot controller - manages trading logic and state
    
    This class implements a contrarian pullback trading strategy:
    - Entry: When 1H SuperTrend shows opposite direction to 4H trend
    - Exit: When 4H SuperTrend flips direction (trend reversal)
    - Partial TP: 50% at margin + fees target
    
    Internal position_side format: "LONG" or "SHORT"
    Bybit API format: "Buy" (for LONG) or "Sell" (for SHORT)
    
    Thread Safety:
    - Uses asyncio.Lock() for critical sections
    - All shared state modifications are protected
    - Lock acquisition order: _state_lock > _entry_lock > _price_lock > _file_lock > _kline_lock
    
    State Management:
    - Each symbol has its own SymbolState for tracking positions and indicators
    - Real-time prices cached from WebSocket
    - Historical data loaded from REST API on startup
    
    Error Handling:
    - Circuit breaker pattern for API failures
    - Retry logic with exponential backoff
    - Graceful degradation when WebSocket disconnects
    """
    
    @staticmethod
    def _bybit_side_to_internal(bybit_side: str) -> str:
        """Convert Bybit side (Buy/Sell) to internal side (LONG/SHORT)"""
        if bybit_side == "Buy":
            return "LONG"
        elif bybit_side == "Sell":
            return "SHORT"
        raise ValueError(f"Invalid Bybit side: {bybit_side}. Must be 'Buy' or 'Sell'")
    
    @staticmethod
    def _internal_side_to_bybit(internal_side: str) -> str:
        """Convert internal side (LONG/SHORT) to Bybit side (Buy/Sell)"""
        if internal_side == "LONG":
            return "Buy"
        elif internal_side == "SHORT":
            return "Sell"
        raise ValueError(f"Invalid internal side: {internal_side}. Must be 'LONG' or 'SHORT'")
    
    @staticmethod
    def _get_opposite_side(side: str) -> str:
        """
        Get opposite side (LONG -> SHORT, SHORT -> LONG)
        Centralized to avoid code duplication
        
        Args:
            side: "LONG" or "SHORT"
        
        Returns:
            Opposite side
        """
        if side == "LONG":
            return "SHORT"
        elif side == "SHORT":
            return "LONG"
        return side  # Fallback
    
    def _get_exit_side_bybit(self, position_side: str) -> str:
        """
        Get Bybit side for exit order (opposite of position side)
        Centralized to avoid code duplication
        
        Args:
            position_side: "LONG" or "SHORT"
        
        Returns:
            Bybit side ("Buy" or "Sell") for exit order
        """
        opposite_side = self._get_opposite_side(position_side)
        return self._internal_side_to_bybit(opposite_side)
    
    def _calculate_pnl(self, position_side: str, entry_price: float, exit_price: float, qty: float) -> float:
        """
        Calculate PnL for a position
        
        Centralized PnL calculation to avoid code duplication and ensure consistency.
        
        Args:
            position_side: "LONG" or "SHORT"
            entry_price: Entry price of the position
            exit_price: Exit price of the position
            qty: Quantity (size) of the position
        
        Returns:
            float: PnL in USDT (positive for profit, negative for loss)
        
        Raises:
            ValueError: If position_side is invalid or prices/qty are invalid
        """
        if not position_side or position_side not in ["LONG", "SHORT"]:
            raise ValueError(f"Invalid position_side: {position_side}")
        
        if not entry_price or entry_price <= 0:
            raise ValueError(f"Invalid entry_price: {entry_price}")
        
        if not exit_price or exit_price <= 0:
            raise ValueError(f"Invalid exit_price: {exit_price}")
        
        if not qty or qty <= 0:
            raise ValueError(f"Invalid qty: {qty}")
        
        if position_side == "LONG":
            return (exit_price - entry_price) * qty
        else:  # SHORT
            return (entry_price - exit_price) * qty
    
    def _parse_position_size_from_api(self, position: dict, symbol: str) -> Optional[float]:
        """
        Parse position size from API response with validation
        
        Centralized parsing to avoid code duplication and ensure consistent error handling.
        
        Args:
            position: Position dict from Bybit API
            symbol: Trading pair (for logging)
        
        Returns:
            float: Position size, or None if invalid
        """
        if not position:
            return None
        
        size_str = position.get('size', '0')
        if size_str is None:
            logger.warning(f"[{symbol}] Invalid position size from API (None)")
            return None
        
        try:
            size = float(size_str)
            if size < 0:
                logger.warning(f"[{symbol}] Invalid position size (negative): {size}")
                return None
            return size
        except (ValueError, TypeError) as e:
            logger.warning(f"[{symbol}] Error parsing position size: {e}")
            return None
    
    async def _clear_tp_order_id(self, state: SymbolState):
        """
        Clear TP order ID with proper locking
        
        Centralized method to ensure consistent state management.
        
        Args:
            state: SymbolState instance
        """
        async with self._state_lock:
            state.tp_limit_order_id = None
    
    def __init__(self):
        self.config = config
        self.client = BybitClient(config.bybit)
        self.websocket = BybitWebSocket(config.bybit.testnet)
        self.trend_filter = TrendFilter(config.indicators.ema_period_4h)
        self.contrarian = ContrarianEntry()
        
        # State tracking per symbol
        self.states: Dict[str, SymbolState] = {
            symbol: SymbolState(symbol=symbol)
            for symbol in config.trading.symbols
        }
        
        # Real-time price cache from WebSocket
        self.realtime_prices: Dict[str, float] = {}
        
        # Track if klines are initialized (historical data loaded)
        self.klines_initialized: Dict[str, bool] = {
            symbol: False for symbol in config.trading.symbols
        }
        
        # Account balance cache
        self.account_balance: Optional[float] = None
        self.total_equity: Optional[float] = None
        self.last_balance_update: Optional[datetime] = None
        self.last_equity_point_time: Optional[datetime] = None
        
        # Equity history persistence
        self.equity_history_file = Path("data/equity_history.json")
        self.equity_history: List[Dict] = []
        self._load_equity_history()
        
        # Trade history persistence
        self.trade_history_file = Path("data/trade_history.json")
        self.trade_history: List[Dict] = []
        self._load_trade_history()
        
        self.trading_enabled = config.trading_enabled
        self.running = False
        self.start_time: Optional[datetime] = None
        self.last_4h_update = {}
        self.ws_task: Optional[asyncio.Task] = None
        
        # ===== CRITICAL FIX 6: Track async tasks for proper shutdown =====
        self._background_tasks: List[asyncio.Task] = []
        # ===== FIX 9.1: Lock for _background_tasks to prevent race conditions =====
        self._background_tasks_lock = asyncio.Lock()
        
        # ===== FIX: Add locks for thread safety =====
        # ===== CRITICAL FIX 5: Lock acquisition order to prevent deadlocks =====
        # Order: _state_lock > _entry_lock > _price_lock > _file_lock > _kline_lock
        # Always acquire locks in this order when multiple locks are needed
        self._entry_lock = asyncio.Lock()  # Prevents race condition on balance check + entry
        self._state_lock = asyncio.Lock()  # Protects state modifications (highest priority)
        self._file_lock = asyncio.Lock()   # Protects file I/O operations
        self._price_lock = asyncio.Lock()  # Protects realtime_prices dict
        
        # ===== FIX: Track processed candles to prevent duplicate entries =====
        self._processed_candles: Dict[str, int] = {}  # symbol:interval -> last_timestamp
        # ===== FIX 4: Lock for _processed_candles access =====
        self._processed_candles_lock = asyncio.Lock()
        
        # ===== FIX: Circuit breaker for API failures =====
        self._api_failure_count = 0
        self._api_failure_threshold = 5
        self._circuit_breaker_until: Optional[datetime] = None
    
    def _create_background_task(self, coro):
        """
        Create and track a background task for proper shutdown
        
        This method ensures all background tasks are properly tracked and can be
        awaited during shutdown to prevent data loss.
        
        Args:
            coro: Coroutine to run as background task
        
        Returns:
            asyncio.Task: The created task, or None if creation failed
        
        Note:
            This method must be called from an async context (where event loop is running).
            The task is added to _background_tasks list immediately for tracking.
        """
        # ===== CRITICAL FIX: Create task and add to list atomically =====
        try:
            task = asyncio.create_task(coro)
            
            # Add task to list immediately (we're in async context, so this is safe)
            # Use a helper coroutine to add with lock protection
            async def add_task_to_list():
                async with self._background_tasks_lock:
                    self._background_tasks.append(task)
            
            # Schedule the add operation (safe because we're in async context)
            asyncio.create_task(add_task_to_list())
            
            # Set up removal callback (runs synchronously when task completes)
            def remove_task(t):
                # Callback runs synchronously - schedule async removal
                try:
                    async def remove_task_async():
                        async with self._background_tasks_lock:
                            if t in self._background_tasks:
                                self._background_tasks.remove(t)
                    
                    # Try to get running loop and schedule removal
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(remove_task_async())
                    except RuntimeError:
                        # No running loop - task will be cleaned up on shutdown
                        pass
                except Exception:
                    # Silently ignore - task will be cleaned up on shutdown
                    pass
            
            task.add_done_callback(remove_task)
            return task
            
        except RuntimeError as e:
            # Event loop might be closed or not running
            logger.warning(f"Could not create background task: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating background task: {e}")
            return None
    
    def _validate_instruments_info(self, info: Optional[Dict], symbol: str) -> bool:
        """
        Validate instruments info from API
        
        Centralized validation to avoid code duplication.
        
        Args:
            info: Instruments info dict from API (can be None)
            symbol: Trading pair for logging
        
        Returns:
            True if valid, False otherwise
        """
        if not info or not isinstance(info, dict):
            logger.error(f"[{symbol}] Invalid instruments info from API")
            return False
        return True
    
    def _validate_price(self, price: float, symbol: str = "") -> bool:
        """
        Validate that price is positive and reasonable
        
        Centralized validation to avoid code duplication.
        
        Args:
            price: Price to validate
            symbol: Optional symbol for logging
        
        Returns:
            True if price is valid, False otherwise
        """
        if price is None:
            if symbol:
                logger.warning(f"[{symbol}] Price is None")
            return False
        
        if price <= 0:
            if symbol:
                logger.warning(f"[{symbol}] Invalid price: {price} (must be positive)")
            return False
        
        # Sanity check: price should be reasonable (not too large)
        if price > 1000000:  # 1M USDT per unit is unreasonable for most assets
            if symbol:
                logger.warning(f"[{symbol}] Price seems unreasonably high: {price}")
            return False
        
        return True
    
    def _validate_position_size(self, position_size: Optional[float], symbol: str = "") -> bool:
        """
        Validate that position size is positive
        
        Centralized validation to avoid code duplication.
        
        Args:
            position_size: Position size to validate
            symbol: Optional symbol for logging
        
        Returns:
            True if position size is valid, False otherwise
        """
        if position_size is None or position_size <= 0:
            if symbol:
                logger.error(f"[{symbol}] Invalid position_size: {position_size}")
            return False
        return True
    
    def _calculate_size_ratio(self, actual_size: float, original_size: float) -> Optional[float]:
        """
        Calculate size ratio between actual and original position size
        
        Centralized calculation to avoid code duplication.
        
        Args:
            actual_size: Current position size
            original_size: Original position size
        
        Returns:
            float: Size ratio (actual/original), or None if invalid
        """
        if not original_size or original_size <= 0:
            return None
        if actual_size < 0:
            return None
        return actual_size / original_size
    
    def _calculate_partial_qty(self, position_size: float, percentage: float = 0.5) -> float:
        """
        Calculate partial quantity to close (default 50%)
        
        Centralized calculation to avoid code duplication.
        
        Args:
            position_size: Total position size
            percentage: Percentage to close (default 0.5 for 50%)
        
        Returns:
            float: Quantity to close
        """
        return position_size * percentage
    
    async def _get_execution_price_from_recent_executions(
        self, 
        symbol: str, 
        position_side: str, 
        time_window_seconds: int = 300
    ) -> Optional[float]:
        """
        Get execution price from recent executions
        
        Centralized method to avoid code duplication.
        
        Args:
            symbol: Trading pair
            position_side: Position side ("LONG" or "SHORT")
            time_window_seconds: Time window to search (default 300 = 5 minutes)
        
        Returns:
            float: Execution price if found, None otherwise
        """
        try:
            executions = await self.client.get_recent_executions(symbol, limit=10)
            if not executions:
                return None
            
            exit_side_bybit = self._get_exit_side_bybit(position_side)
            current_time = datetime.now().timestamp()
            
            for exec_item in executions:
                exec_side = exec_item.get("side", "")
                exec_time_str = exec_item.get("execTime", "0")
                
                try:
                    exec_time = int(exec_time_str) / 1000  # Convert to seconds
                    time_diff = current_time - exec_time
                    
                    if time_diff < time_window_seconds and exec_side == exit_side_bybit:
                        exec_price_str = exec_item.get("execPrice", "0")
                        try:
                            exec_price = float(exec_price_str)
                            if exec_price > 0:
                                logger.info(f"[{symbol}] Found execution price from recent executions: {exec_price}")
                                return exec_price
                        except (ValueError, TypeError):
                            continue
                except (ValueError, TypeError):
                    continue
        except Exception as e:
            logger.debug(f"[{symbol}] Error getting execution price from recent executions: {e}")
        
        return None
    
    def _calculate_required_margin(self, buffer_multiplier: float = 1.5) -> float:
        """
        Calculate required margin with buffer for order placement
        
        Args:
            buffer_multiplier: Multiplier for margin buffer (default 1.5 for volatile markets)
        
        Returns:
            float: Required margin in USDT
        """
        base_margin = self.config.trading.position_size_usdt / self.config.trading.leverage
        return base_margin * buffer_multiplier
    
    def _calculate_tp_target_profit(self, partial_percentage: float = 0.5) -> float:
        """
        Calculate target profit for partial TP (full margin + fees)
        
        When closing 50% of position, profit should equal the ENTIRE margin used to enter
        the position, not just the margin for 50% of position.
        
        This ensures that when partial TP executes, you recover the entire sum you entered with,
        plus trading fees.
        
        Formula:
            margin_total = position_size_usdt / leverage (entire margin used to enter)
            fees = (position_size_usdt * partial_percentage) * 0.002
            target = margin_total + fees
        
        Args:
            partial_percentage: Percentage of position to close (default 0.5 for 50%)
        
        Returns:
            float: Target profit in USDT (entire margin + fees)
        
        Example:
            If position_size_usdt = 100, leverage = 20, partial_percentage = 0.5:
            - margin_total = 100 / 20 = 5 USDT (entire margin used to enter)
            - fees = 50 * 0.002 = 0.1 USDT (fees on 50% of position)
            - target = 5 + 0.1 = 5.1 USDT
        """
        # Calculate TOTAL margin used to enter the position (entire sum you entered with)
        margin_total = self.config.trading.position_size_usdt / self.config.trading.leverage
        
        # Fees are calculated on the partial position value being closed
        partial_position_size = self.config.trading.position_size_usdt * partial_percentage
        fees = partial_position_size * 0.002  # 0.2% total (entry + exit)
        
        # Target profit = entire margin you entered with + fees
        return margin_total + fees
    
    def _calculate_tp_target_price(
        self,
        position_side: str,
        entry_price: float,
        target_profit: float,
        qty_partial: float
    ) -> Optional[float]:
        """
        Calculate TP target price based on position side and target profit
        Centralized to avoid code duplication
        
        Args:
            position_side: "LONG" or "SHORT"
            entry_price: Entry price of the position
            target_profit: Target profit in USDT
            qty_partial: Quantity to close (partial TP)
        
        Returns:
            Target price for TP order, or None if invalid
        """
        if not position_side or not entry_price or not target_profit or not qty_partial:
            return None
        
        if entry_price <= 0 or target_profit <= 0 or qty_partial <= 0:
            return None
        
        if position_side == "LONG":
            target_price = entry_price + (target_profit / qty_partial)
        elif position_side == "SHORT":
            target_price = entry_price - (target_profit / qty_partial)
        else:
            return None
        
        if target_price <= 0:
            return None
        
        # ===== FIX 8.1: Overflow protection - ensure target_price is within reasonable range =====
        # Target price should not exceed ±50% of entry price (sanity check)
        max_price_change_pct = 0.5  # 50%
        if position_side == "LONG":
            max_target_price = entry_price * (1 + max_price_change_pct)
            if target_price > max_target_price:
                logger.warning(f"Target price {target_price:.2f} exceeds maximum {max_target_price:.2f} (50% above entry)")
                return None
        elif position_side == "SHORT":
            min_target_price = entry_price * (1 - max_price_change_pct)
            if target_price < min_target_price:
                logger.warning(f"Target price {target_price:.2f} below minimum {min_target_price:.2f} (50% below entry)")
                return None
        
        # ===== FIX 8.1: Check if target_price is too close to entry_price =====
        # If target_price is within 0.1% of entry_price, it may cause issues with tick size
        price_diff_pct = abs((target_price - entry_price) / entry_price) * 100
        min_price_diff_pct = 0.1  # 0.1% minimum difference
        if price_diff_pct < min_price_diff_pct:
            logger.warning(f"Target price {target_price:.2f} is too close to entry price {entry_price:.2f} (diff: {price_diff_pct:.4f}%)")
            return None
        
        return target_price
    
    def _is_circuit_breaker_active(self) -> bool:
        """Check if circuit breaker is active"""
        if self._circuit_breaker_until is None:
            return False
        if datetime.now() >= self._circuit_breaker_until:
            self._circuit_breaker_until = None
            self._api_failure_count = 0
            logger.info("Circuit breaker reset - resuming trading")
            return False
        return True
    
    def _record_api_failure(self):
        """Record API failure and potentially activate circuit breaker"""
        self._api_failure_count += 1
        if self._api_failure_count >= self._api_failure_threshold:
            self._circuit_breaker_until = datetime.now() + timedelta(minutes=5)
            logger.warning(f"Circuit breaker activated - pausing trading for 5 minutes after {self._api_failure_count} failures")
    
    def _record_api_success(self):
        """Record API success and reset failure count"""
        self._api_failure_count = 0
    
    async def start(self):
        """Start the trading bot"""
        logger.info("=" * 60)
        logger.info("🚀 CONTRARIAN PULLBACK BOT STARTING")
        logger.info("=" * 60)
        
        # Validate configuration
        try:
            self.config.validate()
        except ValueError as e:
            logger.error(f"❌ Configuration error: {e}")
            raise
        
        logger.info(f"Strategy: Buy dips in uptrend, sell rips in downtrend")
        logger.info(f"Symbols: {', '.join(self.config.trading.symbols)}")
        logger.info(f"Position Size: {self.config.trading.position_size_usdt} USDT")
        logger.info(f"Leverage: {self.config.trading.leverage}x {self.config.trading.margin_mode}")
        logger.info("=" * 60)
        
        self.running = True
        self.start_time = datetime.now()
        
        # Setup leverage with timeout
        try:
            await asyncio.wait_for(self._setup_leverage(), timeout=60)
        except asyncio.TimeoutError:
            logger.error("Timeout setting up leverage - continuing anyway")
        
        # Sync existing positions with timeout
        try:
            await asyncio.wait_for(self._sync_positions(), timeout=60)
        except asyncio.TimeoutError:
            logger.error("Timeout syncing positions - continuing anyway")
        
        # Get initial account balance
        await self._update_account_balance()
        
        # Initialize klines with historical data
        await self._initialize_klines()
        
        # Start WebSocket for real-time prices and klines
        await self._start_websocket()
        
        # Start trading loop
        await self.trading_loop()
    
    async def stop(self):
        """Stop the trading bot gracefully"""
        logger.info("🛑 Stopping bot...")
        self.running = False
        
        # Give trading loop time to finish current iteration
        await asyncio.sleep(2)
        
        # ===== CRITICAL FIX 6: Await all background tasks before shutdown =====
        # ===== FIX 9.1: Use lock when accessing _background_tasks =====
        async with self._background_tasks_lock:
            tasks_to_wait = list(self._background_tasks)
        
        if tasks_to_wait:
            logger.info(f"Waiting for {len(tasks_to_wait)} background tasks to complete...")
            # Cancel all background tasks
            for task in tasks_to_wait:
                if not task.done():
                    task.cancel()
            
            # Wait for all tasks with timeout
            if tasks_to_wait:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks_to_wait, return_exceptions=True),
                        timeout=5.0
                    )
                except asyncio.TimeoutError:
                    logger.warning("Some background tasks did not complete in time")
                except Exception as e:
                    logger.warning(f"Error waiting for background tasks: {e}")
        
        # Stop WebSocket
        if self.ws_task:
            self.ws_task.cancel()
            try:
                await self.ws_task
            except asyncio.CancelledError:
                pass
        
        await self.websocket.disconnect()
        
        # Save final state
        async with self._file_lock:
            self._save_equity_history()
            self._save_trade_history()
        
        logger.info("✓ Bot stopped gracefully")
    
    async def _setup_leverage(self):
        """Set leverage and margin mode for all symbols"""
        logger.info("Setting up leverage and margin mode...")
        
        for symbol in self.config.trading.symbols:
            try:
                await self.client.set_leverage(
                    symbol=symbol,
                    leverage=self.config.trading.leverage
                )
                await self.client.set_margin_mode(
                    symbol=symbol,
                    margin_mode=self.config.trading.margin_mode
                )
                logger.info(f"✓ {symbol}: {self.config.trading.leverage}x {self.config.trading.margin_mode}")
                self._record_api_success()
            except Exception as e:
                logger.warning(f"Setup {symbol}: {e}")
                self._record_api_failure()
    
    async def _sync_positions(self):
        """Sync existing positions from Bybit"""
        logger.info("Syncing existing positions...")
        
        for symbol in self.config.trading.symbols:
            try:
                position = await self.client.get_position(symbol)
                
                if position:
                    size = float(position.get('size', 0))
                    if size > 0:
                        raw_side = position.get('side')
                        side = self._bybit_side_to_internal(raw_side)
                        entry_price = float(position.get('avgPrice', 0))
                        
                        # Update state with normalized side (LONG/SHORT)
                        async with self._state_lock:
                            state = self.states[symbol]
                            state.open_position(side, size, entry_price)
                        
                        logger.info(f"✓ {symbol}: Found existing {side} position @ {entry_price}")
                        
                        # Check for existing TP order
                        open_orders = await self.client.get_open_orders(symbol)
                        tp_orders = [
                            o for o in open_orders 
                            if o.get("reduceOnly") in [True, "true", "True"] and 
                               o.get("orderType") == "Limit" and
                               o.get("orderStatus") in ["New", "PartiallyFilled"]
                        ]
                        if tp_orders:
                            # ===== FIX: Set tp_limit_order_id with lock for thread safety =====
                            async with self._state_lock:
                                state.tp_limit_order_id = tp_orders[0].get("orderId")
                            logger.info(f"[{symbol}] Found existing TP limit order: {state.tp_limit_order_id}")
                        else:
                            # Place TP order for existing position
                            await asyncio.sleep(0.5)
                            await self._place_partial_tp_limit_order(symbol, state)
                
                self._record_api_success()
            
            except Exception as e:
                logger.error(f"Error syncing position for {symbol}: {e}")
                self._record_api_failure()
    
    async def _start_websocket(self):
        """Start WebSocket connection for real-time price and kline updates"""
        logger.info("Starting WebSocket for real-time prices and klines...")
        
        connected = await self.websocket.connect()
        
        if not connected:
            logger.warning("Failed to connect WebSocket, will use REST API only")
            return
        
        # Subscribe to tickers for all symbols
        for symbol in self.config.trading.symbols:
            await self.websocket.subscribe_ticker(symbol, self._handle_price_update)
        
        # Subscribe to klines for all symbols (1H and 4H)
        async def kline_callback(symbol, interval, candle, is_confirmed):
            await self._handle_kline_update(symbol, interval, is_confirmed, candle)
        
        for symbol in self.config.trading.symbols:
            await self.websocket.subscribe_kline(symbol, "60", kline_callback)
            await self.websocket.subscribe_kline(symbol, "240", kline_callback)
        
        # Start listening task
        self.ws_task = asyncio.create_task(self.websocket.listen())
        
        logger.info(f"✓ WebSocket active for {len(self.config.trading.symbols)} symbols")
        logger.info(f"✓ Subscribed to tickers and klines (1H, 4H)")
    
    async def _initialize_klines(self):
        """Initialize kline data with historical candles from REST API (one-time)"""
        logger.info("Initializing kline data from REST API...")
        
        for symbol in self.config.trading.symbols:
            try:
                # Fetch historical 1H candles
                candles_1h = await self.client.get_klines(
                    symbol=symbol,
                    interval="60",
                    limit=100
                )
                
                if candles_1h:
                    candles_1h_chronological = candles_1h[::-1]
                    key_1h = f"{symbol}:60"
                    # ===== CRITICAL FIX 3: Protect kline_data access with lock =====
                    async with self.websocket._kline_lock:
                        if key_1h not in self.websocket.kline_data:
                            self.websocket.kline_data[key_1h] = deque(maxlen=500)
                        self.websocket.kline_data[key_1h].extend(candles_1h_chronological)
                    logger.debug(f"✓ {symbol} 1H: Loaded {len(candles_1h)} historical candles")
                
                # Fetch historical 4H candles
                candles_4h = await self.client.get_klines(
                    symbol=symbol,
                    interval="240",
                    limit=self.config.indicators.ema_period_4h + 50
                )
                
                if candles_4h:
                    candles_4h_chronological = candles_4h[::-1]
                    key_4h = f"{symbol}:240"
                    # ===== CRITICAL FIX 3: Protect kline_data access with lock =====
                    async with self.websocket._kline_lock:
                        if key_4h not in self.websocket.kline_data:
                            self.websocket.kline_data[key_4h] = deque(maxlen=500)
                        self.websocket.kline_data[key_4h].extend(candles_4h_chronological)
                    logger.debug(f"✓ {symbol} 4H: Loaded {len(candles_4h)} historical candles")
                
                self.klines_initialized[symbol] = True
                self._record_api_success()
                
            except Exception as e:
                logger.error(f"Error initializing klines for {symbol}: {e}")
                self._record_api_failure()
        
        logger.info("✓ Kline initialization complete")
    
    async def _handle_kline_update(self, symbol: str, interval: str, is_confirmed: bool = False, candle: list = None):
        """
        Handle kline update when a new candle closes or updates
        
        This method processes candle updates from WebSocket:
        - 4H candles: Update 4H trend and check for exits
        - 1H candles: Update 1H signal and check for entries (only on confirmed)
        
        Important:
        - Entry signals are ONLY processed on confirmed (closed) 1H candles
        - This prevents entries on live/unconfirmed candles
        - 4H trend updates trigger exit checks immediately
        
        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            interval: Interval in minutes ("60" for 1H, "240" for 4H)
            is_confirmed: Whether the candle is confirmed (closed). Only True when candle is fully closed.
            candle: Candle data [timestamp, open, high, low, close, volume, turnover]
        
        Note:
            Duplicate candle processing is prevented by tracking processed timestamps
        """
        try:
            state = self.states.get(symbol)
            if not state:
                return
            
            # ===== FIX: Prevent duplicate processing of same candle =====
            if is_confirmed and candle:
                # ===== MEDIUM FIX 3 & 4: Add validation for candle data =====
                if not isinstance(candle, (list, tuple)) or len(candle) < 1:
                    logger.warning(f"[{symbol}] Invalid candle format in kline update")
                    return
                
                candle_key = f"{symbol}:{interval}"
                try:
                    candle_timestamp = int(candle[0]) if candle[0] is not None else 0
                except (ValueError, TypeError, IndexError) as e:
                    logger.warning(f"[{symbol}] Error parsing candle timestamp: {e}")
                    return
                
                # ===== FIX 4: Protect _processed_candles access with lock =====
                async with self._processed_candles_lock:
                    if candle_key in self._processed_candles:
                        if self._processed_candles[candle_key] >= candle_timestamp:
                            logger.debug(f"[{symbol}] Skipping already processed {interval} candle: {candle_timestamp}")
                            return
                    
                    self._processed_candles[candle_key] = candle_timestamp
            
            # New 4H candle closed - update 4H trend
            if interval == "240" and is_confirmed:
                await self._update_4h_trend(symbol, state)
                self.last_4h_update[symbol] = datetime.now()
                logger.info(f"[{symbol}] 📊 4H candle closed - trend updated")
                
                # Check for partial TP and exit immediately after 4H trend update
                if state.position_side:
                    if not state.partial_tp_done:
                        await self._check_partial_tp(symbol, state)
                    await self._check_exit(symbol, state)
            
            # 1H candle update - update 1H signal
            elif interval == "60":
                # ===== FIX 4.1: Use WebSocket data if available, REST API only as fallback =====
                if is_confirmed:
                    # For confirmed candles, try WebSocket first (faster), then REST API if needed
                    has_ws_data = await self.websocket.has_klines(symbol, "60")
                    if has_ws_data:
                        # Use WebSocket data for confirmed candles (already confirmed by WebSocket)
                        await self._update_1h_signal(symbol, state)
                    else:
                        # Fallback to REST API if WebSocket doesn't have data
                        await self._update_1h_signal_from_rest(symbol, state)
                else:
                    # Update from WebSocket for real-time display only
                    await self._update_1h_signal(symbol, state)
                
                # ===== CRITICAL FIX 4: Check for entry ONLY on confirmed (closed) 1H candle =====
                if is_confirmed and not state.position_side and self.trading_enabled:
                    # Double-check that we have confirmed data before entry
                    # If WebSocket update failed, try REST API as fallback
                    if not state.st_1h_direction:
                        await self._update_1h_signal_from_rest(symbol, state)
                    await self._check_entry(symbol, state)
        
        except Exception as e:
            logger.error(f"Error handling kline update for {symbol} {interval}: {e}")
    
    async def _handle_price_update(self, symbol: str, price: float):
        """Handle real-time price update from WebSocket"""
        # Update price cache with lock
        async with self._price_lock:
            self.realtime_prices[symbol] = price
        
        state = self.states.get(symbol)
        if not state:
            return
        
        # If in position, check for exit on significant price move
        if state.position_side and state.entry_price:
            price_change_pct = abs(price - state.entry_price) / state.entry_price * 100
            
            if price_change_pct > 0.5:  # Increased threshold to reduce API calls
                await self._check_exit(symbol, state)
    
    def _get_current_price(self, symbol: str) -> Optional[float]:
        """
        Get current price from WebSocket cache
        
        Note: This is a synchronous method for quick reads.
        For thread-safe access, use _get_current_price_with_fallback which uses locks.
        """
        # ===== FIX 5: Quick read without lock (acceptable for read-only access) =====
        # For writes, _handle_price_update uses _price_lock
        return self.realtime_prices.get(symbol)
    
    async def _get_current_price_with_fallback(self, symbol: str) -> Optional[float]:
        """Get current price from WebSocket cache with REST API fallback"""
        # Try WebSocket cache first (real-time)
        price = self.realtime_prices.get(symbol)
        if price:
            return price
        
        # ===== CRITICAL FIX 8: Fallback to REST API with proper error handling =====
        # ===== FIX 6.1: Add retry logic for REST API fallback =====
        max_retries = 2
        for attempt in range(max_retries):
            try:
                ticker = await self.client.get_ticker(symbol)
                # ===== FIX 1.1: More robust validation for get_ticker =====
                if ticker and isinstance(ticker, dict) and ticker.get('lastPrice'):
                    try:
                        price = float(ticker['lastPrice'])
                        if price > 0:
                            # Update cache for next time
                            async with self._price_lock:
                                self.realtime_prices[symbol] = price
                            self._record_api_success()
                            return price
                        else:
                            logger.warning(f"[{symbol}] Invalid price from API: {price}")
                    except (ValueError, TypeError) as e:
                        logger.warning(f"[{symbol}] Error parsing price from API: {e}")
                
                # If we got here, ticker was None or invalid, retry if not last attempt
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5)  # Brief delay before retry
                    continue
                    
            except Exception as e:
                logger.debug(f"Error getting ticker price for {symbol} (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5)  # Brief delay before retry
                    continue
                else:
                    self._record_api_failure()
        
        # ===== CRITICAL FIX 8: Final fallback - use last known price if available =====
        async with self._price_lock:
            cached_price = self.realtime_prices.get(symbol)
            if cached_price and cached_price > 0:
                logger.debug(f"[{symbol}] Using cached price as fallback: {cached_price}")
                return cached_price
        
        return None
    
    async def trading_loop(self):
        """Main trading loop"""
        logger.info(f"Trading loop started (checking every {self.config.check_interval_seconds}s)")
        
        balance_update_counter = 0
        
        while self.running:
            try:
                # Check circuit breaker
                if self._is_circuit_breaker_active():
                    logger.debug("Circuit breaker active - skipping iteration")
                    await asyncio.sleep(self.config.check_interval_seconds)
                    continue
                
                # Update balance every 10 iterations
                balance_update_counter += 1
                if balance_update_counter >= 10:
                    await self._update_account_balance()
                    balance_update_counter = 0
                
                # ===== FIX: Process symbols sequentially to avoid race conditions =====
                # This is safer than parallel processing for balance checks
                for symbol in self.config.trading.symbols:
                    if not self.running:
                        break
                    await self._process_symbol(symbol)
                
                await asyncio.sleep(self.config.check_interval_seconds)
            
            except asyncio.CancelledError:
                logger.info("Trading loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in trading loop: {e}", exc_info=True)
                await asyncio.sleep(60)
    
    async def _process_symbol(self, symbol: str):
        """Process one symbol"""
        try:
            state = self.states[symbol]
            
            # 0. Sync position from Bybit (verify state matches reality)
            await self._verify_position(symbol, state)
            
            # 1. Update 4H trend periodically
            should_update_4h = (
                symbol not in self.last_4h_update or
                datetime.now() - self.last_4h_update.get(symbol, datetime.min) > 
                timedelta(hours=self.config.update_4h_interval_hours)
            )
            
            if should_update_4h:
                await self._update_4h_trend(symbol, state)
                self.last_4h_update[symbol] = datetime.now()
            
            # 2. Update 1H signal (for display only - entry uses confirmed candles)
            await self._update_1h_signal(symbol, state)
            
            # ===== IMPORTANT FIX 1: Do NOT check entry in trading loop =====
            # Entry should ONLY happen on confirmed 1H candle close (via WebSocket callback)
            # This prevents entries on unconfirmed/live candles
            
            # 3. Check for partial TP
            if state.position_side and not state.partial_tp_done:
                await self._check_partial_tp(symbol, state)
            
            # 4. Check for exit
            if state.position_side:
                await self._check_exit(symbol, state)
        
        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")
    
    def _calculate_current_equity(self) -> Optional[float]:
        """
        Get current equity - calculate in real-time if positions are open
        
        This ensures equity updates live on the dashboard even when total_equity
        from Bybit is only updated every 50 minutes. When positions are open,
        we calculate equity using current prices for real-time updates.
        """
        # Check if we have open positions
        has_open_positions = any(
            state.position_side and state.position_size 
            for state in self.states.values()
        )
        
        if has_open_positions:
            # Calculate equity in real-time using current prices
            # This ensures dashboard shows live equity updates
            if self.account_balance is None:
                return None
            
            # Calculate total unrealized PnL from all open positions
            total_unrealized_pnl = 0.0
            for symbol, state in self.states.items():
                if state.position_side and state.entry_price and state.position_size:
                    current_price = self.realtime_prices.get(symbol)
                    if current_price:
                        unrealized_pnl = state.get_unrealized_pnl(current_price)
                        total_unrealized_pnl += unrealized_pnl
            
            return self.account_balance + total_unrealized_pnl
        
        # No open positions - use cached total_equity from Bybit (most accurate)
        # This is updated every 50 minutes via _update_account_balance()
        if self.total_equity is not None:
            return self.total_equity
        
        if self.account_balance is None:
            return None
        
        return self.account_balance
    
    async def _update_account_balance(self):
        """Update account balance and total equity from Bybit"""
        try:
            balance = await self.client.get_wallet_balance()
            if balance is not None:
                self.account_balance = balance
                self.last_balance_update = datetime.now()
                self._record_api_success()
            
            total_equity = await self.client.get_total_equity()
            if total_equity is not None:
                self.total_equity = total_equity
                self._add_equity_point(total_equity, force_add=False)
                self._record_api_success()
        except Exception as e:
            logger.debug(f"Error updating account balance: {e}")
            self._record_api_failure()
    
    def _load_equity_history(self):
        """Load equity history from file"""
        try:
            if self.equity_history_file.exists():
                with open(self.equity_history_file, 'r') as f:
                    data = json.load(f)
                    self.equity_history = data.get('history', [])
                    logger.info(f"✓ Loaded {len(self.equity_history)} equity data points from history")
        except json.JSONDecodeError as e:
            logger.error(f"Corrupted equity history file: {e}")
            # Backup corrupted file
            backup_path = self.equity_history_file.with_suffix('.json.bak')
            try:
                self.equity_history_file.rename(backup_path)
                logger.info(f"Backed up corrupted file to {backup_path}")
            except Exception:
                pass
            self.equity_history = []
        except Exception as e:
            logger.error(f"Error loading equity history: {e}")
            self.equity_history = []
    
    def _save_equity_history(self):
        """Save equity history to file (call with _file_lock held)"""
        try:
            self.equity_history_file.parent.mkdir(parents=True, exist_ok=True)
            
            data = {
                'last_update': datetime.now().isoformat(),
                'history': self.equity_history
            }
            
            # Write to temp file first, then rename (atomic operation)
            temp_file = self.equity_history_file.with_suffix('.json.tmp')
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)
            temp_file.replace(self.equity_history_file)
        except Exception as e:
            logger.error(f"Error saving equity history: {e}")
    
    def _add_equity_point(self, equity: float, force_add: bool = False):
        """Add new equity point to history"""
        if equity <= 0:
            return
        
        timestamp = datetime.now()
        point = {
            'timestamp': timestamp.isoformat(),
            'time': timestamp.strftime('%H:%M:%S'),
            'value': round(equity, 4)
        }
        
        if force_add:
            self.equity_history.append(point)
            self.last_equity_point_time = timestamp
            if len(self.equity_history) > 1000:
                self.equity_history = self.equity_history[-1000:]
            # ===== CRITICAL FIX 6: Use tracked background task =====
            # We're called from async context, so create_task is safe
            self._create_background_task(self._save_equity_history_async())
            return
        
        should_add = False
        if not self.equity_history:
            should_add = True
        else:
            last_value = self.equity_history[-1]['value']
            last_timestamp = datetime.fromisoformat(self.equity_history[-1]['timestamp'])
            time_diff = (timestamp - last_timestamp).total_seconds()
            
            value_change_pct = abs((point['value'] - last_value) / last_value * 100) if last_value > 0 else 0
            if value_change_pct >= 1.0:
                should_add = True
            elif time_diff >= 3600:
                should_add = True
        
        if should_add:
            self.equity_history.append(point)
            self.last_equity_point_time = timestamp
            
            if len(self.equity_history) > 1000:
                self.equity_history = self.equity_history[-1000:]
            
            if len(self.equity_history) % 10 == 0:
                # ===== CRITICAL FIX 6: Use tracked background task =====
                # We're called from async context, so create_task is safe
                self._create_background_task(self._save_equity_history_async())
    
    async def _save_equity_history_async(self):
        """Save equity history asynchronously with lock"""
        async with self._file_lock:
            self._save_equity_history()
    
    def _load_trade_history(self):
        """Load trade history from file"""
        try:
            if self.trade_history_file.exists():
                with open(self.trade_history_file, 'r') as f:
                    data = json.load(f)
                    self.trade_history = data.get('trades', [])
                    logger.info(f"✓ Loaded {len(self.trade_history)} trades from history")
        except json.JSONDecodeError as e:
            logger.error(f"Corrupted trade history file: {e}")
            backup_path = self.trade_history_file.with_suffix('.json.bak')
            try:
                self.trade_history_file.rename(backup_path)
            except Exception:
                pass
            self.trade_history = []
        except Exception as e:
            logger.error(f"Error loading trade history: {e}")
            self.trade_history = []
    
    def _save_trade_history(self):
        """Save trade history to file (call with _file_lock held)"""
        try:
            self.trade_history_file.parent.mkdir(parents=True, exist_ok=True)
            
            data = {
                'last_update': datetime.now().isoformat(),
                'trades': self.trade_history
            }
            
            temp_file = self.trade_history_file.with_suffix('.json.tmp')
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)
            temp_file.replace(self.trade_history_file)
        except Exception as e:
            logger.error(f"Error saving trade history: {e}")
    
    async def _save_trade_history_async(self):
        """Save trade history asynchronously with lock"""
        async with self._file_lock:
            self._save_trade_history()
    
    def _add_trade(self, symbol: str, side: str, entry_price: float, exit_price: float, 
                   size: float, pnl: float, entry_time: Optional[datetime] = None, is_partial: bool = False):
        """Add completed trade to history"""
        timestamp = datetime.now()
        
        # ===== FIX: Safe division to prevent division by zero =====
        position_value = entry_price * size if entry_price and size else 0
        pnl_percent = round((pnl / position_value) * 100, 2) if position_value > 0 else 0
        
        trade = {
            'id': f"{symbol}_{timestamp.timestamp()}_{len(self.trade_history)}",
            'symbol': symbol,
            'side': side,
            'entry_price': round(entry_price, 2) if entry_price else 0,
            'exit_price': round(exit_price, 2) if exit_price else 0,
            'size': round(size, 6) if size else 0,
            'pnl': round(pnl, 2),
            'pnl_percent': pnl_percent,
            'entry_time': entry_time.isoformat() if entry_time else timestamp.isoformat(),
            'exit_time': timestamp.isoformat(),
            'timestamp': timestamp.isoformat(),
            'is_partial': is_partial
        }
        
        self.trade_history.append(trade)
        
        if len(self.trade_history) > 1000:
            self.trade_history = self.trade_history[-1000:]
        
        # ===== CRITICAL FIX 6: Use tracked background task =====
        # We're called from async context, so create_task is safe
        self._create_background_task(self._save_trade_history_async())
    
    async def _verify_position(self, symbol: str, state: SymbolState):
        """Verify position state matches Bybit reality"""
        try:
            position = await self.client.get_position(symbol)
            
            # ===== CRITICAL FIX 2: Consistent lock ordering - state_lock before entry_lock =====
            async with self._state_lock:
                if position:
                    # ===== CRITICAL FIX 7: Add null/type validation =====
                    size_str = position.get('size', '0')
                    raw_side = position.get('side')
                    avg_price_str = position.get('avgPrice', '0')
                    
                    if not size_str or not raw_side or not avg_price_str:
                        logger.warning(f"[{symbol}] Invalid position data from API")
                        self._record_api_failure()
                        return
                    
                    try:
                        actual_size = float(size_str)
                        actual_entry_price = float(avg_price_str)
                    except (ValueError, TypeError) as e:
                        logger.warning(f"[{symbol}] Invalid position data format: {e}")
                        self._record_api_failure()
                        return
                    
                    actual_side = self._bybit_side_to_internal(raw_side)
                    if actual_side not in ["LONG", "SHORT"]:
                        logger.warning(f"[{symbol}] Invalid side from API: {raw_side}")
                        self._record_api_failure()
                        return
                    
                    if actual_size > 0:
                        if not state.position_side:
                            logger.warning(f"[{symbol}] Position found on Bybit but not in state - syncing")
                            state.open_position(actual_side, actual_size, actual_entry_price)
                        elif state.position_side != actual_side or abs((state.position_size or 0) - actual_size) > 0.0001:
                            logger.warning(f"[{symbol}] Position mismatch - updating state from Bybit")
                            # Note: Manual partial close detection is handled in _check_partial_tp
                            state.open_position(actual_side, actual_size, actual_entry_price)
                    else:
                        # Position closed on Bybit
                        if state.position_side:
                            logger.warning(f"[{symbol}] Position in state but not on Bybit - clearing state")
                            # ===== FIX 10: Reset position including partial_tp_done =====
                            state.reset_position()
                else:
                    if state.position_side:
                        logger.warning(f"[{symbol}] Position in state but not on Bybit - clearing state")
                        # ===== FIX 10: Reset position including partial_tp_done =====
                        state.reset_position()
            
            self._record_api_success()
        
        except Exception as e:
            logger.debug(f"Error verifying position for {symbol}: {e}")
            self._record_api_failure()
    
    async def _update_4h_trend(self, symbol: str, state: SymbolState):
        """Update 4H trend filter using REST API (confirmed candles only)"""
        try:
            candles_4h = await self.client.get_klines(
                symbol=symbol,
                interval="240",
                limit=self.config.indicators.ema_period_4h + 50
            )
            
            if not candles_4h or len(candles_4h) < self.config.indicators.ema_period_4h:
                logger.warning(f"{symbol}: Not enough 4H candles available ({len(candles_4h) if candles_4h else 0})")
                return
            
            # ===== CRITICAL FIX 7: Validate candle data format =====
            most_recent_candle = candles_4h[0]
            if not isinstance(most_recent_candle, (list, tuple)) or len(most_recent_candle) < 5:
                logger.error(f"{symbol}: Invalid candle format from API")
                self._record_api_failure()
                return
            
            try:
                close = float(most_recent_candle[4])
                if close <= 0:
                    logger.error(f"{symbol}: Invalid close price: {close}")
                    self._record_api_failure()
                    return
            except (ValueError, TypeError, IndexError) as e:
                logger.error(f"{symbol}: Error parsing candle data: {e}")
                self._record_api_failure()
                return
            
            # ===== IMPORTANT FIX 5: Validate sufficient data before indicator calculations =====
            min_candles_for_ema = self.config.indicators.ema_period_4h
            min_candles_for_st = self.config.indicators.st_period_4h + 1
            
            if len(candles_4h) < min_candles_for_ema:
                logger.warning(f"{symbol}: Not enough candles for EMA200 ({len(candles_4h)} < {min_candles_for_ema})")
                self._record_api_failure()
                return
            
            if len(candles_4h) < min_candles_for_st:
                logger.warning(f"{symbol}: Not enough candles for SuperTrend ({len(candles_4h)} < {min_candles_for_st})")
                self._record_api_failure()
                return
            
            try:
                ema200 = calculate_ema(candles_4h, self.config.indicators.ema_period_4h)
            except (ValueError, Exception) as e:
                logger.error(f"{symbol}: Error calculating EMA200: {e}")
                self._record_api_failure()
                return
            
            try:
                st_dir, st_val = calculate_supertrend(
                    candles_4h,
                    self.config.indicators.st_period_4h,
                    self.config.indicators.st_multiplier_4h
                )
            except (ValueError, Exception) as e:
                logger.error(f"{symbol}: Error calculating SuperTrend: {e}")
                self._record_api_failure()
                return
            
            trend = self.trend_filter.detect_trend(close, ema200, st_dir)
            
            async with self._state_lock:
                state.update_trend_4h(trend, ema200, st_dir, st_val)
            
            logger.info(f"[{symbol}] 4H: {trend} (close={close:.2f}, EMA200={ema200:.2f}, ST={st_dir}) [REST]")
            self._record_api_success()
        
        except Exception as e:
            logger.error(f"Error updating 4H trend for {symbol}: {e}")
            self._record_api_failure()
    
    async def _update_1h_signal(self, symbol: str, state: SymbolState):
        """Update 1H signal using WebSocket klines (for real-time display)"""
        try:
            # ===== CRITICAL FIX 3: Use await for async get_klines =====
            candles_1h = await self.websocket.get_klines(
                symbol=symbol,
                interval="60",
                limit=100
            )
            
            if not candles_1h:
                logger.debug(f"{symbol}: Fetching 1H candles from REST API (fallback)")
                candles_1h = await self.client.get_klines(
                    symbol=symbol,
                    interval="60",
                    limit=100
                )
            
            if not candles_1h:
                return
            
            # ===== IMPORTANT FIX 5: Validate sufficient data before indicator calculations =====
            min_candles_for_st = self.config.indicators.st_period_1h + 1
            if len(candles_1h) < min_candles_for_st:
                logger.warning(f"{symbol}: Not enough 1H candles for SuperTrend ({len(candles_1h)} < {min_candles_for_st})")
                return
            
            try:
                st_dir, st_val = calculate_supertrend(
                    candles_1h,
                    self.config.indicators.st_period_1h,
                    self.config.indicators.st_multiplier_1h
                )
            except (ValueError, Exception) as e:
                logger.error(f"{symbol}: Error calculating 1H SuperTrend: {e}")
                return
            
            async with self._state_lock:
                state.update_1h_signal(st_dir, st_val)
            
            # ===== CRITICAL FIX 3: Use await for async has_klines =====
            source = "WebSocket" if await self.websocket.has_klines(symbol, "60") else "REST"
            logger.debug(f"[{symbol}] 1H ST: {st_dir} [{source}]")
        
        except Exception as e:
            logger.error(f"Error updating 1H signal for {symbol}: {e}")
    
    async def _update_1h_signal_from_rest(self, symbol: str, state: SymbolState):
        """
        Update 1H signal using REST API (for confirmed candles only)
        
        This method is used when we need confirmed candle data, such as before
        making entry decisions. REST API provides confirmed (closed) candles only,
        unlike WebSocket which may send live candles.
        
        Args:
            symbol: Trading pair
            state: SymbolState instance to update
        
        Note:
            This method validates that sufficient candles are available before
            calculating indicators to prevent errors.
        """
        try:
            # ===== CRITICAL FIX 4: Always use REST API for confirmed candles =====
            candles_1h = await self.client.get_klines(
                symbol=symbol,
                interval="60",
                limit=100
            )
            
            if not candles_1h:
                logger.warning(f"{symbol}: Cannot fetch confirmed 1H candles from REST API")
                return
            
            # ===== IMPORTANT FIX 5: Validate sufficient data before indicator calculations =====
            min_candles_for_st = self.config.indicators.st_period_1h + 1
            if len(candles_1h) < min_candles_for_st:
                logger.warning(f"{symbol}: Not enough 1H candles for SuperTrend ({len(candles_1h)} < {min_candles_for_st})")
                return
            
            try:
                st_dir, st_val = calculate_supertrend(
                    candles_1h,
                    self.config.indicators.st_period_1h,
                    self.config.indicators.st_multiplier_1h
                )
            except (ValueError, Exception) as e:
                logger.error(f"{symbol}: Error calculating 1H SuperTrend from REST: {e}")
                return
            
            async with self._state_lock:
                state.update_1h_signal(st_dir, st_val)
            
            logger.debug(f"[{symbol}] 1H ST: {st_dir} [REST - CONFIRMED]")
        
        except Exception as e:
            logger.error(f"Error updating 1H signal from REST for {symbol}: {e}")
    
    async def _check_entry(self, symbol: str, state: SymbolState):
        """
        Check for entry signal with proper locking
        
        This method implements the contrarian entry strategy:
        - LONG: When 4H trend is BULLISH and 1H SuperTrend is RED (buy the dip)
        - SHORT: When 4H trend is BEARISH and 1H SuperTrend is GREEN (sell the rip)
        
        Entry conditions:
        1. No existing position
        2. Valid 4H trend (BULLISH or BEARISH, not NEUTRAL)
        3. Contrarian signal present (1H ST opposite to 4H trend)
        4. Sufficient balance available
        5. Maximum positions limit not reached (8 positions)
        6. Trading enabled
        7. Circuit breaker not active
        
        Args:
            symbol: Trading pair
            state: SymbolState instance
        
        Note:
            This method uses _entry_lock to prevent race conditions on balance
            checks and position verification. It also re-fetches confirmed 1H data
            from REST API to ensure accuracy before entry.
        """
        # ===== CRITICAL FIX: Prevent deadlock - don't hold _entry_lock when calling _enter_position =====
        # We do initial checks without lock, then _enter_position will acquire its own lock
        # Double-check position first
        if state.position_side:
            logger.debug(f"[{symbol}] Already in position ({state.position_side}), skipping entry check")
            return
        
        # Check circuit breaker
        if self._is_circuit_breaker_active():
            return
        
        # ===== FIX 9.1 & 1.1: Count total open positions with state lock to prevent race condition =====
        async with self._state_lock:
            total_open_positions = sum(1 for s in self.states.values() if s.position_side)
        
        if total_open_positions >= 8:
            logger.debug(f"[{symbol}] Maximum positions reached ({total_open_positions}/8), skipping entry check")
            return
        
        if not state.trend_4h or state.trend_4h == "NEUTRAL":
            logger.debug(f"[{symbol}] No valid 4H trend ({state.trend_4h}), skipping entry check")
            return
        
        # ===== CRITICAL FIX 4: Ensure we have confirmed 1H signal before entry =====
        # Re-fetch confirmed 1H signal to ensure we're using confirmed candle data
        await self._update_1h_signal_from_rest(symbol, state)
        
        if not state.st_1h_direction:
            logger.debug(f"[{symbol}] 1H signal not available after REST update, skipping entry check")
            return
        
        signal = self.contrarian.check_entry_signal(
            state.trend_4h,
            state.st_1h_direction
        )
        
        if not signal:
            logger.debug(f"[{symbol}] No contrarian signal (4H: {state.trend_4h}, 1H: {state.st_1h_direction})")
            return
        
        if not self.trading_enabled:
            logger.debug(f"[{symbol}] Trading disabled, skipping entry")
            return
        
        # Quick balance check (without lock) - _enter_position will do final check with lock
        await self._update_account_balance()
        if not self.account_balance:
            logger.warning(f"[{symbol}] Cannot get account balance, skipping entry")
            return
        
        # ===== FIX 6: Use centralized required_margin calculation =====
        required_margin = self._calculate_required_margin()
            
        if self.account_balance < required_margin:
            logger.warning(f"[{symbol}] Insufficient balance: {self.account_balance:.2f} USDT < {required_margin:.2f} USDT")
            return
        
        logger.info("=" * 60)
        logger.info(f"[{symbol}] 🎯 CONTRARIAN ENTRY SIGNAL: {signal}")
        logger.info(f"         4H Trend: {state.trend_4h}")
        logger.info(f"         1H ST: {state.st_1h_direction} (OPPOSITE!)")
        logger.info(f"         Balance: {self.account_balance:.2f} USDT")
        logger.info(f"         → Entering {signal}")
        logger.info("=" * 60)
        
        # ===== CRITICAL FIX: Don't hold lock here - _enter_position will acquire its own lock =====
        await self._enter_position(symbol, signal, state)
    
    async def _place_partial_tp_limit_order(self, symbol: str, state: SymbolState):
        """Place limit order for partial take profit (50% at margin + fees target)"""
        try:
            if not state.position_side or not state.entry_price or not state.position_size:
                return
            
            if state.partial_tp_done:
                return
            
            # Check for existing TP order
            open_orders = await self.client.get_open_orders(symbol)
            tp_orders = [
                o for o in open_orders 
                if o.get("reduceOnly") in [True, "true", "True"] and 
                   o.get("orderType") == "Limit" and
                   o.get("orderStatus") in ["New", "PartiallyFilled"]
            ]
            
            if tp_orders:
                logger.debug(f"[{symbol}] TP limit order already exists, skipping")
                # ===== FIX: Set tp_limit_order_id with lock for thread safety =====
                async with self._state_lock:
                    state.tp_limit_order_id = tp_orders[0].get("orderId")
                return
            
            # ===== FIX: Use centralized TP calculation =====
            target_profit = self._calculate_tp_target_profit()
            
            qty_partial = self._calculate_partial_qty(state.position_size, percentage=0.5)
            
            # ===== IMPORTANT FIX 2: Correct TP calculation for SHORT positions =====
            # For SHORT: profit = (entry_price - exit_price) * qty
            # Maximum profit occurs when exit_price = 0, so max_profit = entry_price * qty
            # We need to ensure target_profit is achievable
            if state.position_side == "SHORT":
                max_possible_profit = state.entry_price * qty_partial
                
                if target_profit > max_possible_profit:
                    logger.warning(f"[{symbol}] Target profit {target_profit:.2f} USDT exceeds maximum achievable {max_possible_profit:.2f} USDT")
                    # Use 95% of max to leave some buffer for fees and slippage
                    target_profit = max_possible_profit * 0.95
                    logger.info(f"[{symbol}] Adjusted target profit to {target_profit:.2f} USDT (95% of max)")
            
            # ===== MEDIUM FIX 1: Use centralized TP target price calculation =====
            target_price = self._calculate_tp_target_price(
                state.position_side,
                state.entry_price,
                target_profit,
                qty_partial
            )
            
            if target_price is None or target_price <= 0:
                logger.warning(f"[{symbol}] Invalid target price calculated")
                return
            
            # Get instrument info for validation
            # ===== FIX 7.1: Use centralized validation =====
            info = await self.client.get_instruments_info(symbol)
            if not self._validate_instruments_info(info, symbol):
                return
            
            price_filter = info.get("priceFilter", {})
            if not price_filter or not isinstance(price_filter, dict):
                logger.error(f"[{symbol}] Invalid priceFilter in instruments info")
                return
            
            min_price = float(price_filter.get("minPrice", 0))
            max_price = float(price_filter.get("maxPrice", 1000000))
            
            if target_price < min_price or target_price > max_price:
                logger.warning(f"[{symbol}] Target price {target_price:.8f} outside valid range")
                return
            
            target_price = await self._adjust_price_to_tick_size(symbol, target_price)
            
            if target_price <= 0 or target_price < min_price or target_price > max_price:
                logger.warning(f"[{symbol}] Adjusted target price invalid")
                return
            
            qty = await self._adjust_qty_to_step_size(symbol, qty_partial)
            
            # Validate minimum order size
            # ===== FIX 8: Add validation for lotSizeFilter =====
            lot_size_filter = info.get("lotSizeFilter", {})
            if not lot_size_filter or not isinstance(lot_size_filter, dict):
                logger.error(f"[{symbol}] Invalid lotSizeFilter in instruments info")
                return
            
            min_qty = float(lot_size_filter.get("minOrderQty", 0.001))
            if qty < min_qty:
                logger.warning(f"[{symbol}] TP qty {qty} below minimum {min_qty}")
                return
            
            # ===== MEDIUM FIX 2: Use centralized exit side conversion =====
            side = self._get_exit_side_bybit(state.position_side)
            
            order = await self.client.place_order(
                symbol=symbol,
                side=side,
                qty=qty,
                order_type="Limit",
                reduce_only=True,
                price=target_price
            )
            
            if not order:
                logger.warning(f"[{symbol}] Partial TP limit order placement failed")
                return
            
            ret_code = order.get("retCode")
            if ret_code is not None and ret_code != 0:
                error_msg = order.get("retMsg", "Unknown error")
                logger.warning(f"[{symbol}] Partial TP limit order rejected: {error_msg}")
                return
            
            result = order.get("result", {})
            order_id = result.get("orderId") if result else order.get("orderId")
            if order_id:
                # ===== FIX 2.1: Set tp_limit_order_id with lock =====
                async with self._state_lock:
                    state.tp_limit_order_id = order_id
            
            logger.info(f"[{symbol}] ✅ Partial TP limit order placed")
            logger.info(f"         Target price: {target_price:.2f}")
            logger.info(f"         Qty: {qty} (50%)")
            self._record_api_success()
            
        except Exception as e:
            logger.error(f"Error placing partial TP limit order for {symbol}: {e}", exc_info=True)
            self._record_api_failure()
    
    async def _handle_partial_tp_executed(self, symbol: str, state: SymbolState, actual_size: float, exit_price_override: Optional[float] = None):
        """
        Handle partial TP execution (when limit order was filled or manually closed)
        
        Args:
            symbol: Trading pair
            state: SymbolState instance
            actual_size: New position size after partial close
            exit_price_override: Optional exit price (for manual partial closes when execution price is not available)
        """
        try:
            # ===== CRITICAL FIX 7: Validate actual_size before calculations =====
            if actual_size is None or actual_size < 0:
                logger.error(f"[{symbol}] Invalid actual_size for partial TP: {actual_size}")
                return
            
            if not self._validate_position_size(state.position_size, symbol):
                return
            
            if not state.entry_price or state.entry_price <= 0:
                logger.error(f"[{symbol}] Invalid entry_price for partial TP: {state.entry_price}")
                return
            
            if actual_size >= state.position_size:
                logger.warning(f"[{symbol}] actual_size ({actual_size}) >= position_size ({state.position_size}), skipping partial TP")
                return
            
            target_profit = self._calculate_tp_target_profit()
            qty_closed = state.position_size - actual_size
            
            # ===== CRITICAL FIX: Get real execution price from API instead of calculating =====
            if qty_closed <= 0:
                logger.error(f"[{symbol}] Invalid qty_closed for partial TP: {qty_closed}")
                return
            
            # Try to get real execution price from order history
            exit_price = exit_price_override
            if exit_price is None or exit_price <= 0:
                if state.tp_limit_order_id:
                    execution_price = await self.client.get_order_execution_price(symbol, state.tp_limit_order_id)
                    if execution_price and execution_price > 0:
                        exit_price = execution_price
                        logger.info(f"[{symbol}] Using real execution price from API: {exit_price}")
            
            # Fallback: calculate from target profit if execution price not available
            if exit_price is None or exit_price <= 0:
                logger.warning(f"[{symbol}] Execution price not available, calculating from target profit")
                exit_price = self._calculate_tp_target_price(
                    state.position_side,
                    state.entry_price,
                    target_profit,
                    qty_closed
                )
            
            if exit_price is None or exit_price <= 0:
                logger.error(f"[{symbol}] Invalid exit_price calculated: {exit_price}")
                return
            
            # ===== FIX 1.2: Calculate actual PnL based on exit_price, not just target_profit =====
            # The actual PnL may differ from target_profit if execution price differs
            try:
                partial_pnl = self._calculate_pnl(
                    state.position_side,
                    state.entry_price,
                    exit_price,
                    qty_closed
                )
            except ValueError as e:
                logger.error(f"[{symbol}] Error calculating partial PnL: {e}, using target_profit as fallback")
                partial_pnl = target_profit
            
            async with self._state_lock:
                state.total_pnl += partial_pnl
                state.position_size = actual_size
                state.partial_tp_done = True
                # ===== FIX: Clear TP order ID (already in lock, direct assignment is safe) =====
                state.tp_limit_order_id = None
            
            self._add_trade(
                symbol=symbol,
                side=state.position_side,
                entry_price=state.entry_price,
                exit_price=exit_price,
                size=qty_closed,
                pnl=partial_pnl,
                entry_time=state.entry_time,
                is_partial=True
            )
            
            equity = self._calculate_current_equity()
            if equity is not None:
                self._add_equity_point(equity, force_add=True)
            
            logger.info(f"[{symbol}] ✅ Partial TP executed via limit order")
            logger.info(f"         Partial PnL: {partial_pnl:.2f} USDT")
            logger.info(f"         Remaining: {actual_size}")
            
        except Exception as e:
            logger.error(f"Error handling partial TP execution for {symbol}: {e}", exc_info=True)
    
    async def _check_partial_tp(self, symbol: str, state: SymbolState):
        """
        Check for partial take profit (LIMIT ORDER ONLY)
        
        This method checks if partial TP conditions are met:
        1. If TP limit order exists, verify if it was executed
        2. If no TP order exists and partial TP not done, place a new limit order
        
        Partial TP closes 50% of position when target profit (full margin + fees) is reached.
        Uses ONLY limit orders - no market orders.
        
        Args:
            symbol: Trading pair
            state: SymbolState instance
        
        Note:
            Partial TP is only executed once per position (tracked by partial_tp_done flag)
        """
        if not state.position_side or not state.entry_price or state.partial_tp_done:
            return
        
        # If TP order exists, check if executed
        if state.tp_limit_order_id:
            try:
                open_orders = await self.client.get_open_orders(symbol)
                # ===== MEDIUM FIX 3 & 4: Add null/None validation =====
                if open_orders is None:
                    open_orders = []
                
                order_exists = any(
                    o and o.get("orderId") == state.tp_limit_order_id 
                    for o in open_orders if isinstance(o, dict)
                )
                
                if not order_exists:
                    # Order executed or cancelled
                    position = await self.client.get_position(symbol)
                    if position:
                        # ===== MINOR FIX 2: Use centralized parsing method =====
                        actual_size = self._parse_position_size_from_api(position, symbol)
                        if actual_size is None:
                            return
                        
                        # ===== MEDIUM FIX 3: Add edge case validation =====
                        if not self._validate_position_size(state.position_size, symbol):
                            return
                        
                        if actual_size < state.position_size * 0.6:
                            # Partial TP executed - position size reduced by ~50%
                            logger.info(f"[{symbol}] Partial TP limit order executed!")
                            await self._handle_partial_tp_executed(symbol, state, actual_size)
                            return
                        elif actual_size >= state.position_size * 0.95:
                            # ===== FIX 5.1: Order was cancelled (not executed) - reset tp_limit_order_id =====
                            # Position size is still close to original (>95%), so order was cancelled
                            logger.info(f"[{symbol}] TP limit order was cancelled (not executed) - resetting order ID")
                            await self._clear_tp_order_id(state)
                            return
                        else:
                            # Position size changed but not by 50% - might be manual partial close
                            # Check if it's approximately 50% reduction
                            size_ratio = self._calculate_size_ratio(actual_size, state.position_size)
                            if size_ratio is not None and 0.45 <= size_ratio <= 0.55:
                                # It's a partial close - treat as TP executed
                                logger.info(f"[{symbol}] Detected partial position close (size ratio: {size_ratio:.2%}) - treating as TP executed")
                                
                                # ===== CRITICAL FIX: Try to get real execution price from recent executions =====
                                exit_price_override = await self._get_execution_price_from_recent_executions(
                                    symbol, state.position_side, time_window_seconds=300
                                )
                                
                                await self._handle_partial_tp_executed(symbol, state, actual_size, exit_price_override=exit_price_override)
                            else:
                                # Position changed but not a clear partial TP - just reset order ID
                                logger.warning(f"[{symbol}] Position size changed unexpectedly (ratio: {size_ratio:.2%}) - resetting TP order ID")
                                await self._clear_tp_order_id(state)
                            return
                    else:
                        # ===== FIX 5.1: Position closed, reset TP order ID =====
                        await self._clear_tp_order_id(state)
                        return
            except Exception as e:
                logger.error(f"[{symbol}] Error checking TP order status: {e}")
                return
        
        # If no TP order exists and partial TP not done, place limit order
        if not state.tp_limit_order_id and not state.partial_tp_done:
            logger.info(f"[{symbol}] No TP limit order found - placing new limit order")
            await self._place_partial_tp_limit_order(symbol, state)
    
    async def _adjust_qty_to_step_size(self, symbol: str, qty: float) -> float:
        """Adjust quantity to symbol's step size"""
        try:
            # ===== FIX 7.1: Use centralized validation =====
            info = await self.client.get_instruments_info(symbol)
            if not self._validate_instruments_info(info, symbol):
                return qty
            
            lot_size_filter = info.get("lotSizeFilter", {})
            if not lot_size_filter or not isinstance(lot_size_filter, dict):
                logger.error(f"[{symbol}] Invalid lotSizeFilter in instruments info")
                return qty
            
            qty_step = float(lot_size_filter.get("qtyStep", 0.001))
            min_qty = float(lot_size_filter.get("minOrderQty", 0.001))
            max_qty = float(lot_size_filter.get("maxOrderQty", 1000000))
            
            # Use Decimal for precision
            qty_decimal = Decimal(str(qty))
            step_decimal = Decimal(str(qty_step))
            qty_adjusted = float((qty_decimal // step_decimal) * step_decimal)
            
            qty_adjusted = max(min_qty, min(qty_adjusted, max_qty))
            
            return qty_adjusted
        except Exception as e:
            logger.error(f"Error adjusting qty to step size for {symbol}: {e}")
            return qty
    
    async def _adjust_price_to_tick_size(self, symbol: str, price: float) -> float:
        """Adjust price to symbol's tick size"""
        try:
            # ===== FIX 7.1: Use centralized validation =====
            info = await self.client.get_instruments_info(symbol)
            if not self._validate_instruments_info(info, symbol):
                return price
            
            price_filter = info.get("priceFilter", {})
            if not price_filter or not isinstance(price_filter, dict):
                logger.error(f"[{symbol}] Invalid priceFilter in instruments info")
                return price
            
            tick_size = float(price_filter.get("tickSize", 0.01))
            min_price = float(price_filter.get("minPrice", 0))
            max_price = float(price_filter.get("maxPrice", 1000000))
            
            # Use Decimal for precision
            price_decimal = Decimal(str(price))
            tick_decimal = Decimal(str(tick_size))
            price_adjusted = float(round(price_decimal / tick_decimal) * tick_decimal)
            
            price_adjusted = max(min_price, min(price_adjusted, max_price))
            
            return price_adjusted
        except Exception as e:
            logger.error(f"Error adjusting price to tick size for {symbol}: {e}")
            return price
    
    # NOTE: _partial_exit_position is currently unused (fallback market order removed)
    # Kept for potential future use or manual partial exits
    # async def _partial_exit_position(self, symbol: str, state: SymbolState, percentage: float = 0.5):
    #     """Close partial position (currently unused - partial TP uses only limit orders)"""
    #     ...
    
    async def _check_exit(self, symbol: str, state: SymbolState):
        """
        Check for exit signal
        
        Exit conditions:
        1. 4H SuperTrend flips direction (trend reversal)
        2. 4H SuperTrend is opposite to position (immediate exit)
        
        Cooldown protection:
        - Positions opened less than 1 hour ago are protected from flip-based exits
        - Only strong opposite signals (ST opposite to position) can exit during cooldown
        - This prevents premature exits on temporary ST flips right after entry
        
        Args:
            symbol: Trading pair
            state: SymbolState instance
        
        Note:
            Exit is always allowed if 4H ST is opposite to position, regardless
            of cooldown period. This ensures we exit when trend clearly reversed.
        """
        if not state.position_side:
            return
        
        # ===== FIX 9: Check exit signal first, then apply cooldown logic =====
        # Check if exit signal is present (handles both flip and opposite ST)
        should_exit = self.contrarian.check_exit_signal(
            state.position_side,
            state.st_4h_direction,
            state.st_4h_prev_direction,
            state.trend_4h
        )
        
        # ===== IMPORTANT FIX 3: Prevent early exit - add cooldown period =====
        # Don't exit immediately after entry to avoid premature exits due to ST flips
        # BUT allow exit if ST is clearly opposite to position (strong signal)
        if should_exit and state.entry_time:
            time_since_entry = (datetime.now() - state.entry_time).total_seconds()
            min_hold_time = 3600  # 1 hour minimum hold time
            if time_since_entry < min_hold_time:
                # Check if exit is due to opposite ST (strong signal) or just a flip
                is_opposite_st = (
                    (state.position_side == "LONG" and state.st_4h_direction == "red") or
                    (state.position_side == "SHORT" and state.st_4h_direction == "green")
                )
                
                if is_opposite_st:
                    # Allow exit if ST is opposite (strong signal) even during cooldown
                    logger.info(f"[{symbol}] Early exit allowed: {state.position_side} position but 4H ST is {state.st_4h_direction} (cooldown: {int(time_since_entry)}s)")
                elif state.st_4h_prev_direction and state.st_4h_prev_direction != state.st_4h_direction:
                    # This is a flip - allow it even during cooldown if it's a real flip
                    logger.info(f"[{symbol}] Early exit allowed: 4H ST flipped from {state.st_4h_prev_direction} to {state.st_4h_direction} (cooldown: {int(time_since_entry)}s)")
                else:
                    # During cooldown, block exit if it's not a clear signal
                    logger.debug(f"[{symbol}] Exit blocked during cooldown ({int(time_since_entry)}s) - not a strong signal")
                    should_exit = False
        
        if should_exit:
            logger.info("=" * 60)
            logger.info(f"[{symbol}] 🚪 EXIT SIGNAL")
            logger.info(f"         4H SuperTrend: {state.st_4h_direction}")
            logger.info(f"         Previous: {state.st_4h_prev_direction}")
            logger.info("=" * 60)
            await self._exit_position(symbol, state)
    
    async def _enter_position(self, symbol: str, side: str, state: SymbolState):
        """Execute entry order"""
        try:
            # ===== CRITICAL FIX 1: Re-check balance and position immediately before order =====
            # This prevents race conditions where balance/position changes between check and order
            # ===== FIX 2.1: Keep entry_lock until order is placed to prevent duplicate entries =====
            async with self._entry_lock:
                # Re-check position (may have been opened by another process)
                if state.position_side:
                    logger.debug(f"[{symbol}] Position already exists ({state.position_side}), skipping entry")
                    return
                
                # Re-check balance with fresh data
                await self._update_account_balance()
                if not self.account_balance:
                    logger.warning(f"[{symbol}] Cannot get account balance, skipping entry")
                    return
                
                # ===== FIX 6: Use centralized required_margin calculation =====
                required_margin = self._calculate_required_margin()
                if self.account_balance < required_margin:
                    logger.warning(f"[{symbol}] Insufficient balance at order time: {self.account_balance:.2f} USDT < {required_margin:.2f} USDT")
                    return
                
                # Final position check right before order placement
                if state.position_side:
                    logger.debug(f"[{symbol}] Position opened by another process during entry, skipping")
                    return
                
                price = await self._get_current_price_with_fallback(symbol)
                if not price:
                    logger.error(f"[{symbol}] Cannot get ticker price")
                    return
                
                # ===== FIX 8.1 & 1.1: Explicit error handling for calculate_qty =====
                try:
                    qty = await self.client.calculate_qty(
                        symbol=symbol,
                        size_usdt=self.config.trading.position_size_usdt,
                        price=price
                    )
                except ValueError as e:
                    logger.error(f"[{symbol}] Cannot calculate quantity: {e}")
                    self._record_api_failure()
                    return
                except Exception as e:
                    logger.error(f"[{symbol}] Unexpected error calculating quantity: {e}")
                    self._record_api_failure()
                    return
                
                order = await self.client.place_order(
                    symbol=symbol,
                    side=self._internal_side_to_bybit(side),
                    qty=qty,
                    order_type="Market"
                )
                
                if not order:
                    logger.error(f"[{symbol}] Order placement failed")
                    self._record_api_failure()
                    return
                
                if isinstance(order, dict) and order.get("retCode") != 0:
                    error_code = order.get("retCode")
                    error_msg = order.get("retMsg", "Unknown error")
                    
                    if error_code == 110007:
                        logger.warning(f"[{symbol}] Insufficient balance for order: {error_msg}")
                        return
                    
                    logger.error(f"[{symbol}] Order placement failed: {error_msg}")
                    self._record_api_failure()
                    return
            
            await asyncio.sleep(1)
            
            # Verify position
            position = await self.client.get_position(symbol)
            
            if not position:
                logger.error(f"[{symbol}] Order placed but position not found")
                return
            
            # ===== CRITICAL FIX 7: Validate position data from API =====
            size_str = position.get('size', '0')
            raw_side = position.get('side')
            avg_price_str = position.get('avgPrice', '0')
            
            if not size_str or not raw_side or not avg_price_str:
                logger.error(f"[{symbol}] Invalid position data from API after order")
                return
            
            try:
                actual_size = float(size_str)
                actual_entry_price = float(avg_price_str)
            except (ValueError, TypeError) as e:
                logger.error(f"[{symbol}] Error parsing position data: {e}")
                return
            
            if actual_size <= 0:
                logger.error(f"[{symbol}] Order placed but position size is 0")
                return
            
            if actual_entry_price <= 0:
                logger.error(f"[{symbol}] Invalid entry price from API: {actual_entry_price}")
                return
            
            actual_side = self._bybit_side_to_internal(raw_side)
            if actual_side not in ["LONG", "SHORT"]:
                logger.error(f"[{symbol}] Invalid side from API: {raw_side}")
                return
            
            async with self._state_lock:
                state.open_position(actual_side, actual_size, actual_entry_price)
            
            logger.info(f"[{symbol}] ✅ {actual_side} position opened")
            logger.info(f"         Entry: {actual_entry_price}")
            logger.info(f"         Qty: {actual_size}")
            
            # Place TP order
            await asyncio.sleep(0.5)
            await self._place_partial_tp_limit_order(symbol, state)
            self._record_api_success()
        
        except Exception as e:
            logger.error(f"Error entering position for {symbol}: {e}", exc_info=True)
            self._record_api_failure()
    
    async def _exit_position(self, symbol: str, state: SymbolState):
        """Execute exit order"""
        try:
            # Cancel TP order if exists
            if state.tp_limit_order_id:
                try:
                    await self.client.cancel_order(symbol, state.tp_limit_order_id)
                    logger.info(f"[{symbol}] Cancelled TP limit order before exit")
                except Exception as e:
                    logger.warning(f"[{symbol}] Error cancelling TP order: {e}")
                finally:
                    # ===== FIX 3: Use centralized method with lock =====
                    await self._clear_tp_order_id(state)
            
            exit_price = await self._get_current_price_with_fallback(symbol)
            if not exit_price:
                logger.error(f"[{symbol}] Cannot get ticker price for exit")
                return
            
            # ===== MEDIUM FIX 2: Use centralized exit side conversion =====
            # ===== MEDIUM FIX 4: Add null/None validation =====
            if not state.position_side or not state.entry_price or not state.position_size:
                logger.error(f"[{symbol}] Cannot exit - missing position data (side: {state.position_side}, entry: {state.entry_price}, size: {state.position_size})")
                return
            
            if not self._validate_position_size(state.position_size, symbol):
                return
            
            side = self._get_exit_side_bybit(state.position_side)
            
            order = await self.client.place_order(
                symbol=symbol,
                side=side,
                qty=state.position_size,
                order_type="Market",
                reduce_only=True
            )
            
            if not order:
                logger.error(f"[{symbol}] Exit order placement failed")
                self._record_api_failure()
                return
            
            # ===== FIX 7.1: Use centralized PnL calculation =====
            entry_price = state.entry_price
            entry_time = state.entry_time
            position_side = state.position_side
            position_size = state.position_size
            
            # Calculate PnL using centralized method
            try:
                pnl = self._calculate_pnl(position_side, entry_price, exit_price, position_size)
            except ValueError as e:
                logger.error(f"[{symbol}] Error calculating PnL: {e}")
                pnl = state.get_unrealized_pnl(exit_price)  # Fallback
            
            async with self._state_lock:
                # ===== FIX 7.1: Pass pre-calculated PnL to avoid duplication =====
                state.close_position(exit_price, pnl=pnl)
            
            if entry_price and position_side and position_size:
                self._add_trade(
                    symbol=symbol,
                    side=position_side,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    size=position_size,
                    pnl=pnl,
                    entry_time=entry_time
                )
            
            equity = self._calculate_current_equity()
            if equity is not None:
                self._add_equity_point(equity, force_add=True)
            
            logger.info(f"[{symbol}] ✅ Position closed")
            logger.info(f"         Exit: {exit_price}")
            logger.info(f"         PnL: {pnl:.2f} USDT")
            self._record_api_success()
        
        except Exception as e:
            logger.error(f"Error exiting position for {symbol}: {e}", exc_info=True)
            self._record_api_failure()
    
    def get_status(self) -> dict:
        """Get bot status"""
        # Ensure all symbols have states
        for symbol in self.config.trading.symbols:
            if symbol not in self.states:
                self.states[symbol] = SymbolState(symbol=symbol)
            if symbol not in self.klines_initialized:
                self.klines_initialized[symbol] = False
        
        symbols_status = []
        for symbol in self.config.trading.symbols:
            state = self.states.get(symbol)
            if not state:
                state = SymbolState(symbol=symbol)
                self.states[symbol] = state
            status_dict = state.get_status_dict()
            
            current_price = self.realtime_prices.get(symbol)
            if current_price:
                status_dict['current_price'] = current_price
                
                if state.position_side and state.entry_price:
                    unrealized_pnl = state.get_unrealized_pnl(current_price)
                    status_dict['unrealized_pnl'] = unrealized_pnl
                    
                    if state.position_side == "LONG":
                        pnl_percent = ((current_price - state.entry_price) / state.entry_price) * 100
                    else:
                        pnl_percent = ((state.entry_price - current_price) / state.entry_price) * 100
                    status_dict['pnl_percent'] = pnl_percent
            
            symbols_status.append(status_dict)
        
        uptime_seconds = 0
        if self.start_time:
            uptime_seconds = int((datetime.now() - self.start_time).total_seconds())
        
        return {
            "running": self.running,
            "trading_enabled": self.trading_enabled,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "uptime_seconds": uptime_seconds,
            "circuit_breaker_active": self._is_circuit_breaker_active(),
            "api_failure_count": self._api_failure_count,
            "websocket": {
                "connected": self.websocket.is_connected(),
                "subscribed_symbols": list(self.websocket.get_subscribed_symbols()),
                "subscribed_klines": list(self.websocket.get_subscribed_klines()),
                "realtime_prices": len(self.realtime_prices),
                "klines_initialized": sum(1 for v in self.klines_initialized.values() if v)
            },
            "config": {
                "symbols": self.config.trading.symbols,
                "position_size_usdt": self.config.trading.position_size_usdt,
                "leverage": self.config.trading.leverage,
                "margin_mode": self.config.trading.margin_mode
            },
            "account": {
                "balance": self.account_balance,
                "total_equity": self.total_equity,
                "last_update": self.last_balance_update.isoformat() if self.last_balance_update else None
            },
            "equity_history": self.equity_history[-100:],
            "trade_history": self.trade_history[-100:],
            "symbols": symbols_status
        }
    
    async def get_symbol_chart_data(self, symbol: str) -> dict:
        """Get chart data for a symbol"""
        try:
            state = self.states.get(symbol)
            if not state:
                return {"error": "Symbol not found"}
            
            # ===== CRITICAL FIX 3: Use async methods or lock for kline_data access =====
            candles_4h = await self.websocket.get_klines_chronological(symbol, "240", limit=200)
            candles_1h = await self.websocket.get_klines_chronological(symbol, "60", limit=200)
            
            ema200_4h = state.ema200_4h if hasattr(state, 'ema200_4h') and state.ema200_4h else None
            st_4h_direction = state.st_4h_direction if hasattr(state, 'st_4h_direction') else None
            st_4h_value = state.st_4h_value if hasattr(state, 'st_4h_value') and state.st_4h_value else None
            st_1h_direction = state.st_1h_direction if hasattr(state, 'st_1h_direction') else None
            st_1h_value = state.st_1h_value if hasattr(state, 'st_1h_value') and state.st_1h_value else None
            
            position_info = None
            if state.position_side and state.entry_price:
                current_price = self.realtime_prices.get(symbol, state.entry_price)
                unrealized_pnl = state.get_unrealized_pnl(current_price)
                
                if state.position_side == "LONG":
                    pnl_percent = ((current_price - state.entry_price) / state.entry_price) * 100
                else:
                    pnl_percent = ((state.entry_price - current_price) / state.entry_price) * 100
                
                # ===== MEDIUM FIX 1: Use centralized TP target price calculation =====
                tp_target_price = None
                if state.position_size and state.entry_price and state.position_side:
                    target_profit = self._calculate_tp_target_profit()
                    qty_partial = self._calculate_partial_qty(state.position_size, percentage=0.5)
                    tp_target_price = self._calculate_tp_target_price(
                        state.position_side,
                        state.entry_price,
                        target_profit,
                        qty_partial
                    )
                
                position_info = {
                    "side": state.position_side,
                    "entry_price": round(state.entry_price, 2),
                    "entry_time": state.entry_time.isoformat() if state.entry_time else None,
                    "position_size": round(state.position_size, 8) if state.position_size else 0,
                    "current_price": round(current_price, 2),
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "pnl_percent": round(pnl_percent, 2),
                    "partial_tp_done": state.partial_tp_done,
                    "tp_target_price": round(tp_target_price, 2) if tp_target_price else None
                }
            
            current_price = self.realtime_prices.get(symbol, 0)
            if current_price <= 0:
                if candles_4h:
                    current_price = float(candles_4h[-1][4])
                elif candles_1h:
                    current_price = float(candles_1h[-1][4])
            
            return {
                "symbol": symbol,
                "candles_4h": candles_4h[-200:] if candles_4h else [],
                "candles_1h": candles_1h[-200:] if candles_1h else [],
                "indicators": {
                    "ema200_4h": round(ema200_4h, 2) if ema200_4h else None,
                    "st_4h_direction": st_4h_direction,
                    "st_4h_value": round(st_4h_value, 2) if st_4h_value else None,
                    "st_1h_direction": st_1h_direction,
                    "st_1h_value": round(st_1h_value, 2) if st_1h_value else None
                },
                "position": position_info,
                "current_price": round(current_price, 2) if current_price > 0 else 0
            }
        except Exception as e:
            logger.error(f"Error getting chart data for {symbol}: {e}", exc_info=True)
            return {"error": str(e)}


# Global bot instance
bot_controller = None
