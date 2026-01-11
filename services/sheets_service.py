import gspread
import logging
import asyncio
import time
from config.settings import CREDENTIALS_FILE, SHEET_NAME

class SheetsService:
    def __init__(self):
        self.client = None
        self.sheet = None
        self._kantong_cache = None
        self._cache_time = 0
        self.CACHE_TTL = 600  # Cache berlaku selama 10 menit (600 detik)

    def get_sheet(self):
        """Koneksi ke Google Sheets (Singleton-like)."""
        if self.sheet:
            return self.sheet
        
        try:
            gc = gspread.service_account(filename=CREDENTIALS_FILE)
            sh = gc.open(SHEET_NAME)
            self.sheet = sh.sheet1
            return self.sheet
        except Exception as e:
            logging.error(f"Gagal koneksi Sheets: {e}")
            return None

    async def get_correct_kantong_case(self, new_kantong_name):
        """Mencari nama kantong yang benar (case-insensitive) dengan caching."""
        now = time.time()
        
        # Gunakan cache jika masih valid
        if self._kantong_cache and (now - self._cache_time < self.CACHE_TTL):
            corrected_name = self._kantong_cache.get(new_kantong_name.lower())
            return corrected_name if corrected_name else new_kantong_name.title()

        # Jika cache expired atau belum ada, ambil dari Sheets
        wks = self.get_sheet()
        if not wks:
            return new_kantong_name.title()
            
        try:
            logging.info("🔄 Refreshing kantong cache from GSheets...")
            # Asumsi kolom 'kantong' adalah kolom ke-4
            all_kantong_values = await asyncio.to_thread(wks.col_values, 4)
            
            # Buat set unik dan mapping lowercase -> original case
            existing_kantongs = set(all_kantong_values[1:])
            self._kantong_cache = {k.lower(): k for k in existing_kantongs if k}
            self._cache_time = now
            
            corrected_name = self._kantong_cache.get(new_kantong_name.lower())
            return corrected_name if corrected_name else new_kantong_name.title()
                
        except Exception as e:
            logging.error(f"Gagal get/correct kantong case: {e}")
            return new_kantong_name.title()

# Instance global untuk memudahkan pemakaian
sheets_service = SheetsService()
