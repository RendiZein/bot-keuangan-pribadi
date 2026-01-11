import json
import asyncio
import logging
from datetime import datetime
from services.sheets_service import sheets_service
from services.ai_service import ai_service
from utils.helpers import clean_for_json

async def core_process_transaction(text_input, media_path=None, source_info="User Input"):
    """
    Logika Inti: Terima teks/gambar -> AI -> JSON -> Sheets.
    Mengembalikan text laporan hasil untuk dikirim ke user.
    """
    # --- DEBUG INPUT ---
    logging.info(f"📥 Incoming Transaction Text: '{text_input}'")
    
    try:
        final_json_text, used_ai = await ai_service.smart_ai_processing(text_input, media_path)
        
        # --- DEBUG AI RESPONSE ---
        logging.info(f"🤖 AI Raw Response ({used_ai}): '{final_json_text}'")
        
        if not final_json_text:
            return "🤔 Maaf, saya tidak dapat memproses input tersebut."

        clean_text = final_json_text.replace('```json', '').replace('```', '').strip()
        data_parsed = json.loads(clean_text)
        data_list = data_parsed.get("transaksi", []) if isinstance(data_parsed, dict) else data_parsed

        if not data_list:
            # Jika dari MacroDroid/Webhook dan kosong, jangan kirim pesan error (Silent)
            if source_info != "Telegram" and source_info != "User Input":
                logging.info("Ignored empty transaction from Webhook.")
                return ""
            return "🤔 Maaf, saya tidak dapat menemukan detail transaksi dari data tersebut."

        wks = sheets_service.get_sheet()
        if not wks:
            return "❌ Koneksi database putus."

        rows = []
        report_text = f"✅ **Tersimpan!** (via {used_ai} | {source_info})\n"
        
        for item in data_list:
            raw_kantong = item.get('kantong', 'Tunai')
            corrected_kantong = await sheets_service.get_correct_kantong_case(raw_kantong)
            
            item['kantong'] = corrected_kantong
            
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
        
        return report_text

    except Exception as e:
        logging.error(f"Error Core Process: {e}")
        return f"❌ Error: {str(e)}"