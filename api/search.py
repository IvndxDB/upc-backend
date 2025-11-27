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

# ===================== LISTA BLANCA (Tu filtro de calidad) =====================
TRUSTED_SITES = [
    "amazon.com.mx", "mercadolibre.com.mx", "walmart.com.mx", 
    "bodegaaurrera.com.mx", "super.walmart.com.mx", "chedraui.com.mx", 
    "soriana.com", "lacomer.com.mx", "liverpool.com.mx", 
    "fahorro.com", "farmaciasguadalajara.com", "farmaciasanpablo.com.mx", 
    "benavides.com.mx", "sanborns.com.mx", "sears.com.mx", 
    "coppel.com", "elektra.mx", "hebmexico.com", "costco.com.mx", 
    "sams.com.mx", "homedepot.com.mx"
]

# ===================== Helpers =====================
def _clean_upc(s):
    return re.sub(r"\D+", "", s or "")

def _build_targeted_query(upc, product_name):
    # Construye: "UPC nombre (site:amazon... OR site:walmart...)"
    # Limitamos a los top 10 para no romper el límite de longitud de DDG
    top_sites = " OR ".join([f"site:{site}" for site in TRUSTED_SITES[:12]])
    base = f"{product_name} {upc}" if product_name else upc
    return f"{base} ({top_sites})"

def _smart_search_with_gemini(query: str, upc: str) -> dict:
    targeted_query = _build_targeted_query(upc, query)
    print(f"🔎 Query: {targeted_query}")
    
    raw_results = []
    
    # 1. BÚSQUEDA EN DUCKDUCKGO (Robusta contra bloqueos)
    try:
        with DDGS() as ddgs:
            # region='mx-es' fuerza resultados de México
            ddg_gen = ddgs.text(targeted_query, region='mx-es', safesearch='off', max_results=15)
            for r in ddg_gen:
                link = r.get('href', '').lower()
                # Doble verificación: que el link sea de confianza
                if any(site in link for site in TRUSTED_SITES):
                    raw_results.append(f"- Titulo: {r.get('title')}\n  URL: {r.get('href')}\n  Snippet: {r.get('body')}")
    except Exception as e:
        print(f"Error DDG: {e}")
        return {"results": [], "summary": "Error de conexión externo", "price_range": None}

    if not raw_results:
        return {"results": [], "summary": "No se encontraron ofertas en tiendas oficiales.", "price_range": None}

    # 2. GEMINI FILTRA Y EXTRAE PRECIOS
    if not GEMINI_API_KEY:
        return {"results": [], "summary": "Falta API Key", "price_range": None}

    try:
        model = genai.GenerativeModel(
            "gemini-1.5-flash",
            generation_config={"response_mime_type": "application/json"}
        )

        prompt = f"""
        Eres un extractor de precios experto. Analiza estos resultados de búsqueda para UPC: {upc}.
        
        INPUT:
        {chr(10).join(raw_results)}

        INSTRUCCIONES:
        1. Extrae solo ofertas de productos disponibles.
        2. Busca el PRECIO en el snippet (ej: $120, 120 MXN). Si no está claro, pon null.
        3. Estandariza el nombre de la tienda (seller) basado en la URL.
        
        OUTPUT JSON:
        {{
            "offers": [
                {{
                    "title": "Nombre producto",
                    "price": 100.00,
                    "currency": "MXN",
                    "seller": "Amazon",
                    "link": "https://..."
                }}
            ],
            "summary": "Resumen de 1 linea",
            "price_range": {{ "min": 0, "max": 0 }}
        }}
        """

        response = model.generate_content(prompt)
        data = json.loads(response.text)
        
        return {
            "results": data.get("offers", []),
            "summary": data.get("summary", ""),
            "price_range": data.get("price_range", None)
        }

    except Exception as e:
        print(f"Error Gemini: {e}")
        return {"results": [], "summary": "Error procesando datos", "price_range": None}

# ===================== Handler Vercel =====================
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            
            query = data.get("query", "").strip()
            upc = _clean_upc(data.get("upc", ""))
            
            # Ejecutar lógica inteligente
            smart_data = _smart_search_with_gemini(query, upc)
            
            payload = {
                "organic_results": smart_data["results"],
                "gemini_summary": smart_data["summary"],
                "gemini_price_range": smart_data["price_range"],
                "powered_by": "gemini-whitelist"
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))