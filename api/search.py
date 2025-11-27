from http.server import BaseHTTPRequestHandler
import json
import os
import re
from duckduckgo_search import DDGS
import google.generativeai as genai

# ===================== Configuración =====================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# ===================== Helpers =====================
def _clean_upc(s):
    return re.sub(r"\D+", "", s or "")

def _smart_search_with_gemini(query: str, upc: str) -> dict:
    # ESTRATEGIA: Búsqueda abierta pero localizada en México.
    # No usamos "site:..." porque DDG bloquea queries muy largas.
    search_query = f"{query} {upc} precio"
    print(f"🔎 Query Abierta: {search_query}")
    
    raw_results = []
    
    try:
        with DDGS() as ddgs:
            # Pedimos 20 resultados para tener suficiente "materia prima"
            ddg_gen = ddgs.text(search_query, region='mx-es', safesearch='off', max_results=20)
            for r in ddg_gen:
                raw_results.append(f"- Título: {r.get('title')}\n  URL: {r.get('href')}\n  Texto: {r.get('body')}")
    except Exception as e:
        print(f"Error DDG: {e}")

    if not raw_results:
        return {"results": [], "summary": "No se encontraron resultados externos.", "price_range": None}

    # ===================== FILTRO CON GEMINI =====================
    if not GEMINI_API_KEY:
        return {"results": [], "summary": "Falta API Key", "price_range": None}

    try:
        model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"response_mime_type": "application/json"})

        prompt = f"""
        Analiza estos resultados de búsqueda para el producto UPC: {upc}.
        
        INPUT (Resultados Crudos):
        {chr(10).join(raw_results)}

        TU TAREA (FILTRO ESTRICTO):
        1. Identifica ofertas de TIENDAS REALES (Amazon, MercadoLibre, Walmart, Farmacias, Liverpool, Chedraui, Soriana, etc).
        2. DESCARTA TOTALMENTE sitios de cupones (radarcupon, promodescuentos), guías de rastreo, o PDFs.
        3. Extrae el precio actual en MXN. Si no hay precio claro, pon null.
        
        OUTPUT JSON:
        {{
            "offers": [
                {{
                    "title": "Nombre del producto",
                    "price": 150.00,
                    "currency": "MXN",
                    "seller": "Nombre Tienda",
                    "link": "https://..."
                }}
            ],
            "summary": "Resumen breve de disponibilidad"
        }}
        """

        response = model.generate_content(prompt)
        data = json.loads(response.text)
        
        return {
            "results": data.get("offers", []),
            "summary": data.get("summary", ""),
            "price_range": None
        }

    except Exception as e:
        print(f"Error Gemini: {e}")
        return {"results": [], "summary": "Error procesando datos", "price_range": None}

# ===================== Handler =====================
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            
            query = data.get("query", "").strip()
            upc = _clean_upc(data.get("upc", ""))
            
            smart_data = _smart_search_with_gemini(query, upc)
            
            payload = {
                "organic_results": smart_data["results"],
                "gemini_summary": smart_data["summary"],
                "powered_by": "gemini-open-search"
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))