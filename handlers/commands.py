import pandas as pd
import asyncio
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes

from config.settings import ALLOWED_USERS
from services.sheets_service import sheets_service
from utils.helpers import clean_for_json

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan menu utama dengan tombol."""
    keyboard = [
        [KeyboardButton("💰 Cek Saldo"), KeyboardButton("📊 Analisis")],
        [KeyboardButton("↩️ Undo Terakhir"), KeyboardButton("❓ Bantuan")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("🤖 **Menu Utama:**\nSilakan pilih opsi atau ketik transaksi langsung.", reply_markup=reply_markup, parse_mode="Markdown")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ALLOWED_USERS: 
        await update.message.reply_text("⛔ Akses Ditolak.")
        return
    await show_menu(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
    📚 **Panduan Bot Keuangan**
    
    1. **Catat Transaksi**:
       - Ketik: "Beli nasi goreng 15rb"
       - Kirim Foto Struk
       - Kirim Voice Note
       
    2. **Perintah**:
       - `/setsaldo [Kantong] [Jumlah]` : Koreksi saldo
       - `/undo` : Hapus transaksi terakhir
       - `/reset confirm` : Hapus SEMUA data
       
    3. **Analisis**:
       - "Pengeluaran bulan ini berapa?"
       - "Grafik makan minggu lalu"
    """
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ALLOWED_USERS: return
    
    msg = await update.message.reply_text("⏳ Undo transaksi terakhir...")
    wks = sheets_service.get_sheet()
    if not wks:
        await msg.edit_text("❌ Database error.")
        return

    try:
        # Ambil semua data untuk mendapatkan index baris terakhir yang terisi
        # get_all_values lebih aman daripada row_count (yang mengembalikan total grid, termasuk baris kosong)
        all_values = await asyncio.to_thread(wks.get_all_values)
        num_rows = len(all_values)
        
        # Asumsi baris 1 adalah header, jadi jangan hapus jika rows <= 1
        if num_rows <= 1:
            await msg.edit_text("⚠️ Data kosong (hanya header).")
            return

        last_row_data = all_values[-1] # Data baris terakhir
        last_item = "Item"
        if len(last_row_data) > 4:
            last_item = last_row_data[4]

        # Hapus baris terakhir yang memiliki data
        await asyncio.to_thread(wks.delete_rows, num_rows)
        await msg.edit_text(f"✅ **Undo:** _{last_item}_ dihapus.", parse_mode="Markdown")
    except Exception as e:
        import traceback
        logging.error(traceback.format_exc())
        await msg.edit_text(f"❌ Error Undo: {str(e)}")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ALLOWED_USERS: return
    if not context.args or context.args[0].lower() != 'confirm':
        await update.message.reply_text("⚠️ **BAHAYA!** Ketik `/reset confirm` untuk menghapus SEMUA data.", parse_mode="Markdown")
        return

    msg = await update.message.reply_text("⏳ Mereset database...")
    wks = sheets_service.get_sheet()
    if not wks: return

    try:
        await asyncio.to_thread(wks.batch_clear, ["A2:J"])
        await msg.edit_text("♻️ **Database Bersih!** (Header aman).")
    except Exception as e:
        await msg.edit_text(f"❌ Gagal: {e}")

async def setsaldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ALLOWED_USERS: return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("⚠️ Format salah.\nGunakan: `/setsaldo [NamaKantong] [JumlahUang]`\nContoh: `/setsaldo BCA 1500000`", parse_mode="Markdown")
        return

    target_kantong = context.args[0]
    try:
        target_saldo = int(str(context.args[1]).replace(".", "").replace(",", ""))
    except:
        await update.message.reply_text("❌ Jumlah uang harus angka.")
        return

    msg = await update.message.reply_text("🧮 Menghitung selisih...")
    wks = sheets_service.get_sheet()
    if not wks: return

    try:
        data = await asyncio.to_thread(wks.get_all_records)
        df = pd.DataFrame(data)
        df.columns = [str(c).lower().strip() for c in df.columns]
        
        col_harga = next((c for c in df.columns if 'total' in c or 'amount' in c or 'harga' in c if 'satuan' not in c), None)
        
        if not col_harga or df.empty:
            current_saldo = 0
        else:
            mask = df['kantong'].astype(str).str.lower() == target_kantong.lower()
            df_k = df.loc[mask].copy()
            df_k[col_harga] = pd.to_numeric(df_k[col_harga].astype(str).str.replace(r'[^\d-]', '', regex=True), errors='coerce').fillna(0)
            
            masuk = df_k[df_k['tipe'].str.lower() == 'masuk'][col_harga].sum()
            keluar = df_k[df_k['tipe'].str.lower() == 'keluar'][col_harga].sum()
            current_saldo = int(masuk - keluar)

        selisih = target_saldo - current_saldo
        if selisih == 0:
            await msg.edit_text(f"✅ Saldo {target_kantong} sudah pas Rp {target_saldo:,}. Tidak ada perubahan.")
            return

        corrected_kantong = await sheets_service.get_correct_kantong_case(target_kantong)
        tipe_transaksi = "Masuk" if selisih > 0 else "Keluar"
        nominal_koreksi = int(abs(selisih))
        
        now = datetime.now()
        row = [
            now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), tipe_transaksi,
            corrected_kantong, "Koreksi Saldo Otomatis", "x", 1, 0, "Lainnya", nominal_koreksi
        ]
        
        row_clean = clean_for_json(row)
        await asyncio.to_thread(wks.append_row, row_clean)
        await msg.edit_text(
            f"✅ **Saldo Disesuaikan!**\n"
            f"Saldo Lama: Rp {current_saldo:,}\n"
            f"Target: Rp {target_saldo:,}\n"
            f"Tindakan: Input {tipe_transaksi} Rp {nominal_koreksi:,}"
        , parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Gagal set saldo: {e}")
        await msg.edit_text(f"❌ Gagal set saldo: {e}")
