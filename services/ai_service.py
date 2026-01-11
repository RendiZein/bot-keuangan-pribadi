import os
import json
import asyncio
import logging
import PIL.Image
from groq import Groq
import google.generativeai as genai
from pandasai import SmartDataframe
from pandasai.llm import LLM

from config.settings import GOOGLE_API_KEY, GROQ_API_KEY
from utils.helpers import encode_image
from utils.prompts import get_system_prompt

class MyGroqLLM(LLM):
    """Custom LLM adapter for PandasAI using Groq (Llama 3)."""
    def __init__(self, groq_client):
        self.client = groq_client
        self.model_name = "llama-3.3-70b-versatile"

    def call(self, instruction: str, value: str = None, suffix: str = "") -> str:
        from datetime import datetime, timedelta, timezone
        wib = timezone(timedelta(hours=7))
        now = datetime.now(wib)
        date_today = now.strftime("%Y-%m-%d")
        
        prompt = str(instruction)
        if value:
            prompt += f"\n\nContext:\n{str(value)}"
        if suffix:
            prompt += f"\n\n{str(suffix)}"
            
        try:
            completion = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "system", 
                        "content": (
                            f"You are a data analyst. Today's date is {date_today}. "
                            "If the user asks about 'today' or 'hari ini', use this date literal. "
                            "Return ONLY python code inside ```python``` blocks. "
                            "Always include necessary imports like 'import pandas as pd'."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0
            )
            text_response = completion.choices[0].message.content
            if "```" not in text_response and "result =" in text_response:
                text_response = f"```python\n{text_response}\n```"
            return text_response
        except Exception as e:
            raise Exception(f"Groq Error: {str(e)}")

    @property
    def type(self) -> str:
        return "groq-llama-3"

class AIService:
    def __init__(self):
        # Setup Gemini (untuk pencatatan transaksi)
        if GOOGLE_API_KEY:
            genai.configure(api_key=GOOGLE_API_KEY)
            self.gemini_model = genai.GenerativeModel('gemini-flash-latest')
        else:
            self.gemini_model = None

        # Setup Groq (untuk analisis)
        if GROQ_API_KEY:
            self.groq_client = Groq(api_key=GROQ_API_KEY)
        else:
            self.groq_client = None

    # ... (fungsi call_gemini, call_groq, smart_ai_processing tetap sama) ...
    async def call_gemini(self, text, image_path=None):
        if not self.gemini_model: raise Exception("Google API Key tidak dikonfigurasi.")
        print("🔵 Mencoba Gemini...")
        inputs = [get_system_prompt(), text]
        if image_path:
            img = PIL.Image.open(image_path)
            inputs.append(img)
        response = await asyncio.to_thread(self.gemini_model.generate_content, inputs)
        return response.text

    async def call_groq(self, text, image_path=None):
        if not self.groq_client: raise Exception("Groq API Key tidak dikonfigurasi.")
        print("🟠 Beralih ke Groq...")
        messages = [{"role": "user", "content": [{"type": "text", "text": get_system_prompt() + "\nINPUT USER:\n" + text}]}]
        model_name = "llama-3.3-70b-versatile"
        if image_path:
            model_name = "llama-3.2-90b-vision-preview"
            base64_img = await asyncio.to_thread(encode_image, image_path)
            messages[0]["content"].append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}})
        completion = await asyncio.to_thread(self.groq_client.chat.completions.create, model=model_name, messages=messages, temperature=0, response_format={"type": "json_object"})
        return completion.choices[0].message.content
        
    async def smart_ai_processing(self, text, image_path=None):
        json_result = ""; used_ai = ""
        if GOOGLE_API_KEY:
            try:
                json_result = await self.call_gemini(text, image_path)
                used_ai = "Gemini"
            except Exception as e:
                logging.error(f"⚠️ Gemini Error/Limit: {e}. Switching to Groq.")
                json_result = None
        if not json_result and GROQ_API_KEY:
            try:
                json_result = await self.call_groq(text, image_path)
                used_ai = "Groq Llama"
            except Exception as e:
                raise Exception(f"Semua AI Gagal. Error Groq: {e}")
        return json_result, used_ai

    async def run_analysis(self, query, df):
        """Analisis data HANYA menggunakan Groq (Llama 3) untuk stabilitas."""
        if not self.groq_client:
            return {'type': 'error', 'content': "❌ Groq API Key tidak aktif, analisis data dinonaktifkan."}
            
        try:
            llm = MyGroqLLM(self.groq_client)
            
            sdf = SmartDataframe(df, config={
                "llm": llm,
                "save_charts": True,
                "save_charts_path": ".", 
                "enable_cache": False,
                "verbose": True
            })
            
            response = await asyncio.to_thread(sdf.chat, query)
            
            if isinstance(response, str) and (response.endswith('.png') or response.endswith('.jpg')):
                if os.path.exists(response):
                    return {'type': 'image', 'path': response}
                return {'type': 'text', 'content': f"Grafik dibuat di: {response}, tapi file tidak ditemukan."}
            
            elif isinstance(response, (str, int, float)):
                return {'type': 'text', 'content': str(response)}
            
            elif response is None:
                return {'type': 'text', 'content': "Analisis selesai, tapi tidak ada output teks."}
                
            return {'type': 'text', 'content': str(response)}
            
        except Exception as e:
            logging.error(f"PandasAI/Groq Error: {e}")
            return {'type': 'error', 'content': f"❌ Gagal analisis: {str(e)}"}


# Instance global
ai_service = AIService()
