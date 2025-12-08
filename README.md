# 🎯 Contrarian Pullback Trading Bot

**Strategy:** Enter positions when 1H SuperTrend shows OPPOSITE direction of 4H trend - catching pullbacks at the best price!

---

## 📊 Strategy Overview

### Concept
This bot combines **trend-following** with **contrarian entries**:
- **4H Timeframe**: Identifies the main trend (EMA 200 + SuperTrend)
- **1H Timeframe**: Enters when price pulls back (SuperTrend shows opposite)

### Why This Works

**LONG Example:**
```
4H: BULLISH trend (close > EMA200, ST green)
1H: Shows RED (price pullback) ← ENTER LONG HERE!
Result: You buy at a better price during temporary weakness
```

**SHORT Example:**
```
4H: BEARISH trend (close < EMA200, ST red)
1H: Shows GREEN (price bounce) ← ENTER SHORT HERE!
Result: You sell at a better price during temporary strength
```

### Entry Rules

**LONG Entry:**
- 4H: Close > EMA 200 ✓
- 4H: SuperTrend = GREEN ✓
- 1H: SuperTrend = RED (OPPOSITE!) ← Entry trigger

**SHORT Entry:**
- 4H: Close < EMA 200 ✓
- 4H: SuperTrend = RED ✓
- 1H: SuperTrend = GREEN (OPPOSITE!) ← Entry trigger

### Exit Rules

**Exit LONG:**
- 1H SuperTrend flips back to GREEN (pullback ended)
- OR 4H trend changes to BEARISH

**Exit SHORT:**
- 1H SuperTrend flips back to RED (pullback ended)
- OR 4H trend changes to BULLISH

---

## 🚀 Quick Start

### Prerequisites
- Python 3.11+
- Bybit account with API keys
- GitHub account (for deployment)

### Local Development

1. **Clone/Download** this bot

2. **Install dependencies:**
```bash
pip install -r requirements.txt
```

3. **Setup environment:**
```bash
cp .env.example .env
# Edit .env with your API keys
```

4. **Run locally:**
```bash
python main.py
```

5. **Access dashboard:**
```
http://localhost:10000
```

---

## ☁️ Deploy to Render (Recommended)

### Step 1: Push to GitHub

1. Create new repository on GitHub
2. Push this code:
```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/contrarian-bot.git
git push -u origin main
```

### Step 2: Deploy on Render

1. Go to [Render Dashboard](https://dashboard.render.com)
2. Click **"New +"** → **"Blueprint"**
3. Connect your GitHub repository
4. Render will detect `render.yaml` automatically
5. Enter your Bybit API credentials:
   - `BYBIT_API_KEY`
   - `BYBIT_API_SECRET`
6. Click **"Apply"**

**Your bot will be live at:**
```
https://contrarian-pullback-bot.onrender.com
```

---

## ⚙️ Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BYBIT_API_KEY` | - | Your Bybit API key (required) |
| `BYBIT_API_SECRET` | - | Your Bybit API secret (required) |
| `BYBIT_TESTNET` | false | Use testnet (true/false) |
| `SYMBOLS` | BTCUSDT,... | Trading pairs (comma-separated) |
| `POSITION_SIZE_USDT` | 100 | USDT per trade |
| `LEVERAGE` | 10 | Leverage (Isolated margin) |
| `EMA_PERIOD_4H` | 200 | EMA period for trend filter |
| `ST_PERIOD_4H` | 10 | SuperTrend period (4H) |
| `ST_MULTIPLIER_4H` | 3.0 | SuperTrend multiplier (4H) |
| `ST_PERIOD_1H` | 10 | SuperTrend period (1H) |
| `ST_MULTIPLIER_1H` | 3.0 | SuperTrend multiplier (1H) |

### Symbols

Default symbols (top 5 by volume):
- BTCUSDT
- ETHUSDT
- BNBUSDT
- SOLUSDT
- XRPUSDT

To change, edit `SYMBOLS` in `.env` or Render environment variables.

---

## 📱 Features

### Dashboard (`/`)
- Real-time status for all symbols
- 4H trend indicator (EMA200 + SuperTrend)
- 1H entry signal (contrarian detection)
- Position tracking with live PnL
- **WebSocket connection status indicator**
- **Real-time price updates (< 100ms latency)**
- Toggle trading ON/OFF
- Force close all positions

### Mobile View (`/mobile`)
- Touch-optimized interface
- Quick symbol overview
- Trading controls

### API Endpoints
- `GET /api/status` - Bot status
- `POST /api/toggle-trading` - Enable/disable trading
- `POST /api/close-all` - Close all positions
- `GET /health` - Health check

---

## 📈 Example Trade Flow

### LONG Trade

```
Time: 00:00 - 4H Check
├─ BTC = 45,000$
├─ EMA200 = 43,000$ → Close > EMA ✓
├─ ST 4H = GREEN ✓
└─ Trend = BULLISH

Time: 03:00 - 1H Check
├─ BTC = 44,500$ (pullback)
├─ ST 1H = RED ← OPPOSITE!
└─ 🎯 ENTER LONG at 44,500$

Time: 06:00 - 1H Check
├─ BTC = 44,800$
├─ ST 1H FLIP → GREEN
└─ 🚪 EXIT LONG at 44,800$
    Profit: +300$ (0.67%)
```

### SHORT Trade

```
Time: 00:00 - 4H Check
├─ ETH = 2,500$
├─ EMA200 = 2,700$ → Close < EMA ✓
├─ ST 4H = RED ✓
└─ Trend = BEARISH

Time: 02:00 - 1H Check
├─ ETH = 2,550$ (bounce)
├─ ST 1H = GREEN ← OPPOSITE!
└─ 🎯 ENTER SHORT at 2,550$

Time: 05:00 - 1H Check
├─ ETH = 2,480$
├─ ST 1H FLIP → RED
└─ 🚪 EXIT SHORT at 2,480$
    Profit: +70$ (2.75%)
```

---

## ⚠️ Risk Warning

**IMPORTANT:** This bot trades with REAL MONEY on live markets.

### Risk Factors
- **Leverage 10x** = Moderate risk, moderate reward
- **No stop loss** by default (exits on ST flip)
- **Pullbacks can extend** before reversing
- **Whipsaws** possible in ranging markets

### Recommendations
1. **Start with appropriate position sizes** ($100 default)
2. **Test on testnet first** (`BYBIT_TESTNET=true`)
3. **Monitor regularly** - automated ≠ unattended
4. **Understand the strategy** before deploying
5. **Never trade more than you can afford to lose**

---

## 🔧 Technical Details

### Architecture
- **FastAPI** - Web framework
- **Async/await** - Concurrent symbol processing
- **Bybit V5 API** - Latest API version
- **Pandas** - Indicator calculations
- **No external dependencies** for indicators

### Indicators
- **EMA 200** - Exponential Moving Average (trend direction)
- **SuperTrend** - Custom implementation (trend + volatility)

### Timeframes
- **4H candles** - Updated every 4 hours
- **1H candles** - Checked every 5 minutes

### Order Type
- **Market orders only** - Instant execution
- **Reduce-only for exits** - Closes positions safely

---

## 🐛 Troubleshooting

### Bot won't start
- Check API keys are correct
- Verify API keys have trading permissions
- Check Bybit API is accessible

### No trades executing
- Check `trading_enabled` is ON
- Verify symbols have sufficient volume
- Check leverage is set correctly

### Render deployment fails
- Check `PYTHON_VERSION` is 3.11.7
- Verify all required env vars are set
- Check logs in Render dashboard

---

## 📝 Changelog

### v1.1.0 (Current - WEBSOCKET ADDED)
- 🟢 Added WebSocket for real-time price updates
- ✅ Auto-fallback to REST API if WebSocket fails
- ✅ Faster exit signals on price movements
- ✅ Dashboard shows WebSocket connection status
- ✅ Auto-reconnect with exponential backoff
- ✅ Reduced API rate limit usage

### v1.0.1 (FIXED)
- ✅ Fixed config.bot references (critical)
- ✅ Added position sync on startup
- ✅ Added retry logic with exponential backoff
- ✅ Added ticker validation (prevent KeyError)
- ✅ Added config validation on startup
- ✅ Added order placement verification
- ✅ Removed unused imports
- ✅ Added error handling improvements

### v1.0.0
- Initial release
- Contrarian pullback strategy
- Multi-symbol support (5 symbols)
- Web dashboard
- Render deployment ready

---

## 📄 License

MIT License - Use at your own risk

---

## 🤝 Credits

**Strategy Inspiration:**
- Trend-following: Classic EMA + SuperTrend
- Contrarian entries: Mean reversion + pullback trading

**Built with:**
- FastAPI, Pandas, Bybit API V5

---

## 📞 Support

For issues or questions:
1. Check this README thoroughly
2. Review Render logs for errors
3. Verify API keys and permissions
4. Test on testnet first

---

## 🎯 Strategy Summary

**In one sentence:**
> "Trade with the 4H trend, enter when 1H shows the opposite (pullback), exit when 1H aligns back with trend."

**Why it works:**
- Catches pullbacks at best prices
- Trend filter reduces false signals
- Mean reversion + trend-following combined

**Best for:**
- Trending markets (strong 4H direction)
- Volatile symbols (clear pullbacks)
- Automated execution (no emotions)

---

**Happy Trading! 🚀**

Remember: Past performance does not guarantee future results. Always trade responsibly.
