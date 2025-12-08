# Import BotController only, not bot_controller to avoid circular imports
from app.trading.bot_controller import BotController

__all__ = ['BotController']
