# Contrarian Pullback Bot - Changelog

## Version 2.0.0 - Major Bug Fixes and Improvements

### 🔴 CRITICAL FIXES

#### 1. Race Condition Prevention (bot_controller.py)
- **Added `asyncio.Lock()` for critical sections:**
  - `_entry_lock` - Prevents multiple entries from passing balance check simultaneously
  - `_state_lock` - Protects position state modifications
  - `_file_lock` - Protects file I/O operations
  - `_price_lock` - Protects realtime prices dictionary

- **Changed parallel to sequential processing:**
  ```python
  # Before: Race condition possible
  await asyncio.gather(*[self._process_symbol(s) for s in symbols])
  
  # After: Sequential processing prevents balance race
  for symbol in symbols:
      await self._process_symbol(symbol)
  ```

#### 2. Exit Signal Logic Fixed (contrarian_entry.py)
- **Added immediate exit check** - Exits position if current 4H SuperTrend is opposite to position, regardless of flip detection
- **Before:** Only checked for flip (prev_direction != current_direction)
- **After:** Also exits if LONG position + red ST, or SHORT position + green ST

#### 3. Duplicate Entry Prevention (bot_controller.py)
- **Added `_processed_candles` dictionary** to track last processed candle timestamp
- Prevents same candle from triggering entry multiple times after WebSocket reconnect

#### 4. Position State Reset (state_machine.py)
- **Added `reset_position()` method** - Properly resets all position-related state including `partial_tp_done`
- **Fixed:** When position closed externally, all flags are now properly cleared

### 🟡 HIGH PRIORITY FIXES

#### 5. Circuit Breaker Implementation (bot_controller.py)
- **Added automatic pause after 5 consecutive API failures**
- Pauses trading for 5 minutes to prevent cascade failures
- Auto-resets on successful API call

#### 6. Consistent Error Handling (bybit_client.py)
- **Added `BybitAPIError` exception class**
- Standardized error code handling:
  - Non-critical errors (110043, 110007, etc.) return dict with retCode
  - Critical errors raise BybitAPIError
  - Retry errors (10002, 10006, etc.) trigger automatic retry

#### 7. Increased Balance Buffer (bot_controller.py)
- **Increased margin buffer from 20% to 50%**
- Prevents insufficient balance errors in volatile markets
```python
# Before
required_margin = (position_size / leverage) * 1.2

# After
required_margin = (position_size / leverage) * 1.5
```

#### 8. Atomic File Writes (bot_controller.py)
- **Write to temp file, then rename** - Prevents file corruption on crash
- Added async file saving with locks

### 🟢 MEDIUM PRIORITY FIXES

#### 9. Centralized TP Calculation (bot_controller.py)
- **Created `_calculate_tp_target_profit()` method**
- Single source of truth for margin + fees calculation
- Eliminates code duplication and potential divergence

#### 10. Input Validation (supertrend.py, ema.py)
- **Added `validate_candles()` function**
- Validates candle format before calculations
- Raises `ValueError` with clear message for invalid data

#### 11. Safe Division (bot_controller.py)
- **Added zero-check before division in PnL calculation**
```python
position_value = entry_price * size if entry_price and size else 0
pnl_percent = (pnl / position_value * 100) if position_value > 0 else 0
```

#### 12. Decimal Precision (bybit_client.py, bot_controller.py)
- **Using `Decimal` for quantity and price calculations**
- Prevents floating-point precision errors

#### 13. WebSocket Improvements (websocket.py)
- **Added reconnection counter and max attempts**
- **Proper callback error handling** - Errors in callbacks don't crash listener
- **Improved cleanup** - Sessions and connections properly closed

### 🔵 LOW PRIORITY FIXES

#### 14. Graceful Shutdown (main.py)
- **Added 2-second wait after stop()** before canceling task
- **Close API client session** on shutdown

#### 15. Timeout on Startup (bot_controller.py)
- **Added 60-second timeout** for leverage setup and position sync
- Prevents startup hang if API is slow

#### 16. Status Improvements (bot_controller.py)
- **Added `circuit_breaker_active` and `api_failure_count`** to status
- Better monitoring visibility

#### 17. Candle Order Handling (supertrend.py, ema.py)
- **Sort by timestamp** regardless of input order
- Works correctly with both REST (newest first) and WebSocket (oldest first) data

### 📁 Files Modified

| File | Changes |
|------|---------|
| `app/trading/bot_controller.py` | Major rewrite - locks, circuit breaker, centralized TP |
| `app/strategy/state_machine.py` | Added `reset_position()`, input validation |
| `app/strategy/contrarian_entry.py` | Fixed exit logic, added signal strength |
| `app/exchange/bybit_client.py` | Added error class, Decimal precision, session reuse |
| `app/exchange/websocket.py` | Reconnection improvements, callback safety |
| `app/indicators/supertrend.py` | Input validation, sort by timestamp |
| `app/indicators/ema.py` | Input validation, series calculation |
| `app/exchange/__init__.py` | Export `BybitAPIError` |
| `main.py` | Graceful shutdown |

### ⚠️ Breaking Changes
- None - All changes are backward compatible

### 🧪 Testing Recommendations
1. Test with paper trading first
2. Monitor circuit breaker behavior
3. Verify TP orders are placed correctly
4. Check exit signals on SuperTrend flip
5. Test WebSocket reconnection
