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

# ===================== LISTA BLANCA DE TIENDAS =====================
# Solo buscaremos resultados dentro de estos dominios confiables
TRUSTED_SITES = [
    "amazon.com.mx",
    "mercadolibre.com.mx",
    "walmart.com.mx",
    "bodegaaurrera.com.mx",
    "super.walmart.com.mx",
    "chedraui.com.mx",
    "soriana.com",
    "lacomer.com.mx",
    "liverpool.com.mx",
    "fahorro.com",           # Farmacias del Ahorro
    "farmaciasguadalajara.com",
    "farmaciasanpablo.com.mx",
    "benavides.com.mx",
    "sanborns.com.mx",
    "sears.com.mx",
    "coppel.com",
    "elektra.mx",
    "hebmexico.com",
    "costco.com.mx",
    "sams.com.mx"
]

# ===================== Helpers =====================
def _clean_upc(s):
    return re.sub(r"\D+", "", s or "")

def _build_targeted_query(upc, product_name):
    """
    Construye una query que fuerza al buscador a mirar SOLO en sitios confiables.
    Ejemplo: '750100... (site:amazon.com.mx OR site:walmart.com.mx ...)'
    """
    # Dividimos los sitios en grupos para no saturar la query (DDG tiene limite de caracteres)
    # Priorizamos los top 5 para la búsqueda principal
    top_sites = " OR ".join([f"site:{site}" for site in TRUSTED_SITES[:8]])
    
    # Si tenemos nombre del producto, ayuda mucho
    base = f"{product_name} {upc}" if product_name else upc
    
    return f"{base} ({top_sites})"

def _smart_search_with_gemini_filter(query: str, upc: str) -> dict:
    """
    1. Busca en DDG restringido a sitios mexicanos confiables.
    2. Usa Gemini para limpiar y estructurar el precio.
    """
    
    # Construimos la búsqueda "quirúrgica"
    targeted_query = _build_targeted_query(upc, query)
    print(f"🔎 Query restringida: {targeted_query}")
    
    raw_results = []
    
    try:
        with DDGS() as ddgs:
            # Buscamos en región MX
            ddg_gen = ddgs.text(
                targeted_query, 
                region='mx-es', 
                safesearch='off',
                max_results=15  # 15 resultados de calidad valen más que 50 de basura
            )
            
            for r in ddg_gen:
                # Pre-validación rápida: ¿El link contiene alguno de nuestros sitios confiables?
                link = r.get('href', '').lower()
                if any(site in link for site in TRUSTED_SITES):
                    raw_results.append(f"- Titulo: {r.get('title')}\n  URL: {link}\n  Texto: {r.get('body')}")
                    
    except Exception as e:
        print(f"Error DDG: {e}")
        return {"results": [], "summary": "Error externo", "price_range": None}

    if not raw_results:
        # Fallback: Si la búsqueda restringida falla, intentamos una búsqueda abierta simple
        try:
             with DDGS() as ddgs:
                fallback_query = f"{query} {upc} precio"
                print(f"⚠️ Fallback a búsqueda abierta: {fallback_query}")
                for r in ddgs.text(fallback_query, region='mx-es', max_results=5):
                    raw_results.append(f"- Titulo: {r.get('title')}\n  URL: {r.get('href')}\n  Texto: {r.get('body')}")
        except: pass

    if not raw_results:
         return {"results": [], "summary": "No se encontraron productos.", "price_range": None}

    # --- GEMINI: EL EXTRACTOR ---
    if not GEMINI_API_KEY:
        return {"results": [], "summary": "Falta API Key", "price_range": None}

    try:
        model = genai.GenerativeModel(
            "gemini-1.5-flash",
            generation_config={"response_mime_type": "application/json"}
        )

        prompt = f"""
        Analiza estos resultados de búsqueda para el producto UPC: {upc} ({query}).
        
        OBJETIVO: Extraer ofertas válidas de tiendas mexicanas.
        
        INPUT DATOS CRUDOS:
        {chr(10).join(raw_results)}

        INSTRUCCIONES:
        1. Ignora resultados que no parezcan páginas de producto (blogs, pdfs).
        2. Intenta identificar el PRECIO ACTUAL en MXN (busca signos $ o 'precio').
        3. Si no encuentras precio en el texto, pon null (el frontend lo buscará después).
        4. Estandariza el nombre de la tienda (seller) basado en la URL.

        OUTPUT JSON:
        {{
            "offers": [
                {{
                    "title": "...",
                    "price": 120.00,
                    "currency": "MXN",
                    "seller": "Walmart",
                    "link": "..."
                }}
            ],
            "summary": "Resumen breve de disponibilidad",
            "price_range": {{ "min": 100, "max": 200 }}
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
        return {"results": [], "summary": "Error procesando", "price_range": None}


# ===================== Handler =====================
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            
            query = data.get("query", "").strip()
            upc = _clean_upc(data.get("upc", ""))
            
            # Lógica inteligente
            smart_data = _smart_search_with_gemini_filter(query, upc)
            
            payload = {
                "organic_results": smart_data["results"],
                "gemini_summary": smart_data["summary"],
                "gemini_price_range": smart_data["price_range"],
                "powered_by": "gemini-smart-filter"
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))