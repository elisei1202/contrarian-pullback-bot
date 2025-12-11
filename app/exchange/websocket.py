import asyncio
import json
import logging
from typing import Dict, Callable, Optional, List, Set
from collections import deque
import aiohttp

logger = logging.getLogger(__name__)


class BybitWebSocket:
    """
    Bybit WebSocket V5 Client for real-time price updates and klines
    
    Features:
    - Automatic reconnection with exponential backoff
    - Proper cleanup of sessions
    - Thread-safe callback handling
    - Candle confirmation tracking
    """
    
    def __init__(self, testnet: bool = False):
        self.testnet = testnet
        self.ws_url = "wss://stream-testnet.bybit.com/v5/public/linear" if testnet else "wss://stream.bybit.com/v5/public/linear"
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.callbacks: Dict[str, Callable] = {}
        self.kline_callbacks: Dict[str, Callable] = {}
        self.running = False
        self.subscribed_symbols: Set[str] = set()
        self.subscribed_klines: Set[str] = set()
        self.ping_task: Optional[asyncio.Task] = None
        self.reconnect_delay = 5
        self.max_reconnect_delay = 60
        self.current_reconnect_delay = 5
        
        # Kline data storage
        self.kline_data: Dict[str, deque] = {}
        self.max_klines_per_symbol = 500
        # ===== CRITICAL FIX 3: Lock for kline_data access to prevent race conditions =====
        self._kline_lock = asyncio.Lock()
        
        # Connection state
        self._connecting = False
        self._reconnect_count = 0
        self._max_reconnect_attempts = 10
    
    async def connect(self) -> bool:
        """Establish WebSocket connection"""
        if self._connecting:
            return False
        
        self._connecting = True
        
        try:
            # Cleanup old connections
            await self._cleanup()
            
            self.session = aiohttp.ClientSession()
            self.ws = await self.session.ws_connect(
                self.ws_url,
                heartbeat=20,
                receive_timeout=60  # Increased to reduce false timeouts
            )
            self.running = True
            self._reconnect_count = 0
            self.current_reconnect_delay = self.reconnect_delay
            
            logger.info(f"✓ WebSocket connected to {self.ws_url}")
            
            # Start ping task
            if self.ping_task:
                self.ping_task.cancel()
            self.ping_task = asyncio.create_task(self._ping_loop())
            
            return True
        
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
            return False
        finally:
            self._connecting = False
    
    async def _cleanup(self):
        """Cleanup old WebSocket and session"""
        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None
        
        if self.session and not self.session.closed:
            try:
                await self.session.close()
            except Exception:
                pass
        self.session = None
    
    async def disconnect(self):
        """Close WebSocket connection"""
        self.running = False
        
        if self.ping_task:
            self.ping_task.cancel()
            try:
                await self.ping_task
            except asyncio.CancelledError:
                pass
        
        await self._cleanup()
        logger.info("✓ WebSocket disconnected")
    
    async def subscribe_ticker(self, symbol: str, callback: Callable) -> bool:
        """Subscribe to ticker updates for a symbol"""
        if not self.ws or not self.running:
            logger.error("WebSocket not connected")
            return False
        
        try:
            self.callbacks[symbol] = callback
            
            subscribe_msg = {
                "op": "subscribe",
                "args": [f"tickers.{symbol}"]
            }
            
            await self.ws.send_json(subscribe_msg)
            self.subscribed_symbols.add(symbol)
            
            logger.info(f"✓ Subscribed to {symbol} ticker")
            return True
        
        except Exception as e:
            logger.error(f"Error subscribing to {symbol}: {e}")
            return False
    
    async def subscribe_kline(self, symbol: str, interval: str, callback: Optional[Callable] = None) -> bool:
        """Subscribe to kline updates for a symbol and interval"""
        if not self.ws or not self.running:
            logger.error("WebSocket not connected")
            return False
        
        try:
            key = f"{symbol}:{interval}"
            if callback:
                self.kline_callbacks[key] = callback
            
            if key not in self.kline_data:
                self.kline_data[key] = deque(maxlen=self.max_klines_per_symbol)
            
            subscribe_msg = {
                "op": "subscribe",
                "args": [f"kline.{interval}.{symbol}"]
            }
            
            await self.ws.send_json(subscribe_msg)
            self.subscribed_klines.add(key)
            
            logger.info(f"✓ Subscribed to {symbol} kline {interval}min")
            return True
        
        except Exception as e:
            logger.error(f"Error subscribing to {symbol} kline {interval}: {e}")
            return False
    
    async def get_klines(self, symbol: str, interval: str, limit: int = 200) -> List[list]:
        """
        Get stored kline data from WebSocket cache
        
        Returns in reverse chronological order (newest first) to match REST API format
        """
        key = f"{symbol}:{interval}"
        async with self._kline_lock:
            if key not in self.kline_data:
                return []
            
            klines = list(self.kline_data[key])
            latest_klines = klines[-limit:] if len(klines) > limit else klines
            return latest_klines[::-1]
    
    async def get_klines_chronological(self, symbol: str, interval: str, limit: int = 200) -> List[list]:
        """
        Get stored kline data in chronological order (oldest first)
        
        Useful for indicator calculations
        """
        key = f"{symbol}:{interval}"
        async with self._kline_lock:
            if key not in self.kline_data:
                return []
            
            klines = list(self.kline_data[key])
            return klines[-limit:] if len(klines) > limit else klines
    
    async def has_klines(self, symbol: str, interval: str) -> bool:
        """Check if kline data is available"""
        key = f"{symbol}:{interval}"
        async with self._kline_lock:
            return key in self.kline_data and len(self.kline_data[key]) > 0
    
    async def unsubscribe_ticker(self, symbol: str):
        """Unsubscribe from ticker updates"""
        if not self.ws or not self.running:
            return
        
        try:
            unsubscribe_msg = {
                "op": "unsubscribe",
                "args": [f"tickers.{symbol}"]
            }
            
            await self.ws.send_json(unsubscribe_msg)
            self.subscribed_symbols.discard(symbol)
            self.callbacks.pop(symbol, None)
            
            logger.info(f"✓ Unsubscribed from {symbol}")
        
        except Exception as e:
            logger.error(f"Error unsubscribing from {symbol}: {e}")
    
    async def unsubscribe_kline(self, symbol: str, interval: str):
        """Unsubscribe from kline updates"""
        if not self.ws or not self.running:
            return
        
        try:
            key = f"{symbol}:{interval}"
            unsubscribe_msg = {
                "op": "unsubscribe",
                "args": [f"kline.{interval}.{symbol}"]
            }
            
            await self.ws.send_json(unsubscribe_msg)
            self.subscribed_klines.discard(key)
            self.kline_callbacks.pop(key, None)
            
            logger.info(f"✓ Unsubscribed from {symbol} kline {interval}min")
        
        except Exception as e:
            logger.error(f"Error unsubscribing from {symbol} kline: {e}")
    
    async def _ping_loop(self):
        """Send ping every 20 seconds to keep connection alive"""
        while self.running:
            try:
                await asyncio.sleep(20)
                
                if self.ws and not self.ws.closed:
                    await self.ws.send_json({"op": "ping"})
                    logger.debug("WebSocket ping sent")
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ping error: {e}")
    
    async def _reconnect(self):
        """Handle reconnection with backoff"""
        if self._reconnect_count >= self._max_reconnect_attempts:
            logger.error(f"Max reconnection attempts ({self._max_reconnect_attempts}) reached")
            return False
        
        self._reconnect_count += 1
        logger.warning(f"Reconnecting... (attempt {self._reconnect_count})")
        
        await asyncio.sleep(self.current_reconnect_delay)
        self.current_reconnect_delay = min(
            self.current_reconnect_delay * 2,
            self.max_reconnect_delay
        )
        
        connected = await self.connect()
        
        if connected:
            # Re-subscribe to all tickers
            for symbol in list(self.subscribed_symbols):
                callback = self.callbacks.get(symbol)
                if callback:
                    await self.subscribe_ticker(symbol, callback)
            
            # Re-subscribe to all klines
            for key in list(self.subscribed_klines):
                parts = key.split(":")
                if len(parts) == 2:
                    symbol, interval = parts
                    callback = self.kline_callbacks.get(key)
                    await self.subscribe_kline(symbol, interval, callback)
        
        return connected
    
    async def listen(self):
        """Listen for WebSocket messages and dispatch to callbacks"""
        while self.running:
            try:
                if not self.ws or self.ws.closed:
                    logger.warning("WebSocket closed, reconnecting...")
                    if not await self._reconnect():
                        await asyncio.sleep(self.current_reconnect_delay)
                    continue
                
                msg = await self.ws.receive()
                
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning(f"WebSocket {msg.type.name}, will reconnect...")
                    await self._cleanup()
                    continue
            
            except asyncio.CancelledError:
                break
            
            except (TimeoutError, asyncio.TimeoutError) as e:
                # Timeout doesn't necessarily mean connection is dead
                # Check if connection is still alive before reconnecting
                if not self.ws or self.ws.closed:
                    logger.warning("WebSocket timeout and connection closed, reconnecting...")
                    if not await self._reconnect():
                        await asyncio.sleep(self.current_reconnect_delay)
                else:
                    # Connection still alive, just no messages received
                    logger.debug("WebSocket receive timeout (no messages), continuing...")
                    continue
            
            except Exception as e:
                logger.error(f"WebSocket listen error: {e}", exc_info=True)
                # Check connection state before deciding to reconnect
                if not self.ws or self.ws.closed:
                    logger.warning("WebSocket error and connection closed, reconnecting...")
                    if not await self._reconnect():
                        await asyncio.sleep(self.current_reconnect_delay)
                else:
                    await asyncio.sleep(self.reconnect_delay)
    
    async def _handle_message(self, data: str):
        """Handle incoming WebSocket message"""
        try:
            message = json.loads(data)
            
            # Handle subscription confirmation
            if message.get("op") == "subscribe":
                if message.get("success"):
                    logger.debug(f"Subscription confirmed: {message.get('ret_msg')}")
                else:
                    logger.error(f"Subscription failed: {message.get('ret_msg')}")
                return
            
            # Handle pong
            if message.get("op") == "pong":
                logger.debug("WebSocket pong received")
                return
            
            # Handle ticker data
            topic = message.get("topic", "")
            if topic.startswith("tickers."):
                await self._handle_ticker_update(message)
            
            # Handle kline data
            elif topic.startswith("kline."):
                await self._handle_kline_update(message)
            
            # Log unknown messages for debugging
            elif topic and not message.get("op"):
                logger.debug(f"Unknown message topic: {topic}")
        
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse WebSocket message: {e}")
        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}", exc_info=True)
    
    async def _handle_ticker_update(self, message: dict):
        """Handle ticker update message"""
        try:
            topic = message.get("topic", "")
            symbol = topic.replace("tickers.", "")
            
            data = message.get("data", {})
            
            if isinstance(data, list):
                if not data:
                    return
                ticker_data = data[-1] if isinstance(data[-1], dict) else {}
            elif isinstance(data, dict):
                ticker_data = data
            else:
                return
            
            price_str = ticker_data.get("lastPrice", "0")
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                return
            
            if price > 0 and symbol in self.callbacks:
                callback = self.callbacks[symbol]
                try:
                    await callback(symbol, price)
                except Exception as e:
                    logger.error(f"Error in ticker callback for {symbol}: {e}")
        
        except Exception as e:
            logger.error(f"Error processing ticker update: {e}")
    
    async def _handle_kline_update(self, message: dict):
        """Handle kline update message"""
        try:
            topic = message.get("topic", "")
            parts = topic.split(".")
            if len(parts) != 3:
                return
            
            interval = parts[1]
            symbol = parts[2]
            key = f"{symbol}:{interval}"
            
            data = message.get("data", {})
            if not data:
                return
            
            candles_list = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
            if not candles_list:
                return
            
            latest_candle = candles_list[-1]
            
            # Parse candle data
            if isinstance(latest_candle, list):
                if len(latest_candle) < 6:
                    return
                candle_array = latest_candle
            elif isinstance(latest_candle, dict):
                candle_array = [
                    latest_candle.get("start", 0),
                    latest_candle.get("open", "0"),
                    latest_candle.get("high", "0"),
                    latest_candle.get("low", "0"),
                    latest_candle.get("close", "0"),
                    latest_candle.get("volume", "0"),
                    latest_candle.get("turnover", "0"),
                    latest_candle.get("confirm", False)
                ]
            else:
                return
            
            # Convert to standard format
            try:
                candle = [
                    int(candle_array[0]),
                    float(candle_array[1]),
                    float(candle_array[2]),
                    float(candle_array[3]),
                    float(candle_array[4]),
                    float(candle_array[5]),
                    float(candle_array[6]) if len(candle_array) > 6 else 0.0
                ]
            except (ValueError, TypeError, IndexError) as e:
                logger.warning(f"Invalid candle data for {key}: {e}")
                return
            
            is_confirmed = bool(candle_array[7]) if len(candle_array) > 7 else False
            
            # ===== CRITICAL FIX 3: Protect kline_data access with lock =====
            async with self._kline_lock:
                # Store kline data
                if key not in self.kline_data:
                    self.kline_data[key] = deque(maxlen=self.max_klines_per_symbol)
                
                klines = self.kline_data[key]
                
                if len(klines) > 0:
                    last_candle = klines[-1]
                    last_timestamp = int(last_candle[0]) if last_candle[0] else 0
                    current_timestamp = candle[0]
                    
                    if current_timestamp == last_timestamp:
                        # Update existing candle
                        klines[-1] = candle
                    elif current_timestamp > last_timestamp:
                        # New candle
                        klines.append(candle)
                else:
                    klines.append(candle)
            
            # Trigger callback for confirmed candles (outside lock to avoid deadlock)
            if is_confirmed and key in self.kline_callbacks:
                callback = self.kline_callbacks[key]
                try:
                    await callback(symbol, interval, candle, is_confirmed)
                except Exception as e:
                    logger.error(f"Error in kline callback for {key}: {e}")
        
        except Exception as e:
            logger.error(f"Error processing kline update: {e}", exc_info=True)
    
    def is_connected(self) -> bool:
        """Check if WebSocket is connected"""
        return self.ws is not None and not self.ws.closed and self.running
    
    def get_subscribed_symbols(self) -> Set[str]:
        """Get set of subscribed symbols"""
        return self.subscribed_symbols.copy()
    
    def get_subscribed_klines(self) -> Set[str]:
        """Get set of subscribed klines (format: symbol:interval)"""
        return self.subscribed_klines.copy()
    
    def get_reconnect_count(self) -> int:
        """Get number of reconnection attempts"""
        return self._reconnect_count
