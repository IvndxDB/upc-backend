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
    Usa SerpApi en modo ORGANICO.
    """
    if not SERPAPI_KEY:
        print("⚠️ Falta SERPAPI_KEY")
        return []

    print(f"🌎 SerpApi Orgánico: {query}")
    params = {
        'engine': 'google',
        'q': query,
        'api_key': SERPAPI_KEY,
        'hl': 'es', 
        'gl': 'mx', 
        'google_domain': 'google.com.mx',
        'num': '15' 
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
    Intenta extraer precios con Gemini.
    """
    if not raw_items: return [], "Sin resultados brutos"
    if not GEMINI_API_KEY: return raw_items, "Sin API Key de Gemini (Mostrando crudos)"

    try:
        model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"response_mime_type": "application/json"})
        
        prompt = f"""
        Analiza estos resultados de búsqueda para UPC: {upc}.
        
        DATOS:
        {json.dumps(raw_items[:15], ensure_ascii=False)}

        INSTRUCCIONES:
        1. Devuelve una lista "offers" con los productos encontrados.
        2. Intenta extraer el precio. Si no hay, pon null.
        3. Estandariza el "seller" (ej: amazon.com.mx -> Amazon).
        4. NO filtres agresivamente. Si parece una tienda, inclúyelo.

        OUTPUT JSON:
        {{
            "offers": [
                {{ "title": "...", "price": 100.00, "currency": "MXN", "seller": "...", "link": "..." }}
            ],
            "summary": "Resumen"
        }}
        """
        
        resp = model.generate_content(prompt)
        data = json.loads(resp.text)
        return data.get("offers", []), data.get("summary", "")

    except Exception as e:
        print(f"⚠️ Error Gemini: {e}")
        # FALLBACK MANUAL: Si Gemini falla, devolvemos los datos crudos formateados
        # para que el frontend no se quede vacío.
        fallback = []
        for r in raw_items:
            fallback.append({
                "title": r['title'],
                "price": None, # El frontend lo buscará
                "currency": "MXN",
                "seller": _extract_seller_from_url(r['link']),
                "link": r['link']
            })
        return fallback, "Error IA (Fallback Activado)"

def _extract_seller_from_url(url):
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower().replace('www.', '')
        return domain.split('.')[0].capitalize()
    except: return "Desconocido"

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            
            query = data.get("query", "").strip()
            upc = _clean_upc(data.get("upc", ""))
            
            # Construir query robusta
            search_query = f"{query} {upc} precio".strip()
            
            # 1. Obtener datos crudos
            raw_results = _fetch_serpapi_organic(search_query)
            
            if not raw_results:
                # Si SerpApi devolvió vacío, avisar
                msg = "SerpApi no devolvió resultados (Revisar API Key)"
                print(msg)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"organic_results": [], "gemini_summary": msg}).encode("utf-8"))
                return

            # 2. Procesar con IA (o Fallback)
            verified_items, summary = _analyze_with_gemini(raw_results, upc)
            
            payload = {
                "organic_results": verified_items,
                "gemini_summary": summary,
                "powered_by": "serpapi-organic"
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))