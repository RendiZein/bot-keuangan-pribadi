import os
import pandas as pd
import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes

from config.settings import ALLOWED_USERS
from services.sheets_service import sheets_service
from services.ai_service import ai_service
from services.transaction_service import core_process_transaction
from handlers.commands import undo_command, help_command, start_command

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ALLOWED_USERS: return

    user_input = update.message.text or ""
    
    # --- 0. MENU HANDLER ---
    if user_input == "💰 Cek Saldo":
        await proses_cek_saldo(update, context)
        return
    elif user_input == "↩️ Undo Terakhir":
        await undo_command(update, context)
        return
    elif user_input == "❓ Bantuan":
        await help_command(update, context)
        return
    elif user_input == "📊 Analisis":
        await update.message.reply_text("💡 Silakan ketik pertanyaan analisis Anda.\nContoh: _'Berapa pengeluaran makan bulan ini?'_", parse_mode="Markdown")
        return

    # 1. DETEKSI INTENT ANALISA
    keywords_analisa = [
        "analisa", "grafik", "chart", "plot", "tren", "statistik", 
        "berapa", "total", "bandingkan", "pengeluaran saya", 
        "habis berapa", "kemarin", "bulan ini", "minggu ini"
    ]
    
    is_analisa = any(k in user_input.lower() for k in keywords_analisa)
    
    if is_analisa and len(user_input.split()) > 1:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        msg = await update.message.reply_text("🧠 Sedang menganalisis data (PandasAI)...")
        
        try:
            wks = sheets_service.get_sheet()
            data = await asyncio.to_thread(wks.get_all_records)
            
            if not data:
                await msg.edit_text("❌ Data kosong.")
                return

            df = pd.DataFrame(data)
            df.columns = [str(c).lower().strip() for c in df.columns]
            
            if 'tanggal' in df.columns:
                df['tanggal'] = pd.to_datetime(df['tanggal'], errors='coerce')
            
            candidates = [c for c in df.columns if ('total' in c or 'amount' in c or 'harga' in c) and 'satuan' not in c]
            col_harga = candidates[0] if candidates else df.columns[-1]
            
            df[col_harga] = df[col_harga].astype(str).str.replace(r'[^\d-]', '', regex=True)
            df[col_harga] = pd.to_numeric(df[col_harga], errors='coerce').fillna(0)

            hasil = await ai_service.run_analysis(user_input, df)
            
            if hasil['type'] == 'image':
                await update.message.reply_photo(photo=open(hasil['path'], 'rb'), caption="📊 Grafik Analisis")
                await msg.delete()
                try: os.remove(hasil['path'])
                except: pass
            elif hasil['type'] == 'text':
                await msg.edit_text(f"💡 **Hasil:**\n{hasil['content']}", parse_mode="Markdown")
            else:
                await msg.edit_text(hasil['content'])

        except Exception as e:
            logging.error(f"ERROR ANALISA: {e}")
            await msg.edit_text(f"Gagal analisa: {str(e)}")
        return 

    # 2. CEK SALDO
    keywords_saldo = ["saldo", "cek uang", "dompet", "keuanganku", "sisa uang"]
    if any(k in user_input.lower() for k in keywords_saldo):
        await proses_cek_saldo(update, context)
        return

    # 3. CATAT TRANSAKSI
    try:
        await proses_catat_transaksi(update, context)
    except Exception as e:
        logging.error(f"Error Transaksi: {e}")
        await update.message.reply_text("🤔 Saya tidak mengerti. Apakah ini transaksi atau pertanyaan analisis?")

async def proses_cek_saldo(update, context):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    msg = await update.message.reply_text("🔍 Menghitung aset...")
    
    try:
        wks = sheets_service.get_sheet()
        if not wks:
            await msg.edit_text("❌ Database error.")
            return
            
        data = await asyncio.to_thread(wks.get_all_records)
        if not data:
            await msg.edit_text("Belum ada data.")
            return

        df = pd.DataFrame(data)
        df.columns = [c.lower().strip() for c in df.columns]
        
        col_harga = next((c for c in df.columns if 'total' in c or 'amount' in c or 'harga' in c if 'satuan' not in c), None)
        
        if not col_harga:
            await msg.edit_text("❌ Kolom harga tidak ditemukan.")
            return

        total_aset = 0
        kantongs = df['kantong'].unique()
        report = "💰 **Kondisi Keuangan**\n"
        
        for k in kantongs:
            if not k: continue
            df_k = df[df['kantong'] == k].copy()
            df_k[col_harga] = pd.to_numeric(df_k[col_harga].astype(str).str.replace(r'[^\d-]', '', regex=True), errors='coerce').fillna(0)
            
            masuk = df_k[df_k['tipe'].str.lower() == 'masuk'][col_harga].sum()
            keluar = df_k[df_k['tipe'].str.lower() == 'keluar'][col_harga].sum()
            saldo = masuk - keluar
            
            total_aset += saldo
            report += f"\n🏦 **{k}:** Rp {saldo:,.0f}"

        report += f"\n\n💎 **Total:** Rp {total_aset:,.0f}"
        await msg.edit_text(report, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Error Saldo: {e}")

async def proses_catat_transaksi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⚡ Memproses...")
    
    try:
        media_path = None
        user_text_input = update.message.text or ""

        if update.message.voice:
            voice_file = await update.message.voice.get_file()
            media_path = "temp_audio.ogg"
            await voice_file.download_to_drive(media_path)
            
            with open(media_path, "rb") as file:
                transcription = await asyncio.to_thread(
                    ai_service.groq_client.audio.transcriptions.create,
                    file=(media_path, file.read()),
                    model="whisper-large-v3-turbo",
                    response_format="json"
                )
            user_text_input = transcription.text
            await msg.edit_text(f"🗣️: \"{user_text_input}\"")
            try: os.remove(media_path)
            except: pass
            media_path = None

        elif update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
            media_path = "temp_image.jpg"
            await photo_file.download_to_drive(media_path)

        report_text = await core_process_transaction(user_text_input, media_path, source_info="Telegram")
        
        if media_path:
            try: os.remove(media_path)
            except: pass
        
        await msg.edit_text(report_text)

    except Exception as e:
        logging.error(f"Error Handler: {e}")
        await msg.edit_text(f"❌ Error: {str(e)}")
