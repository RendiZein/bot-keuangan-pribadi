import os
import logging
from dotenv import load_dotenv

# Load Environment Variables
load_dotenv()

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mysecret123")

# Google Sheets
SHEET_NAME = os.getenv("SHEET_NAME")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE")

# Security
allowed_users_raw = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = [uid.strip() for uid in allowed_users_raw.split(",") if uid.strip()]

# AI APIs
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# Logging Configuration
def setup_logging():
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    # Configure Root Logger
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.FileHandler("bot.log"), # Simpan ke file
            logging.StreamHandler()         # Tampilkan di terminal
        ]
    )
    
    # Suppress noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pyngrok").setLevel(logging.WARNING)
