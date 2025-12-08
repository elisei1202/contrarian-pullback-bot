from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard"""
    return templates.TemplateResponse("dashboard.html", {"request": request})

@router.get("/positions", response_class=HTMLResponse)
async def positions_page(request: Request):
    """Positions page"""
    return templates.TemplateResponse("positions.html", {"request": request})

@router.get("/mobile", response_class=HTMLResponse)
async def mobile_dashboard(request: Request):
    """Mobile-optimized dashboard"""
    return templates.TemplateResponse("mobile.html", {"request": request})

@router.get("/symbol/{symbol}", response_class=HTMLResponse)
async def symbol_detail(request: Request, symbol: str):
    """Symbol detail page with TradingView-like chart"""
    return templates.TemplateResponse("symbol_detail.html", {"request": request, "symbol": symbol})

@router.get("/mobile/symbol/{symbol}", response_class=HTMLResponse)
async def symbol_detail_mobile(request: Request, symbol: str):
    """Mobile-optimized symbol detail page with TradingView-like chart"""
    return templates.TemplateResponse("symbol_detail_mobile.html", {"request": request, "symbol": symbol})

@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """Configuration page"""
    return templates.TemplateResponse("config.html", {"request": request})

@router.get("/api/status")
async def get_status():
    """Get bot status"""
    from app.trading.bot_controller import bot_controller
    
    if bot_controller:
        return JSONResponse(bot_controller.get_status())
    return JSONResponse({"error": "Bot not initialized"}, status_code=503)

class ToggleRequest(BaseModel):
    enabled: bool

@router.post("/api/toggle-trading")
async def toggle_trading(request: ToggleRequest):
    """Enable/disable trading"""
    from app.trading.bot_controller import bot_controller
    import logging
    logger = logging.getLogger(__name__)
    
    if bot_controller:
        old_value = bot_controller.trading_enabled
        bot_controller.trading_enabled = request.enabled
        logger.info(f"Trading toggled: {old_value} -> {request.enabled}")
        return JSONResponse({
            "success": True,
            "trading_enabled": request.enabled
        })
    return JSONResponse({"error": "Bot not initialized"}, status_code=503)


@router.get("/api/equity-history")
async def get_equity_history():
    """Get equity history for chart"""
    from app.trading.bot_controller import bot_controller
    
    if bot_controller:
        return JSONResponse({
            "history": bot_controller.equity_history[-500:]  # Last 500 points
        })
    return JSONResponse({"history": []})

@router.get("/api/trade-history")
async def get_trade_history():
    """Get trade history"""
    from app.trading.bot_controller import bot_controller
    
    if bot_controller:
        return JSONResponse({
            "trades": bot_controller.trade_history[-500:]  # Last 500 trades
        })
    return JSONResponse({"trades": []})

@router.get("/api/symbol/{symbol}/chart-data")
async def get_symbol_chart_data(symbol: str):
    """Get chart data for a symbol (candles, indicators, position info)"""
    from app.trading.bot_controller import bot_controller
    import logging
    logger = logging.getLogger(__name__)
    
    if not bot_controller:
        return JSONResponse({"error": "Bot not initialized"}, status_code=503)
    
    try:
        # Get chart data from bot_controller
        chart_data = await bot_controller.get_symbol_chart_data(symbol)
        
        # Log response details for debugging
        logger.info(f"Chart data for {symbol}: candles_4h={len(chart_data.get('candles_4h', []))}, candles_1h={len(chart_data.get('candles_1h', []))}, has_position={chart_data.get('position') is not None}")
        
        if "error" in chart_data:
            logger.error(f"Error in chart data for {symbol}: {chart_data.get('error')}")
            return JSONResponse(chart_data, status_code=404 if chart_data.get("error") == "Symbol not found" else 500)
        
        return JSONResponse(chart_data)
    except Exception as e:
        logger.error(f"Error in get_symbol_chart_data endpoint for {symbol}: {e}", exc_info=True)
        return JSONResponse({"error": f"Internal error: {str(e)}"}, status_code=500)

@router.get("/api/config")
async def get_config():
    """Get current bot configuration"""
    from app.trading.bot_controller import bot_controller
    
    if not bot_controller:
        return JSONResponse({"error": "Bot not initialized"}, status_code=503)
    
    return JSONResponse({
        "leverage": bot_controller.config.trading.leverage,
        "position_size_usdt": bot_controller.config.trading.position_size_usdt,
        "margin_mode": bot_controller.config.trading.margin_mode,
        "symbols": bot_controller.config.trading.symbols,
        "trading_enabled": bot_controller.trading_enabled
    })

class UpdateConfigRequest(BaseModel):
    leverage: int = None
    position_size_usdt: float = None
    margin_mode: str = None

@router.post("/api/config/update")
async def update_config(request: UpdateConfigRequest):
    """Update bot configuration dynamically"""
    from app.trading.bot_controller import bot_controller
    import logging
    logger = logging.getLogger(__name__)
    
    if not bot_controller:
        return JSONResponse({"error": "Bot not initialized"}, status_code=503)
    
    changes = []
    
    # Update leverage
    if request.leverage is not None:
        if request.leverage < 1 or request.leverage > 100:
            return JSONResponse({"error": "Leverage must be between 1 and 100"}, status_code=400)
        
        old_leverage = bot_controller.config.trading.leverage
        bot_controller.config.trading.leverage = request.leverage
        changes.append(f"leverage: {old_leverage} -> {request.leverage}")
        
        # Apply leverage to all symbols
        try:
            await bot_controller._setup_leverage()
            logger.info(f"Leverage updated to {request.leverage}x for all symbols")
        except Exception as e:
            logger.error(f"Error updating leverage: {e}")
            return JSONResponse({"error": f"Failed to update leverage: {str(e)}"}, status_code=500)
    
    # Update position size
    if request.position_size_usdt is not None:
        if request.position_size_usdt <= 0:
            return JSONResponse({"error": "Position size must be greater than 0"}, status_code=400)
        
        old_size = bot_controller.config.trading.position_size_usdt
        bot_controller.config.trading.position_size_usdt = request.position_size_usdt
        changes.append(f"position_size_usdt: {old_size} -> {request.position_size_usdt}")
        logger.info(f"Position size updated to {request.position_size_usdt} USDT")
    
    # Update margin mode
    if request.margin_mode is not None:
        if request.margin_mode not in ["ISOLATED", "CROSS"]:
            return JSONResponse({"error": "Margin mode must be ISOLATED or CROSS"}, status_code=400)
        
        old_mode = bot_controller.config.trading.margin_mode
        bot_controller.config.trading.margin_mode = request.margin_mode
        changes.append(f"margin_mode: {old_mode} -> {request.margin_mode}")
        
        # Apply margin mode to all symbols
        try:
            await bot_controller._setup_leverage()
            logger.info(f"Margin mode updated to {request.margin_mode} for all symbols")
        except Exception as e:
            logger.error(f"Error updating margin mode: {e}")
            return JSONResponse({"error": f"Failed to update margin mode: {str(e)}"}, status_code=500)
    
    if not changes:
        return JSONResponse({"error": "No valid configuration changes provided"}, status_code=400)
    
    return JSONResponse({
        "success": True,
        "changes": changes,
        "config": {
            "leverage": bot_controller.config.trading.leverage,
            "position_size_usdt": bot_controller.config.trading.position_size_usdt,
            "margin_mode": bot_controller.config.trading.margin_mode
        }
    })

@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return JSONResponse({"status": "healthy"})
