# 🤖 Bot Keuangan Pribadi (AI Hybrid)

Bot Telegram cerdas untuk mencatat keuangan pribadi, cek saldo, dan analisis data menggunakan **Google Gemini** (Prioritas) dan **Groq Llama** (Fallback). Data tersimpan aman di **Google Sheets**.

## ✨ Fitur Utama

1.  **Pencatatan Fleksibel**:
    *   **Teks**: "Beli nasi goreng 15rb pakai gopay"
    *   **Suara**: Kirim voice note, bot akan transkrip & catat.
    *   **Foto**: Kirim foto struk, bot ekstrak detailnya.
2.  **Manajemen Saldo**:
    *   Cek saldo semua akun (BCA, Mandiri, Gopay, dll).
    *   Koreksi saldo otomatis dengan `/setsaldo`.
3.  **Analisis Data (PandasAI)**:
    *   Tanya natural: "Berapa pengeluaran makan bulan ini?"
    *   Minta grafik: "Buatkan grafik pengeluaran per kategori minggu lalu".
4.  **Keamanan**:
    *   Whitelist user (hanya ID Telegram terdaftar yang bisa akses).
    *   Data tersimpan di Google Sheets pribadi Anda.

## 🛠️ Instalasi & Setup

### 1. Persiapan
*   Python 3.10+
*   Akun Google Cloud (untuk Sheets API & Gemini API)
*   Akun Groq (untuk Llama & Whisper)
*   Bot Token dari @BotFather

### 2. Install Dependencies
```bash
pip install -r requirements.txt