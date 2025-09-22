import os
import asyncio
import logging
import aiohttp
import json
from datetime import datetime
from typing import Set, Dict, Any
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import signal
import sys
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress httpx INFO logs to reduce spam
logging.getLogger("httpx").setLevel(logging.WARNING)

class RedditBanMonitor:
    def __init__(self):
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.reddit_client_id = os.getenv('REDDIT_CLIENT_ID')
        self.reddit_client_secret = os.getenv('REDDIT_CLIENT_SECRET')
        self.reddit_user_agent = os.getenv('REDDIT_USER_AGENT', 'BanMonitor/1.0')
        self.check_interval = int(os.getenv('CHECK_INTERVAL_MINUTES', '30'))
        
        self.monitored_users: Set[str] = set()
        self.user_status: Dict[str, bool] = {}  # True = active, False = banned
        self.reddit_token = None
        self.session = None
        self.monitoring_task = None
        self.application = None
        
        # Validate required environment variables
        self._validate_config()
    
    def _validate_config(self):
        required_vars = [
            'TELEGRAM_BOT_TOKEN',
            'TELEGRAM_CHAT_ID', 
            'REDDIT_CLIENT_ID',
            'REDDIT_CLIENT_SECRET'
        ]
        
        missing_vars = [var for var in required_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
    
    async def initialize(self):
        """Initialize the bot components"""
        self.session = aiohttp.ClientSession()
        
        # Get Reddit OAuth token
        await self._get_reddit_token()
        
        # Initialize Telegram bot application
        self.application = Application.builder().token(self.bot_token).build()
        
        # Add command handlers
        self.application.add_handler(CommandHandler("start", self._start_command))
        self.application.add_handler(CommandHandler("add", self._add_user_command))
        self.application.add_handler(CommandHandler("remove", self._remove_user_command))
        self.application.add_handler(CommandHandler("list", self._list_users_command))
        self.application.add_handler(CommandHandler("status", self._status_command))
        
        # Initialize the application
        await self.application.initialize()
        
        logger.info("Bot initialized successfully")
    
    async def start(self):
        """Start the bot and monitoring"""
        try:
            # Start the application
            await self.application.start()
            
            # Start monitoring task
            self.monitoring_task = asyncio.create_task(self._monitoring_loop())
            
            # Start polling
            logger.info("Starting Reddit Ban Monitor Bot...")
            
            # Send startup message to Telegram
            await self._send_telegram_message("Reddit Ban Monitor Bot started successfully!")
            
            await self.application.updater.start_polling()
            
            # Keep the bot running
            try:
                await asyncio.Event().wait()  # Wait indefinitely
            except asyncio.CancelledError:
                logger.info("Bot shutdown requested")
                
        except Exception as e:
            logger.error(f"Error in start: {e}")
            raise
    
    async def stop(self):
        """Stop the bot and cleanup"""
        logger.info("Stopping bot...")
        
        if self.monitoring_task and not self.monitoring_task.done():
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
        
        if self.application:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
        
        if self.session:
            await self.session.close()
        
        logger.info("Bot stopped successfully")
    
    async def _get_reddit_token(self):
        """Get OAuth token from Reddit API"""
        auth = aiohttp.BasicAuth(self.reddit_client_id, self.reddit_client_secret)
        headers = {'User-Agent': self.reddit_user_agent}
        data = {'grant_type': 'client_credentials'}
        
        try:
            async with self.session.post(
                'https://www.reddit.com/api/v1/access_token',
                auth=auth,
                headers=headers,
                data=data
            ) as response:
                if response.status == 200:
                    token_data = await response.json()
                    self.reddit_token = token_data['access_token']
                    logger.info("Successfully obtained Reddit OAuth token")
                else:
                    logger.error(f"Failed to get Reddit token: {response.status}")
                    raise Exception("Failed to authenticate with Reddit")
        except Exception as e:
            logger.error(f"Error getting Reddit token: {e}")
            raise
    
    async def _check_user_status(self, username: str) -> bool:
        """Check if a Reddit user is banned or suspended"""
        if not self.reddit_token:
            await self._get_reddit_token()
        
        headers = {
            'Authorization': f'Bearer {self.reddit_token}',
            'User-Agent': self.reddit_user_agent
        }
        
        try:
            async with self.session.get(
                f'https://oauth.reddit.com/user/{username}/about',
                headers=headers
            ) as response:
                if response.status == 200:
                    user_data = await response.json()
                    # If we can access user data, account is active
                    return True
                elif response.status == 404:
                    # User not found - could be banned/suspended
                    return False
                else:
                    logger.warning(f"Unexpected status {response.status} for user {username}")
                    return True  # Assume active if unclear
        except Exception as e:
            logger.error(f"Error checking user {username}: {e}")
            return True  # Assume active on error
    
    async def _send_telegram_message(self, message: str):
        """Send message to Telegram chat"""
        try:
            url = f'https://api.telegram.org/bot{self.bot_token}/sendMessage'
            data = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            
            async with self.session.post(url, json=data) as response:
                if response.status != 200:
                    logger.error(f"Failed to send Telegram message: {response.status}")
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")
    
    async def _monitoring_loop(self):
        """Main monitoring loop"""
        while True:
            try:
                if not self.monitored_users:
                    await asyncio.sleep(60)  # Check every minute if no users
                    continue
                
                logger.info(f"Checking {len(self.monitored_users)} users for ban status...")
                
                for username in self.monitored_users.copy():
                    try:
                        is_active = await self._check_user_status(username)
                        previous_status = self.user_status.get(username, True)
                        
                        # Status changed from active to banned
                        if previous_status and not is_active:
                            message = f"<b>ACCOUNT BANNED</b>\n\nUsername: u/{username}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\nThe account appears to be banned or suspended."
                            await self._send_telegram_message(message)
                            logger.info(f"User {username} has been banned - notification sent")
                        
                        # Status changed from banned to active (rare but possible)
                        elif not previous_status and is_active:
                            message = f"<b>ACCOUNT RESTORED</b>\n\nUsername: u/{username}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\nThe account is now active again."
                            await self._send_telegram_message(message)
                            logger.info(f"User {username} has been restored - notification sent")
                        
                        self.user_status[username] = is_active
                        
                        # Small delay between checks
                        await asyncio.sleep(2)
                        
                    except Exception as e:
                        logger.error(f"Error checking user {username}: {e}")
                
                # Wait for next check cycle
                await asyncio.sleep(self.check_interval * 60)
                
            except asyncio.CancelledError:
                logger.info("Monitoring loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(60)
    
    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        message = (
            "<b>Reddit Ban Monitor Bot</b>\n\n"
            "Commands:\n"
            "/add &lt;username&gt; - Add user to monitor\n"
            "/remove &lt;username&gt; - Remove user from monitoring\n"
            "/list - Show monitored users\n"
            "/status - Show bot status\n\n"
            "The bot will notify you when monitored accounts get banned or suspended."
        )
        await update.message.reply_text(message, parse_mode='HTML')
    
    async def _add_user_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /add command"""
        if not context.args:
            await update.message.reply_text("Usage: /add &lt;username&gt;", parse_mode='HTML')
            return
        
        username = context.args[0].replace('u/', '').replace('/u/', '')
        
        if username in self.monitored_users:
            await update.message.reply_text(f"User u/{username} is already being monitored.")
            return
        
        # Check if user exists
        is_active = await self._check_user_status(username)
        
        self.monitored_users.add(username)
        self.user_status[username] = is_active
        
        status_text = "Active" if is_active else "Banned/Suspended"
        message = f"Added u/{username} to monitoring list.\nCurrent status: {status_text}"
        await update.message.reply_text(message)
        
        logger.info(f"Added user {username} to monitoring (Status: {'Active' if is_active else 'Banned'})")
    
    async def _remove_user_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /remove command"""
        if not context.args:
            await update.message.reply_text("Usage: /remove &lt;username&gt;", parse_mode='HTML')
            return
        
        username = context.args[0].replace('u/', '').replace('/u/', '')
        
        if username not in self.monitored_users:
            await update.message.reply_text(f"User u/{username} is not being monitored.")
            return
        
        self.monitored_users.remove(username)
        if username in self.user_status:
            del self.user_status[username]
        
        await update.message.reply_text(f"Removed u/{username} from monitoring list.")
        logger.info(f"Removed user {username} from monitoring")
    
    async def _list_users_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /list command"""
        if not self.monitored_users:
            await update.message.reply_text("No users are currently being monitored.")
            return
        
        message = "<b>Monitored Users:</b>\n\n"
        for username in sorted(self.monitored_users):
            status = self.user_status.get(username, True)
            status_text = "Active" if status else "Banned"
            message += f"u/{username} - {status_text}\n"
        
        await update.message.reply_text(message, parse_mode='HTML')
    
    async def _status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        active_count = sum(1 for status in self.user_status.values() if status)
        banned_count = len(self.user_status) - active_count
        
        message = (
            f"<b>Bot Status:</b>\n\n"
            f"Total monitored: {len(self.monitored_users)}\n"
            f"Active accounts: {active_count}\n"
            f"Banned accounts: {banned_count}\n"
            f"Check interval: {self.check_interval} minutes\n"
            f"Last check: {datetime.now().strftime('%H:%M:%S UTC')}"
        )
        await update.message.reply_text(message, parse_mode='HTML')

# Global bot instance for signal handling
bot_instance = None

async def main():
    global bot_instance
    
    try:
        bot_instance = RedditBanMonitor()
        await bot_instance.initialize()
        await bot_instance.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        if bot_instance:
            await bot_instance.stop()

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}")
    sys.exit(0)

if __name__ == "__main__":
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run the bot
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application terminated by user")
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")