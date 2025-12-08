import asyncio
import logging
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from fastapi import FastAPI
import uvicorn

# Load environment variables from .env file
load_dotenv()

from app.config import config
from app.trading.bot_controller import BotController
from app.web.routes import router
import app.trading.bot_controller as bot_module

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for FastAPI"""
    # Startup
    logger.info("Starting Contrarian Pullback Bot...")
    
    # Initialize bot
    bot = BotController()
    bot_module.bot_controller = bot
    
    # Start bot in background
    bot_task = asyncio.create_task(bot.start())
    
    yield
    
    # Shutdown - graceful stop first
    logger.info("Shutting down...")
    await bot.stop()
    
    # Wait for graceful shutdown
    await asyncio.sleep(2)
    
    # Cancel task if still running
    if not bot_task.done():
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
    
    # Close API client session
    if hasattr(bot.client, 'close'):
        await bot.client.close()
    
    logger.info("Shutdown complete")

# Create FastAPI app
app = FastAPI(
    title="Contrarian Pullback Bot",
    description="Enter when 1H shows opposite of 4H trend",
    version="1.0.0",
    lifespan=lifespan
)

# Include routes
app.include_router(router)

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=config.port,
        log_level="info"
    )
