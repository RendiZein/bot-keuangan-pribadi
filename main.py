import uvicorn
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Header
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from config.settings import TELEGRAM_TOKEN, WEBHOOK_SECRET, ALLOWED_USERS, setup_logging
from handlers.commands import start_command, help_command, undo_command, reset_command, setsaldo_command
from handlers.messages import handle_message
from services.transaction_service import core_process_transaction

# Initialize Logging
setup_logging()

# Setup Telegram Application
ptb_application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle Manager for FastAPI and Telegram Bot."""
    # 1. Register Handlers
    ptb_application.add_handler(CommandHandler("start", start_command))
    ptb_application.add_handler(CommandHandler("menu", start_command))
    ptb_application.add_handler(CommandHandler("help", help_command))
    ptb_application.add_handler(CommandHandler("undo", undo_command))
    ptb_application.add_handler(CommandHandler("reset", reset_command))
    ptb_application.add_handler(CommandHandler("setsaldo", setsaldo_command))
    
    filter_all = filters.TEXT | filters.PHOTO | filters.VOICE
    ptb_application.add_handler(MessageHandler(filter_all & (~filters.COMMAND), handle_message))
    
    # 2. Start Bot
    await ptb_application.initialize()
    await ptb_application.start()
    await ptb_application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    logging.info("🚀 Bot Hybrid (Telegram + Webhook) STARTED!")
    
    yield
    
    # 3. Stop Bot
    logging.info("🛑 Stopping Bot...")
    await ptb_application.updater.stop()
    await ptb_application.stop()
    await ptb_application.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook/macrodroid")
async def macrodroid_webhook(request: Request, x_secret_token: str = Header(None)):
    """Endpoint for external notifications (e.g., MacroDroid)."""
    if x_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid Secret Token")
        
    try:
        data = await request.json()
        text_input = data.get("text", "")
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON")
        
    if not text_input:
        return {"status": "ignored", "message": "Empty text"}
        
    result_text = await core_process_transaction(text_input, source_info="MacroDroid")
    
    # Hanya kirim notifikasi jika ada hasil (tidak kosong)
    if result_text and ALLOWED_USERS:
        for user_id in ALLOWED_USERS:
            try:
                await ptb_application.bot.send_message(
                    chat_id=user_id, 
                    text=f"📩 **Notif Masuk:**\n{result_text}", 
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.error(f"Gagal kirim notif telegram ke {user_id}: {e}")
            
    return {"status": "success", "result": result_text}

@app.get("/")
async def root():
    return {"status": "running", "bot": "Gemini Finance Bot"}

if __name__ == '__main__':
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
