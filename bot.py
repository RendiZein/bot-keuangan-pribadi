import os
import logging
import json
import asyncio
import base64
import PIL.Image  # Library untuk gambar Gemini
from datetime import datetime, timedelta, timezone
import pandas as pd
import gspread
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
from dotenv import load_dotenv
from groq import Groq
import google.generativeai as genai
import io
import matplotlib
matplotlib.use('Agg') # Wajib untuk server/VPS
import matplotlib.pyplot as plt
import seaborn as sns

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

def get_system_prompt():
    wib = timezone(timedelta(hours=7))
    
    # Ambil waktu sekarang dengan zona waktu WIB
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

# --- 4. CORE AI LOGIC (HYBRID) ---

async def call_gemini(text, image_path=None):
    """Fungsi Prioritas: Menggunakan Gemini 2.5 Flash"""
    print("🔵 Mencoba Gemini...")
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    inputs = [get_system_prompt(), text]
    
    if image_path:
        # Gemini suka input berupa PIL Image
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
        model_name = "meta-llama/llama-4-scout-17b-16e-instruct" # Vision Llama 4
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
            source = "Gemini 2.5"
        except Exception as e:
            logging.error(f"⚠️ Gemini Error/Limit: {e}. Switching to Groq.")
            # Jangan return, biarkan lanjut ke blok Groq di bawah
            json_result = None

    # 2. COBA GROQ (FALLBACK)
    # Jalankan jika Gemini Gagal (json_result None) atau API Key Gemini tidak ada
    if not json_result:
        try:
            json_result = await call_groq(text, image_path)
            source = "Groq Llama"
        except Exception as e:
            raise Exception(f"Semua AI Gagal. Error Groq: {e}")
            
    return json_result, source

# --- 5. COMMAND HANDLERS ---

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
            # gunakan .loc + .copy() untuk menghindari SettingWithCopyWarning
            mask = df['kantong'].astype(str).str.lower() == target_kantong.lower()
            df_k = df.loc[mask].copy()

            # bersihkan angka di salinan; hasilnya tetap pandas Series tapi kita cast ke int nanti
            df_k[col_harga] = pd.to_numeric(df_k[col_harga].astype(str).str.replace(r'[^\d-]', '', regex=True), errors='coerce').fillna(0)

            masuk = df_k[df_k['tipe'].str.lower() == 'masuk'][col_harga].sum()
            keluar = df_k[df_k['tipe'].str.lower() == 'keluar'][col_harga].sum()

            # konversi ke Python int untuk menghindari numpy.int64
            current_saldo = int(masuk - keluar)

        selisih = target_saldo - current_saldo

        if selisih == 0:
            await msg.edit_text(f"✅ Saldo {target_kantong} sudah pas Rp {target_saldo:,}. Tidak ada perubahan.")
            return

        tipe_transaksi = "Masuk" if selisih > 0 else "Keluar"
        nominal_koreksi = int(abs(selisih))

        now = datetime.now()
        row = [
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M"),
            tipe_transaksi,
            target_kantong,
            "Koreksi Saldo Otomatis",
            "x", 1, 0,
            "Lainnya",
            nominal_koreksi
        ]

        # Pastikan semua angka adalah Python int/str (nominal_koreksi sudah int)
        await asyncio.to_thread(wks.append_row, row)
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

# --- FITUR BARU: DATA ANALYST (PAL) ---
async def run_analysis(query, df):
    """
    Fungsi ini mengubah Natural Language -> Python Code -> Eksekusi -> Hasil/Grafik
    """
    # 1. Siapkan Konteks Data (Strategi 3: Pre-Computation Schema)
    # Kita berikan info kolom dan contoh data ke AI
    buffer_info = io.StringIO()
    df.info(buf=buffer_info)
    schema_info = buffer_info.getvalue()
    sample_data = df.head(3).to_markdown()
    
    # [BARU] Ambil tanggal hari ini untuk konteks "Kemarin/Hari ini"
    today_str = datetime.now().strftime("%Y-%m-%d")

    system_prompt = f"""
    Kamu adalah Senior Data Analyst Python.
    Waktu Saat Ini: {today_str} (Gunakan ini untuk menghitung 'kemarin', 'minggu lalu', dll).
    
    Tugas: Tulis kode Python untuk menganalisis DataFrame `df` berdasarkan pertanyaan user.
    
    INFO DATAFRAME:
    {schema_info}
    
    CONTOH DATA:
    {sample_data}
    
    ATURAN KODING (STRICT):
    1. Variabel DataFrame bernama `df` sudah tersedia.
    2. KONVERSI TANGGAL: Kolom 'tanggal' mungkin string. Wajib ubah dulu: 
       `df['tanggal'] = pd.to_datetime(df['tanggal'], errors='coerce')`
    3. Jika User minta GRAFIK: Akhiri dengan `plt.savefig('chart_output.png')` dan set `tipe_output = 'gambar'`.
    4. Jika User minta ANGKA/TEKS: Simpan hasil string di `hasil_teks` dan set `tipe_output = 'teks'`.
    5. FILTER WAKTU: Jika user tanya "Kemarin", filter `df['tanggal'] == pd.Timestamp('{today_str}') - pd.Timedelta(days=1)`.
    6. HANYA RETURN KODE PYTHON. Tanpa markdown.
    """

    # 2. Generate Kode via Groq (Llama 3 70B sangat jago coding)
    # Kita pakai Groq karena cepat.
    prompt_full = f"Pertanyaan User: {query}\n\nTulis kodenya sekarang:"
    
    completion = await asyncio.to_thread(
        groq_client.chat.completions.create,
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_full}
        ],
        temperature=0
    )
    
    code_raw = completion.choices[0].message.content
    
    # Bersihkan markdown (```python ... ```)
    code_clean = code_raw.replace("```python", "").replace("```", "").strip()
    
    # 3. Eksekusi Kode (Strategi 2: Execution Sandbox Sederhana)
    # Kita siapkan dictionary lokal untuk menangkap variabel hasil
    local_vars = {'df': df, 'plt': plt, 'sns': sns, 'pd': pd}
    
    try:
        # Eksekusi kode yang dibuat AI
        exec(code_clean, {}, local_vars)
        
        # Cek tipe output
        tipe = local_vars.get('tipe_output', 'teks')
        
        if tipe == 'gambar':
            return {'type': 'image', 'path': 'chart_output.png'}
        else:
            return {'type': 'text', 'content': local_vars.get('hasil_teks', 'Selesai, tapi variabel hasil_teks kosong.')}
            
    except Exception as e:
        logging.error(f"Error Eksekusi Code: {e}")
        logging.error(f"Code Bermasalah: {code_clean}")
        return {'type': 'error', 'content': f"❌ Gagal hitung: {str(e)}"}

# --- 6. MESSAGE HANDLER UTAMA ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ALLOWED_USERS: return

    user_input = update.message.text or ""
    
    # [UPDATE] 1. DETEKSI INTENT ANALISA LEBIH LUAS
    # Tambahkan keyword: "pengeluaran", "habis", "kemarin", "bulan ini"
    keywords_analisa = [
        "analisa", "grafik", "chart", "plot", "tren", "statistik", 
        "berapa", "total", "bandingkan", "pengeluaran saya", 
        "habis berapa", "kemarin", "bulan ini", "minggu ini"
    ]
    
    # Logic: Jika ada keyword analisa DAN TIDAK mengandung format transaksi jelas (misal: angka dlm juta)
    # Tujuannya agar "Berapa pengeluaran kemarin" masuk sini.
    is_analisa = any(k in user_input.lower() for k in keywords_analisa)
    
    if is_analisa and len(user_input.split()) > 1:
        
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        msg = await update.message.reply_text("🧠 Sedang menganalisis data...")
        
        try:
            print(f"--- DEBUG: Mode Analisa Triggered oleh '{user_input}' ---")
            wks = get_gspread_client()
            data = await asyncio.to_thread(wks.get_all_records)
            
            if not data:
                await msg.edit_text("❌ Data kosong.")
                return

            df = pd.DataFrame(data)
            
            # Bersihkan Data
            df.columns = [str(c).lower().strip() for c in df.columns]
            
            # Cari kolom harga (Logic Robust)
            candidates = [c for c in df.columns if ('total' in c or 'amount' in c or 'harga' in c) and 'satuan' not in c]
            col_harga = candidates[0] if candidates else df.columns[-1]
            
            # Bersihkan Angka
            df[col_harga] = df[col_harga].astype(str).str.replace(r'[^\d-]', '', regex=True)
            df[col_harga] = pd.to_numeric(df[col_harga], errors='coerce').fillna(0)

            # Jalankan AI
            hasil = await run_analysis(user_input, df)
            
            if hasil['type'] == 'image':
                await update.message.reply_photo(photo=open(hasil['path'], 'rb'), caption="📊 Grafik Analisis")
                await msg.delete()
                os.remove(hasil['path'])
            elif hasil['type'] == 'text':
                await msg.edit_text(f"💡 **Hasil:**\n{hasil['content']}", parse_mode="Markdown")
            else:
                await msg.edit_text(hasil['content'])

        except Exception as e:
            import traceback
            print(f"❌ ERROR ANALISA: {traceback.format_exc()}")
            await msg.edit_text(f"Gagal analisa: {str(e)}")
            
        return # STOP, jangan lanjut ke pencatatan

    # --- 2. CEK SALDO ---
    keywords_saldo = ["saldo", "cek uang", "dompet", "keuanganku", "sisa uang"]
    if any(k in user_input.lower() for k in keywords_saldo):
        await proses_cek_saldo(update, context)
        return

    # --- 3. CATAT TRANSAKSI (Dengan Anti-Crash) ---
    # Jika lolos dari filter di atas, kita asumsikan ini transaksi.
    # Tapi kita pasang Try-Except agar kalau bukan JSON, tidak crash.
    try:
        await proses_catat_transaksi(update, context)
    except Exception as e:
        print(f"❌ Error Transaksi: {e}")
        # Jangan reply error ke user jika itu cuma chat iseng "Halo"
        # Biarkan silent atau balas standar
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
        
        # Logic Cari Kolom Harga (Total -> Amount -> Harga)
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
            df_k = df[df['kantong'] == k]
            # Bersihkan angka
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

        # A. HANDLER AUDIO (Tetap pakai Groq Whisper karena cepat & spesifik)
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

        # B. HANDLER GAMBAR
        elif update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
            media_path = "temp_image.jpg"
            await photo_file.download_to_drive(media_path)
            # user_text_input tetap kosong atau caption kalau ada

        # C. PROSES SMART AI (Gemini -> Fallback Groq)
        final_json_text, used_ai = await smart_ai_processing(user_text_input, media_path)
        
        # Bersihkan temp image
        if media_path:
            try: os.remove(media_path)
            except: pass

        # D. PARSING & SIMPAN
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
        for item in data_list:
            rows.append([
                item.get('tanggal'), item.get('jam'), item.get('tipe'),
                item.get('kantong', 'Tunai'), item.get('nama'), item.get('satuan', 'x'),
                item.get('volume', 1), item.get('harga_satuan', 0),
                item.get('kategori'), item.get('harga_total', 0)
            ])
            arrow = "➡️" if item.get('tipe') == 'Keluar' else "⬅️"
            report_text += f"\n{arrow} {item.get('kantong')}: Rp {item.get('harga_total'):,} ({item.get('nama')})"

        await asyncio.to_thread(wks.append_rows, rows)
        await msg.edit_text(report_text)

    except Exception as e:
        logging.error(f"Error Utama: {e}")
        await msg.edit_text(f"❌ Error: {str(e)}")

# --- 7. MAIN RUN ---
if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    filter_all = filters.TEXT | filters.PHOTO | filters.VOICE
    application.add_handler(MessageHandler(filter_all & (~filters.COMMAND), handle_message))
    application.add_handler(CommandHandler("undo", undo_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("setsaldo", setsaldo_command))
    
    print("🚀 Bot Hybrid (Gemini Priority -> Groq Fallback) SIAP!...")
    application.run_polling()