from http.server import BaseHTTPRequestHandler
import json
import os
import re
import requests
import google.generativeai as genai

# ===================== Configuración =====================
SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '')
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

def _clean_upc(s):
    return re.sub(r"\D+", "", s or "")

def _fetch_serpapi_organic(query):
    """
    Usa SerpApi en modo ORGANICO (no shopping) para traer más resultados.
    """
    if not SERPAPI_KEY: return []

    print(f"🌎 SerpApi Orgánico: {query}")
    params = {
        'engine': 'google',
        'q': query,
        'api_key': SERPAPI_KEY,
        'hl': 'es', 
        'gl': 'mx', 
        'google_domain': 'google.com.mx',
        'num': '20' # Pedimos 20 para tener variedad
    }
    
    results = []
    try:
        resp = requests.get('https://serpapi.com/search.json', params=params, timeout=20)
        data = resp.json()
        
        # Procesamos resultados orgánicos
        for r in data.get('organic_results', []):
            results.append({
                'title': r.get('title'),
                'link': r.get('link'),
                'snippet': r.get('snippet', ''),
                'rich_snippet': r.get('rich_snippet', {}).get('top', {}).get('detected_extensions', {})
            })
            
    except Exception as e:
        print(f"Error SerpApi: {e}")
        
    return results

def _analyze_with_gemini(raw_items, upc):
    """
    Gemini actúa como EXTRACTOR: Lee el título y snippet para hallar el precio.
    """
    if not raw_items: return [], "Sin resultados"
    
    if not GEMINI_API_KEY:
        return raw_items, "Sin IA"

    try:
        model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"response_mime_type": "application/json"})
        
        # Le damos los datos crudos a Gemini
        prompt = f"""
        Analiza estos resultados de búsqueda para UPC: {upc}.
        
        INPUT DATOS:
        {json.dumps(raw_items[:20], ensure_ascii=False)}

        INSTRUCCIONES:
        1. Identifica ofertas de TIENDAS (Amazon, Walmart, Chedraui, La Comer, Farmacias, etc).
        2. EXTRAE EL PRECIO del 'snippet' o 'rich_snippet'. 
           - Busca formatos como "$100", "MXN 100", "$100.00".
           - Si no hay precio visible, pon "price": null (mi sistema lo buscará después).
        3. Estandariza el nombre de la tienda ("seller").
        4. Descarta PDFs, noticias o sitios de cupones basura.

        OUTPUT JSON:
        {{
            "offers": [
                {{ 
                    "title": "...", 
                    "price": 120.00, 
                    "currency": "MXN", 
                    "seller": "Walmart", 
                    "link": "..." 
                }},
                {{ 
                    "title": "...", 
                    "price": null, 
                    "currency": "MXN", 
                    "seller": "Chedraui", 
                    "link": "..." 
                }}
            ],
            "summary": "Resumen breve"
        }}
        """
        
        resp = model.generate_content(prompt)
        data = json.loads(resp.text)
        return data.get("offers", []), data.get("summary", "")

    except Exception as e:
        print(f"Error Gemini: {e}")
        return [], "Error IA"

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            
            query = data.get("query", "").strip()
            upc = _clean_upc(data.get("upc", ""))
            
            # Búsqueda amplia para encontrar Chedraui, La Comer, etc.
            search_query = f"{query} {upc} precio".strip()
            
            # 1. Traer datos crudos (muchos)
            raw_results = _fetch_serpapi_organic(search_query)
            
            # 2. IA filtra y extrae precios
            verified_items, summary = _analyze_with_gemini(raw_results, upc)
            
            payload = {
                "organic_results": verified_items,
                "gemini_summary": summary,
                "powered_by": "serpapi-organic-gemini"
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))