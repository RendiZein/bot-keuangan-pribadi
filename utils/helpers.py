import base64
import numpy as np
import matplotlib

# Wajib untuk server/VPS tanpa display (seperti Google VM Anda)
matplotlib.use('Agg') 

def encode_image(image_path):
    """Mengubah file gambar menjadi base64 string."""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def clean_for_json(data):
    """
    Mengubah tipe data Numpy (int64, float64) menjadi Python native (int, float) 
    agar bisa di-JSON-kan dengan aman.
    """
    if isinstance(data, list):
        return [clean_for_json(x) for x in data]
    if isinstance(data, dict):
        return {k: clean_for_json(v) for k, v in data.items()}
    if isinstance(data, (np.int64, np.int32, np.integer)):
        return int(data)
    if isinstance(data, (np.float64, np.float32, np.floating)):
        return float(data)
    return data
