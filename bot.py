import os
import logging
import json
import asyncio
import base64
import PIL.Image
from datetime import datetime, timedelta, timezone
import pandas as pd
import gspread
import numpy as np # Tambahan untuk handling tipe data
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton # Tambah ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
from dotenv import load_dotenv
from groq import Groq
import google.generativeai as genai
import io
import matplotlib
matplotlib.use('Agg') # Wajib untuk server/VPS
import matplotlib.pyplot as plt
import seaborn as sns

# --- PANDASAI IMPORTS ---
from pandasai import SmartDataframe
from pandasai.llm import GoogleGemini # Ini pasti jalan di 2.2.15

# --- 1. KONFIGURASI ENV & CLIENT ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SHEET_NAME = os.getenv("SHEET_NAME")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE")

# Security Check
allowed_users_raw = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = [uid.strip() for uid in allowed_users_raw.split(",") if uid.strip()]

# Setup Groq (Fallback)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY)

# Setup Gemini (Prioritas)
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- 2. KONEKSI SHEETS ---
def get_gspread_client():
    try:
        gc = gspread.service_account(filename=CREDENTIALS_FILE)
        sh = gc.open(SHEET_NAME)
        return sh.sheet1
    except Exception as e:
        logging.error(f"Gagal koneksi Sheets: {e}")
        return None

# --- 3. HELPER FUNCTIONS ---
def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def clean_for_json(data):
    """Mengubah tipe data Numpy (int64, float64) menjadi Python native (int, float) agar bisa di-JSON-kan."""
    if isinstance(data, list):
        return [clean_for_json(x) for x in data]
    if isinstance(data, dict):
        return {k: clean_for_json(v) for k, v in data.items()}
    if isinstance(data, (np.int64, np.int32, np.integer)):
        return int(data)
    if isinstance(data, (np.float64, np.float32, np.floating)):
        return float(data)
    return data

def get_system_prompt():
    wib = timezone(timedelta(hours=7))
    now = datetime.now(wib)
    return f"""
    Kamu adalah manajer keuangan pribadi. Waktu: {now.strftime("%Y-%m-%d %H:%M")}.
    Tugas: Ekstrak informasi transaksi menjadi format JSON Valid.
    
    ATURAN KHUSUS:
    1. TRANSFER: Jika "TF BCA ke Gopay 50rb" -> Buat 2 item: (Keluar BCA) dan (Masuk Gopay).
    
    ATURAN UMUM:
    - Tipe: "Masuk" atau "Keluar".
    - Kantong: Deteksi akun (BCA, Mandiri, Gopay, Tunai, dll). Default="Tunai".
    - Harga: Integer.
    - Nama: Singkat, hapus kata kerja (cth: "Bensin Pertalite", "Gajian Bulan Ini").
    - Kategori: WAJIB pilih salah satu dari: 
      [Makan, Transportasi, Belanja, Tagihan, Hiburan, Kesehatan, Pendidikan, Investasi, Amal, Pemasukan, Lainnya].
    
    ATURAN KATEGORI:
    - Jika Tipe = "Masuk" (Gaji, Bonus, Temu Uang), maka Kategori WAJIB = "Pemasukan".
    - Bensin, Parkir, Service, Ojol = "Transportasi".
    
    OUTPUT JSON OBJECT:
    {{ "transaksi": [ {{ "tanggal": "YYYY-MM-DD", "jam": "HH:MM", "tipe": "Masuk/Keluar", "kantong": "...", "nama": "...", "satuan": "x", "volume": 1, "harga_satuan": 0, "kategori": "...", "harga_total": 0 }} ] }}
    """

async def get_correct_kantong_case(wks, new_kantong_name):
    """Mencari nama kantong yang benar (case-insensitive) atau mengembalikan input asli."""
    try:
        # Asumsi kolom 'kantong' adalah kolom ke-4 (gspread uses 1-based index)
        all_kantong_values = await asyncio.to_thread(wks.col_values, 4)
        
        # Buat set unik dari baris ke-2 dst (abaikan header)
        existing_kantongs = set(all_kantong_values[1:])
        
        # Buat mapping lowercase -> original case
        kantong_map = {k.lower(): k for k in existing_kantongs if k}
        
        # Cari match case-insensitive
        corrected_name = kantong_map.get(new_kantong_name.lower())
        
        if corrected_name:
            return corrected_name # Kembalikan nama kantong yg sudah ada
        else:
            # Jika baru, seragamkan ke Title Case (contoh: "shopeepay" -> "Shopeepay")
            return new_kantong_name.title()
            
    except Exception as e:
        logging.error(f"Gagal get/correct kantong case: {e}")
        # Fallback aman jika GSheets error, tetap format ke Title Case
        return new_kantong_name.title()

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan menu utama dengan tombol."""
    keyboard = [
        [KeyboardButton("💰 Cek Saldo"), KeyboardButton("📊 Analisis")],
        [KeyboardButton("↩️ Undo Terakhir"), KeyboardButton("❓ Bantuan")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("🤖 **Menu Utama:**\nSilakan pilih opsi atau ketik transaksi langsung.", reply_markup=reply_markup, parse_mode="Markdown")

# --- 4. CORE AI LOGIC (HYBRID) ---

async def call_gemini(text, image_path=None):
    """Fungsi Prioritas: Menggunakan Gemini 2.5 Flash"""
    print("🔵 Mencoba Gemini...")
    model = genai.GenerativeModel('gemini-2.0-flash') # Update model name if needed, 2.5 might be typo in user code or preview
    
    inputs = [get_system_prompt(), text]
    
    if image_path:
        img = PIL.Image.open(image_path)
        inputs.append(img)
        
    response = await asyncio.to_thread(model.generate_content, inputs)
    return response.text

async def call_groq(text, image_path=None):
    """Fungsi Fallback: Menggunakan Groq (Llama 3/4)"""
    print("🟠 Beralih ke Groq...")
    
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": get_system_prompt() + "\nINPUT USER:\n" + text}
        ]}
    ]
    
    model_name = "llama-3.3-70b-versatile" # Default Text
    
    if image_path:
        model_name = "llama-3.2-90b-vision-preview" # Vision Llama (Updated for better availability)
        base64_img = await asyncio.to_thread(encode_image, image_path)
        messages[0]["content"].append({
            "type": "image_url", 
            "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}
        })
        
    completion = await asyncio.to_thread(
        groq_client.chat.completions.create,
        model=model_name,
        messages=messages,
        temperature=0,
        response_format={"type": "json_object"}
    )
    return completion.choices[0].message.content

async def smart_ai_processing(text, image_path=None):
    """
    Manager Cerdas: Coba Gemini dulu, kalau gagal baru Groq.
    """
    json_result = ""
    source = ""
    
    # 1. COBA GEMINI (PRIORITAS)
    if GOOGLE_API_KEY:
        try:
            json_result = await call_gemini(text, image_path)
            source = "Gemini"
        except Exception as e:
            logging.error(f"⚠️ Gemini Error/Limit: {e}. Switching to Groq.")
            json_result = None

    # 2. COBA GROQ (FALLBACK)
    if not json_result:
        try:
            json_result = await call_groq(text, image_path)
            source = "Groq Llama"
        except Exception as e:
            raise Exception(f"Semua AI Gagal. Error Groq: {e}")
            
    return json_result, source

# --- 5. COMMAND HANDLERS ---

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
    wks = get_gspread_client()
    if not wks:
        await msg.edit_text("❌ Database error.")
        return

    try:
        all_values = await asyncio.to_thread(wks.get_all_values)
        if len(all_values) <= 1:
            await msg.edit_text("⚠️ Data kosong.")
            return
        
        last_item = "Item"
        try: last_item = all_values[-1][4]
        except: pass
        
        await asyncio.to_thread(wks.delete_rows, len(all_values))
        await msg.edit_text(f"✅ **Undo:** _{last_item}_ dihapus.", parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ALLOWED_USERS: return
    if not context.args or context.args[0].lower() != 'confirm':
        await update.message.reply_text("⚠️ **BAHAYA!** Ketik `/reset confirm` untuk menghapus SEMUA data.", parse_mode="Markdown")
        return

    msg = await update.message.reply_text("⏳ Mereset database...")
    wks = get_gspread_client()
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
    wks = get_gspread_client()
    if not wks: return

    try:
        data = await asyncio.to_thread(wks.get_all_records)
        df = pd.DataFrame(data)
        df.columns = [str(c).lower().strip() for c in df.columns]
        
        col_harga = next((c for c in df.columns if 'total' in c or 'amount' in c or 'harga' in c if 'satuan' not in c), None)
        
        if not col_harga or df.empty:
            current_saldo = 0
        else:
            # FIX: Gunakan .loc dan .copy() untuk menghindari SettingWithCopyWarning
            mask = df['kantong'].astype(str).str.lower() == target_kantong.lower()
            df_k = df.loc[mask].copy()
            
            df_k[col_harga] = pd.to_numeric(df_k[col_harga].astype(str).str.replace(r'[^\d-]', '', regex=True), errors='coerce').fillna(0)
            
            masuk = df_k[df_k['tipe'].str.lower() == 'masuk'][col_harga].sum()
            keluar = df_k[df_k['tipe'].str.lower() == 'keluar'][col_harga].sum()
            current_saldo = int(masuk - keluar) # FIX: Cast ke int python

        selisih = target_saldo - current_saldo
        
        if selisih == 0:
            await msg.edit_text(f"✅ Saldo {target_kantong} sudah pas Rp {target_saldo:,}. Tidak ada perubahan.")
            return

        # Dapatkan nama kantong yang benar sebelum membuat transaksi koreksi
        corrected_kantong = await get_correct_kantong_case(wks, target_kantong)

        tipe_transaksi = "Masuk" if selisih > 0 else "Keluar"
        nominal_koreksi = int(abs(selisih)) # FIX: Cast ke int python
        
        now = datetime.now()
        row = [
            now.strftime("%Y-%m-%d"), 
            now.strftime("%H:%M"), 
            tipe_transaksi,
            corrected_kantong, 
            "Koreksi Saldo Otomatis", 
            "x", 1, 0, 
            "Lainnya", 
            nominal_koreksi
        ]
        
        # FIX: Bersihkan row dari tipe data numpy sebelum kirim
        row_clean = clean_for_json(row)
        
        await asyncio.to_thread(wks.append_row, row_clean)
        await msg.edit_text(
            f"✅ **Saldo Disesuaikan!**\n"
            f"Saldo Lama: Rp {current_saldo:,}\n"
            f"Target: Rp {target_saldo:,}\n"
            f"Tindakan: Input {tipe_transaksi} Rp {nominal_koreksi:,}"
        , parse_mode="Markdown")

    except Exception as e:
        import traceback
        logging.error(traceback.format_exc())
        await msg.edit_text(f"❌ Gagal set saldo: {e}")

# --- FITUR BARU: DATA ANALYST (PANDASAI) ---
async def run_analysis(query, df):
    """
    Menggunakan PandasAI untuk analisis data yang lebih aman dan powerful.
    """
    try:
        # 1. Setup LLM (Gunakan Gemini karena user punya key-nya)
        llm = GoogleGemini(api_key=GOOGLE_API_KEY)
        
        # 2. Setup SmartDataframe
        # save_charts=True akan menyimpan chart di folder 'exports/charts' secara default
        sdf = SmartDataframe(df, config={
            "llm": llm,
            "save_charts": True,
            "save_charts_path": ".", # Simpan di root agar mudah diambil
            "enable_cache": False,
            "verbose": True
        })
        
        # 3. Eksekusi Query
        # PandasAI akan mengembalikan path file jika itu gambar, atau string/int jika teks
        response = await asyncio.to_thread(sdf.chat, query)
        
        # 4. Cek Tipe Response
        if isinstance(response, str) and (response.endswith('.png') or response.endswith('.jpg')):
            # Jika response adalah path file gambar
            if os.path.exists(response):
                return {'type': 'image', 'path': response}
            else:
                # Fallback jika path tidak ketemu tapi string mengandung .png
                return {'type': 'text', 'content': f"Grafik dibuat di: {response}, tapi file tidak ditemukan."}
        
        elif isinstance(response, (str, int, float)):
            return {'type': 'text', 'content': str(response)}
            
        elif response is None:
            # Kadang PandasAI return None tapi generate chart
            # Cek apakah ada file .png baru dibuat
            # (Simplifikasi: Kita asumsikan kalau None berarti mungkin error atau chart only)
            return {'type': 'text', 'content': "Analisis selesai, tapi tidak ada output teks."}
            
        else:
            return {'type': 'text', 'content': str(response)}

    except Exception as e:
        logging.error(f"PandasAI Error: {e}")
        return {'type': 'error', 'content': f"❌ Gagal analisis: {str(e)}"}

# --- 6. MESSAGE HANDLER UTAMA ---

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

    # [UPDATE] 1. DETEKSI INTENT ANALISA
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
            wks = get_gspread_client()
            data = await asyncio.to_thread(wks.get_all_records)
            
            if not data:
                await msg.edit_text("❌ Data kosong.")
                return

            df = pd.DataFrame(data)
            df.columns = [str(c).lower().strip() for c in df.columns]
            
            # Preprocessing Data untuk PandasAI
            # Pastikan kolom tanggal dikenali
            if 'tanggal' in df.columns:
                df['tanggal'] = pd.to_datetime(df['tanggal'], errors='coerce')
            
            # Pastikan kolom harga numerik
            candidates = [c for c in df.columns if ('total' in c or 'amount' in c or 'harga' in c) and 'satuan' not in c]
            col_harga = candidates[0] if candidates else df.columns[-1]
            
            df[col_harga] = df[col_harga].astype(str).str.replace(r'[^\d-]', '', regex=True)
            df[col_harga] = pd.to_numeric(df[col_harga], errors='coerce').fillna(0)

            # Jalankan PandasAI
            hasil = await run_analysis(user_input, df)
            
            if hasil['type'] == 'image':
                await update.message.reply_photo(photo=open(hasil['path'], 'rb'), caption="📊 Grafik Analisis")
                await msg.delete()
                # Cleanup image
                try: os.remove(hasil['path'])
                except: pass
            elif hasil['type'] == 'text':
                await msg.edit_text(f"💡 **Hasil:**\n{hasil['content']}", parse_mode="Markdown")
            else:
                await msg.edit_text(hasil['content'])

        except Exception as e:
            import traceback
            print(f"❌ ERROR ANALISA: {traceback.format_exc()}")
            await msg.edit_text(f"Gagal analisa: {str(e)}")
            
        return 

    # --- 2. CEK SALDO ---
    keywords_saldo = ["saldo", "cek uang", "dompet", "keuanganku", "sisa uang"]
    if any(k in user_input.lower() for k in keywords_saldo):
        await proses_cek_saldo(update, context)
        return

    # --- 3. CATAT TRANSAKSI ---
    try:
        await proses_catat_transaksi(update, context)
    except Exception as e:
        print(f"❌ Error Transaksi: {e}")
        await update.message.reply_text("🤔 Saya tidak mengerti. Apakah ini transaksi atau pertanyaan analisis?")

async def proses_cek_saldo(update, context):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    msg = await update.message.reply_text("🔍 Menghitung aset...")
    
    try:
        wks = get_gspread_client()
        if not wks:
            await msg.edit_text("❌ Database error.")
            return
            
        data = await asyncio.to_thread(wks.get_all_records)
        if not data:
            await msg.edit_text("Belum ada data.")
            return

        df = pd.DataFrame(data)
        df.columns = [c.lower().strip() for c in df.columns]
        
        col_harga = next((c for c in df.columns if 'total' in c), None)
        if not col_harga: col_harga = next((c for c in df.columns if 'amount' in c), None)
        if not col_harga: col_harga = next((c for c in df.columns if 'harga' in c and 'satuan' not in c), None)
        
        if not col_harga:
            await msg.edit_text("❌ Kolom harga tidak ditemukan.")
            return

        rekap_saldo = {}
        total_aset = 0
        kantongs = df['kantong'].unique()
        report = "💰 **Kondisi Keuangan**\n"
        
        for k in kantongs:
            if not k: continue
            # FIX: Tambahkan .copy() di sini untuk menghindari SettingWithCopyWarning
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

async def proses_catat_transaksi(update, context):
    msg = await update.message.reply_text("⚡ Memproses (Auto-Switch AI)...")
    
    try:
        final_json_text = ""
        used_ai = ""
        media_path = None
        user_text_input = update.message.text or ""

        if update.message.voice:
            voice_file = await update.message.voice.get_file()
            media_path = "temp_audio.ogg"
            await voice_file.download_to_drive(media_path)
            
            with open(media_path, "rb") as file:
                transcription = await asyncio.to_thread(
                    groq_client.audio.transcriptions.create,
                    file=(media_path, file.read()),
                    model="whisper-large-v3-turbo",
                    response_format="json"
                )
            user_text_input = transcription.text
            await msg.edit_text(f"🗣️: \"{user_text_input}\"")
            try: os.remove(media_path)
            except: pass
            media_path = None # Reset agar tidak dianggap gambar

        elif update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
            media_path = "temp_image.jpg"
            await photo_file.download_to_drive(media_path)

        final_json_text, used_ai = await smart_ai_processing(user_text_input, media_path)
        
        if media_path:
            try: os.remove(media_path)
            except: pass

        clean_text = final_json_text.replace('```json', '').replace('```', '').strip()
        data_parsed = json.loads(clean_text)
        data_list = data_parsed.get("transaksi", []) if isinstance(data_parsed, dict) else data_parsed

        if not data_list:
            await msg.edit_text(f"⚠️ {used_ai} tidak mengerti data ini.")
            return

        wks = get_gspread_client()
        if not wks:
            await msg.edit_text("❌ Koneksi database putus.")
            return

        rows = []
        report_text = f"✅ **Tersimpan!** (via {used_ai})\n"
        
        # Proses setiap item untuk memperbaiki 'kantong'
        for item in data_list:
            raw_kantong = item.get('kantong', 'Tunai')
            # Panggil helper function untuk koreksi case
            corrected_kantong = await get_correct_kantong_case(wks, raw_kantong)
            
            # Update item dengan kantong yg benar (untuk laporan) dan buat row
            item['kantong'] = corrected_kantong # Ini penting untuk report_text
            
            row = [
                item.get('tanggal'), item.get('jam'), item.get('tipe'),
                corrected_kantong, item.get('nama'), item.get('satuan', 'x'),
                item.get('volume', 1), item.get('harga_satuan', 0),
                item.get('kategori'), item.get('harga_total', 0)
            ]
            
            rows.append(clean_for_json(row))
            
            arrow = "➡️" if item.get('tipe') == 'Keluar' else "⬅️"
            report_text += f"\n{arrow} {item.get('kantong')}: Rp {item.get('harga_total'):,} ({item.get('nama')})"

        if rows:
            await asyncio.to_thread(wks.append_rows, rows)
        
        await msg.edit_text(report_text)

    except Exception as e:
        logging.error(f"Error Utama: {e}")
        await msg.edit_text(f"❌ Error: {str(e)}")

# --- 7. MAIN RUN ---
if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    filter_all = filters.TEXT | filters.PHOTO | filters.VOICE
    application.add_handler(CommandHandler("start", start_command)) # Tambah start handler
    application.add_handler(CommandHandler("menu", start_command))  # Alias menu
    application.add_handler(CommandHandler("help", help_command))   # Tambah help handler
    
    application.add_handler(MessageHandler(filter_all & (~filters.COMMAND), handle_message))
    application.add_handler(CommandHandler("undo", undo_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("setsaldo", setsaldo_command))
    
    print("🚀 Bot Hybrid (Gemini Priority -> Groq Fallback) SIAP!...")
    application.run_polling()