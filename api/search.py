from http.server import BaseHTTPRequestHandler
import json
import os
import re
import requests
import google.generativeai as genai
from urllib.parse import urlparse

# ===================== Configuración =====================
SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '')
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# ===================== Helpers =====================
def _clean_upc(s):
    return re.sub(r"\D+", "", s or "")

def _extract_domain(url):
    try:
        netloc = urlparse(url).netloc.lower().replace('www.', '')
        # Extraer nombre principal (ej: walmart.com.mx -> walmart)
        return netloc.split('.')[0].capitalize()
    except: return "Desconocido"

def _deduplicate_by_domain(items):
    """
    Filtro matemático: Asegura que solo haya 1 item por tienda.
    Si hay repetidos (ej: 3 links de Amazon), se queda con el primero (el más relevante).
    """
    seen_domains = set()
    unique_items = []
    
    for item in items:
        # Normalizar el vendedor o dominio
        seller = item.get('seller') or _extract_domain(item.get('link', ''))
        seller_key = seller.lower().strip()
        
        # Corrección manual para variantes (ej: "Super" vs "Walmart")
        if 'walmart' in seller_key: seller_key = 'walmart'
        if 'aurrera' in seller_key: seller_key = 'bodega aurrera'
        
        if seller_key not in seen_domains:
            unique_items.append(item)
            seen_domains.add(seller_key)
            
    return unique_items

def _fetch_serpapi_organic(query):
    if not SERPAPI_KEY:
        print("⚠️ Falta SERPAPI_KEY")
        return []

    print(f"🌎 SerpApi Query: {query}")
    params = {
        'engine': 'google',
        'q': query,
        'api_key': SERPAPI_KEY,
        'hl': 'es', 
        'gl': 'mx', 
        'google_domain': 'google.com.mx',
        'num': '20' 
    }
    
    results = []
    try:
        resp = requests.get('https://serpapi.com/search.json', params=params, timeout=20)
        data = resp.json()
        
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
    if not raw_items: return [], "Sin resultados brutos"
    
    # 1. FALLBACK MANUAL (Si no hay IA)
    if not GEMINI_API_KEY: 
        fallback = []
        for r in raw_items:
            fallback.append({
                "title": r['title'],
                "price": None, 
                "currency": "MXN",
                "seller": _extract_domain(r['link']),
                "link": r['link']
            })
        # Aplicar deduplicación manual
        return _deduplicate_by_domain(fallback), "Sin API Key (Crudos Deduplicados)"

    # 2. INTENTO CON IA
    try:
        model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"response_mime_type": "application/json"})
        
        prompt = f"""
        Analiza estos resultados de búsqueda para UPC: {upc}.
        
        DATOS:
        {json.dumps(raw_items[:20], ensure_ascii=False)}

        INSTRUCCIONES:
        1. Devuelve una lista "offers".
        2. IMPORTANTE: ELIMINA DUPLICADOS. Si ves 3 resultados de Amazon, quédate SOLO CON EL MEJOR (el que parezca ser el producto principal).
        3. Solo 1 resultado por Dominio/Tienda.
        4. Extrae precio si es visible (formato numérico). Si no, null.
        5. Estandariza "seller" (ej: amazon.com.mx -> Amazon).

        OUTPUT JSON:
        {{
            "offers": [
                {{ "title": "...", "price": 100.00, "currency": "MXN", "seller": "Amazon", "link": "..." }}
            ],
            "summary": "Resumen"
        }}
        """
        
        resp = model.generate_content(prompt)
        data = json.loads(resp.text)
        
        # Doble seguridad: Pasamos el filtro matemático también a lo que devolvió Gemini
        # por si la IA alucinó y mandó duplicados de todas formas.
        cleaned_offers = _deduplicate_by_domain(data.get("offers", []))
        
        return cleaned_offers, data.get("summary", "")

    except Exception as e:
        print(f"⚠️ Error Gemini: {e}")
        # FALLBACK POR ERROR
        fallback = []
        for r in raw_items:
            fallback.append({
                "title": r['title'],
                "price": None,
                "currency": "MXN",
                "seller": _extract_domain(r['link']),
                "link": r['link']
            })
        return _deduplicate_by_domain(fallback), "Error IA (Fallback Deduplicado)"

# ===================== Handler =====================
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            
            query = data.get("query", "").strip()
            upc = _clean_upc(data.get("upc", ""))
            
            # Query Híbrida
            forced_sites = "site:walmart.com.mx OR site:bodegaaurrera.com.mx OR site:super.walmart.com.mx"
            
            if query:
                search_query = f"{upc} {query} (precio OR {forced_sites})"
            else:
                search_query = f"{upc} (precio OR {forced_sites})"
            
            search_query = search_query.strip()
            
            # 1. Traer datos
            raw_results = _fetch_serpapi_organic(search_query)
            
            if not raw_results:
                msg = "SerpApi no devolvió resultados"
                print(msg)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"organic_results": [], "gemini_summary": msg}).encode("utf-8"))
                return

            # 2. Procesar y Limpiar
            verified_items, summary = _analyze_with_gemini(raw_results, upc)
            
            payload = {
                "organic_results": verified_items,
                "gemini_summary": summary,
                "powered_by": "serpapi-organic-deduplicated"
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))