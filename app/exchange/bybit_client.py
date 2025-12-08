import aiohttp
import asyncio
import time
import hmac
import hashlib
import json
import logging
from typing import Dict, List, Optional
from decimal import Decimal, ROUND_DOWN

logger = logging.getLogger(__name__)


class BybitAPIError(Exception):
    """Custom exception for Bybit API errors"""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Bybit API Error {code}: {message}")


class BybitClient:
    """
    Async Bybit V5 API Client
    
    Consistent error handling:
    - All methods return result dict on success
    - Non-critical errors return dict with retCode
    - Critical errors raise BybitAPIError
    """
    
    NON_CRITICAL_ERRORS = {
        110043,  # Leverage not modified
        100028,  # Unified account margin mode issue
        110007,  # Not enough balance
        110025,  # Price out of range
        110026,  # Position not exists
    }
    
    RETRY_ERRORS = {
        10002,   # Timestamp error
        10006,   # Rate limit
        10018,   # Internal error
        10019,   # Server busy
        10004,   # IP ban (temporary)
    }
    
    def __init__(self, config):
        self.api_key = config.api_key
        self.api_secret = config.api_secret
        self.base_url = config.base_url
        self.recv_window = "10000"
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close(self):
        """Close the client session"""
        if self._session and not self._session.closed:
            await self._session.close()
    
    def _generate_signature(self, params: str, timestamp: str) -> str:
        """Generate HMAC SHA256 signature"""
        param_str = f"{timestamp}{self.api_key}{self.recv_window}{params}"
        return hmac.new(
            self.api_secret.encode('utf-8'),
            param_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    
    def _get_headers(self, params: str = "") -> Dict[str, str]:
        """Get request headers with signature"""
        timestamp = str(int(time.time() * 1000))
        signature = self._generate_signature(params, timestamp)
        
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "Content-Type": "application/json"
        }
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        signed: bool = False,
        max_retries: int = 3,
        return_full_response: bool = False
    ) -> Dict:
        """Make HTTP request to Bybit API with retry logic"""
        url = f"{self.base_url}{endpoint}"
        session = await self._get_session()
        
        for attempt in range(max_retries):
            try:
                headers = {}
                if signed:
                    params_str = json.dumps(params) if params and method == "POST" else ""
                    if method == "GET" and params:
                        params_str = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
                    headers = self._get_headers(params_str)
                
                if method == "GET":
                    async with session.get(url, params=params, headers=headers) as response:
                        data = await response.json()
                else:
                    async with session.post(url, json=params, headers=headers) as response:
                        data = await response.json()
                
                if not data:
                    raise Exception("Empty response from Bybit API")
                
                ret_code = data.get("retCode", 0)
                ret_msg = data.get("retMsg", "Unknown error")
                
                if ret_code != 0:
                    if ret_code in self.RETRY_ERRORS and attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.warning(f"API error {ret_code}, retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    
                    if ret_code in self.NON_CRITICAL_ERRORS:
                        return {"error": ret_msg, "retCode": ret_code, "retMsg": ret_msg}
                    
                    logger.error(f"Bybit API error: {data}")
                    raise BybitAPIError(ret_code, ret_msg)
                
                if return_full_response:
                    return data
                
                return data.get("result", {})
            
            except asyncio.TimeoutError:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"Request timeout, retry {attempt + 1}/{max_retries} in {wait_time}s")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Request timeout after {max_retries} attempts")
                    raise
            
            except aiohttp.ClientError as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"Request error: {e}, retry {attempt + 1}/{max_retries} in {wait_time}s")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Request failed after {max_retries} attempts: {e}")
                    raise
            
            except BybitAPIError:
                raise
            
            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)
                raise
        
        raise Exception("Max retries exceeded")
    
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200
    ) -> List[list]:
        """
        Get kline/candle data
        
        Returns:
            List of candles [timestamp, open, high, low, close, volume, turnover]
            Returns in reverse chronological order (newest first)
        """
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        
        result = await self._request("GET", "/v5/market/kline", params)
        candles = result.get("list", [])
        
        # ===== FIX 8.2: Warning for incomplete data =====
        if len(candles) < limit * 0.9:
            logger.warning(f"[{symbol}] Received fewer candles than requested: {len(candles)}/{limit} (may indicate API issue)")
        
        # Validate candle data
        validated = []
        for candle in candles:
            if len(candle) >= 5:
                validated.append(candle)
        
        return validated
    
    async def get_ticker(self, symbol: str) -> Optional[Dict]:
        """
        Get latest ticker price
        
        Returns:
            Dict with ticker info, or None if not found
        """
        params = {
            "category": "linear",
            "symbol": symbol
        }
        
        result = await self._request("GET", "/v5/market/tickers", params)
        tickers = result.get("list", [])
        # ===== FIX 3.1: Return None instead of empty dict for better validation =====
        return tickers[0] if tickers else None
    
    async def get_instruments_info(self, symbol: str) -> Optional[Dict]:
        """
        Get instrument specifications
        
        Returns:
            Dict with instrument info, or None if not found
        """
        params = {
            "category": "linear",
            "symbol": symbol
        }
        
        result = await self._request("GET", "/v5/market/instruments-info", params)
        instruments = result.get("list", [])
        # ===== FIX 3.1: Return None instead of empty dict for better validation =====
        return instruments[0] if instruments else None
    
    async def set_leverage(self, symbol: str, leverage: int):
        """Set leverage for symbol"""
        params = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage)
        }
        
        result = await self._request("POST", "/v5/position/set-leverage", params, signed=True)
        
        if isinstance(result, dict) and result.get("retCode") == 110043:
            logger.debug(f"{symbol} leverage already set to {leverage}x")
        else:
            logger.info(f"Set {symbol} leverage to {leverage}x")
    
    async def set_margin_mode(self, symbol: str, margin_mode: str = "ISOLATED"):
        """Set margin mode (ISOLATED or CROSS)"""
        params = {
            "category": "linear",
            "symbol": symbol,
            "tradeMode": 1 if margin_mode == "CROSS" else 0,
            "buyLeverage": "0",
            "sellLeverage": "0"
        }
        
        result = await self._request("POST", "/v5/position/switch-isolated", params, signed=True)
        
        if isinstance(result, dict) and result.get("retCode") in [100028, 110043]:
            logger.debug(f"{symbol} margin mode already set to {margin_mode}")
        else:
            logger.info(f"Set {symbol} margin mode to {margin_mode}")
    
    async def get_position(self, symbol: str) -> Optional[Dict]:
        """Get current position for symbol"""
        params = {
            "category": "linear",
            "symbol": symbol
        }
        
        result = await self._request("GET", "/v5/position/list", params, signed=True)
        positions = result.get("list", [])
        
        for pos in positions:
            size = pos.get("size", "0")
            try:
                if float(size) > 0:
                    return pos
            except (ValueError, TypeError):
                continue
        return None
    
    async def get_wallet_balance(self) -> Optional[float]:
        """Get available balance in USDT for trading"""
        try:
            params = {"accountType": "UNIFIED"}
            
            result = await self._request("GET", "/v5/account/wallet-balance", params, signed=True)
            accounts = result.get("list", [])
            
            if accounts:
                account = accounts[0]
                coins = account.get("coin", [])
                
                for coin in coins:
                    if coin.get("coin") == "USDT":
                        available_str = coin.get("availableBalance", coin.get("availableToWithdraw", "0"))
                        if available_str:
                            try:
                                available_balance = float(available_str)
                                if available_balance > 0:
                                    return available_balance
                            except (ValueError, TypeError):
                                continue
                
                # Fallback to totalEquity
                total_equity_str = account.get("totalEquity", "0")
                if total_equity_str:
                    try:
                        return float(total_equity_str)
                    except (ValueError, TypeError):
                        pass
            
            return None
        except Exception as e:
            logger.error(f"Error getting wallet balance: {e}")
            return None
    
    async def get_total_equity(self) -> Optional[float]:
        """Get total equity from Bybit (includes unrealized PnL)"""
        try:
            params = {"accountType": "UNIFIED"}
            
            result = await self._request("GET", "/v5/account/wallet-balance", params, signed=True)
            accounts = result.get("list", [])
            
            if accounts:
                account = accounts[0]
                total_equity_str = account.get("totalEquity", "0")
                if total_equity_str:
                    try:
                        total_equity = float(total_equity_str)
                        if total_equity > 0:
                            return total_equity
                    except (ValueError, TypeError):
                        pass
            
            return None
        except Exception as e:
            logger.error(f"Error getting total equity: {e}")
            return None
    
    async def calculate_qty(
        self,
        symbol: str,
        size_usdt: float,
        price: float
    ) -> float:
        """
        Calculate order quantity based on USDT size
        
        Uses Decimal for precision
        
        Raises:
            ValueError: If instruments info cannot be retrieved
        """
        # ===== FIX 6.2: Validate instruments info =====
        info = await self.get_instruments_info(symbol)
        if not info or not isinstance(info, dict):
            raise ValueError(f"Cannot get instruments info for {symbol}")
        
        lot_size_filter = info.get("lotSizeFilter", {})
        if not lot_size_filter or not isinstance(lot_size_filter, dict):
            raise ValueError(f"Invalid lotSizeFilter for {symbol}")
        
        min_qty = Decimal(str(lot_size_filter.get("minOrderQty", "0.001")))
        qty_step = Decimal(str(lot_size_filter.get("qtyStep", "0.001")))
        max_qty = Decimal(str(lot_size_filter.get("maxOrderQty", "1000000")))
        
        # Calculate base quantity with Decimal precision
        qty = Decimal(str(size_usdt)) / Decimal(str(price))
        
        # Adjust to step size (round down)
        qty = (qty // qty_step) * qty_step
        
        # Ensure min/max
        qty = max(min_qty, min(qty, max_qty))
        
        return float(qty)
    
    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "Market",
        reduce_only: bool = False,
        price: Optional[float] = None
    ) -> Dict:
        """
        Place order
        
        Returns:
            Full response dict including retCode for consistent error handling
        """
        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
            "timeInForce": "GTC"
        }
        
        if reduce_only:
            params["reduceOnly"] = True
        
        if order_type == "Limit":
            if price is None:
                raise ValueError("Price is required for Limit orders")
            params["price"] = str(price)
        
        # Return full response for order placement
        url = f"{self.base_url}/v5/order/create"
        params_str = json.dumps(params)
        headers = self._get_headers(params_str)
        
        try:
            session = await self._get_session()
            async with session.post(url, json=params, headers=headers) as response:
                data = await response.json()
                
                if not data:
                    raise Exception("Empty response from Bybit API")
                
                ret_code = data.get("retCode", 0)
                if ret_code == 0:
                    logger.info(f"Order placed: {symbol} {side} {qty} {order_type}")
                else:
                    logger.warning(f"Order response: {symbol} {side} - {data.get('retMsg', 'Unknown')} (code: {ret_code})")
                
                return data
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            raise
    
    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get open orders"""
        params = {
            "category": "linear",
            "orderStatus": "New,PartiallyFilled"
        }
        
        if symbol:
            params["symbol"] = symbol
        
        result = await self._request("GET", "/v5/order/realtime", params, signed=True)
        return result.get("list", [])
    
    async def cancel_order(self, symbol: str, order_id: Optional[str] = None) -> Dict:
        """Cancel order"""
        params = {
            "category": "linear",
            "symbol": symbol
        }
        
        if order_id:
            params["orderId"] = order_id
        
        result = await self._request("POST", "/v5/order/cancel", params, signed=True)
        logger.info(f"Order cancelled: {symbol} {order_id if order_id else 'all'}")
        return result
    
    async def cancel_all_orders(self, symbol: str) -> Dict:
        """Cancel all open orders for symbol"""
        return await self.cancel_order(symbol, order_id=None)
    
    async def get_order_execution_price(self, symbol: str, order_id: str) -> Optional[float]:
        """
        Get the actual execution price for a filled order
        
        Args:
            symbol: Trading pair
            order_id: Order ID to check
            
        Returns:
            Average execution price, or None if order not found or not filled
        """
        try:
            params = {
                "category": "linear",
                "symbol": symbol,
                "orderId": order_id
            }
            
            # Try to get from execution list (most accurate)
            result = await self._request("GET", "/v5/execution/list", params, signed=True)
            executions = result.get("list", [])
            
            if executions:
                # Calculate average execution price from all fills
                total_qty = 0.0
                total_value = 0.0
                
                for exec_item in executions:
                    exec_qty_str = exec_item.get("execQty", "0")
                    exec_price_str = exec_item.get("execPrice", "0")
                    
                    try:
                        exec_qty = float(exec_qty_str)
                        exec_price = float(exec_price_str)
                        
                        if exec_qty > 0 and exec_price > 0:
                            total_qty += exec_qty
                            total_value += exec_qty * exec_price
                    except (ValueError, TypeError):
                        continue
                
                if total_qty > 0:
                    avg_execution_price = total_value / total_qty
                    return avg_execution_price
            
            # Fallback: try order history
            result = await self._request("GET", "/v5/order/history", params, signed=True)
            orders = result.get("list", [])
            
            for order in orders:
                if order.get("orderId") == order_id:
                    # Check if order is filled
                    order_status = order.get("orderStatus", "")
                    if order_status == "Filled":
                        avg_price_str = order.get("avgPrice", "0")
                        try:
                            avg_price = float(avg_price_str)
                            if avg_price > 0:
                                return avg_price
                        except (ValueError, TypeError):
                            pass
            
            return None
            
        except Exception as e:
            logger.debug(f"Error getting execution price for order {order_id}: {e}")
            return None
    
    async def get_recent_executions(self, symbol: str, limit: int = 10) -> List[Dict]:
        """
        Get recent executions for a symbol (for finding execution prices of recent trades)
        
        Args:
            symbol: Trading pair
            limit: Maximum number of executions to return (default 10)
            
        Returns:
            List of execution dicts, sorted by time (newest first)
        """
        try:
            params = {
                "category": "linear",
                "symbol": symbol,
                "limit": limit
            }
            
            result = await self._request("GET", "/v5/execution/list", params, signed=True)
            executions = result.get("list", [])
            
            # Sort by execution time (newest first)
            if executions:
                try:
                    executions.sort(key=lambda x: int(x.get("execTime", "0")), reverse=True)
                except (ValueError, TypeError):
                    pass
            
            return executions
            
        except Exception as e:
            logger.debug(f"Error getting recent executions for {symbol}: {e}")
            return []
