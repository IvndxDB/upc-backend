from http.server import BaseHTTPRequestHandler
import json
import os
import re
import urllib.parse
import requests
import google.generativeai as genai
import statistics

# ===================== Configuración =====================
SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '')
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# ===================== Helpers =====================
def _clean_upc(s):
    return re.sub(r"\D+", "", s or "")

def _fetch_serpapi(query):
    """Trae resultados crudos de SerpApi (Shopping + Organic)"""
    if not SERPAPI_KEY:
        print("Falta SERPAPI_KEY")
        return []

    print(f"🌎 Consultando SerpApi: {query}")
    params = {
        'engine': 'google',
        'q': query,
        'api_key': SERPAPI_KEY,
        'hl': 'es', 
        'gl': 'mx', 
        'google_domain': 'google.com.mx',
        'num': '15',
        'tbm': 'shop' # Priorizamos Shopping para precios
    }
    
    results = []
    try:
        # 1. Búsqueda Shopping (Mejor para precios)
        resp = requests.get('https://serpapi.com/search.json', params=params, timeout=15)
        data = resp.json()
        
        # Procesar Shopping Results
        for r in data.get('shopping_results', []):
            results.append({
                'title': r.get('title'),
                'link': r.get('link'),
                'price_raw': r.get('extracted_price') or r.get('price'),
                'seller': r.get('source'),
                'source': 'shopping'
            })
            
        # 2. Si hay pocos resultados, intentar búsqueda orgánica normal
        if len(results) < 5:
            params.pop('tbm') # Quitar modo shopping
            resp_org = requests.get('https://serpapi.com/search.json', params=params, timeout=15)
            data_org = resp_org.json()
            
            for r in data_org.get('organic_results', []):
                # Intentar sacar precio del snippet
                snippet = r.get('snippet', '')
                price_match = re.search(r'\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', snippet)
                price = float(price_match.group(1).replace(',', '')) if price_match else None
                
                results.append({
                    'title': r.get('title'),
                    'link': r.get('link'),
                    'price_raw': price,
                    'seller': urllib.parse.urlparse(r.get('link')).netloc.replace('www.',''),
                    'source': 'organic'
                })

    except Exception as e:
        print(f"Error SerpApi: {e}")
        
    return results

def _audit_with_gemini(raw_items, upc):
    """
    Gemini analiza los precios. Si detecta 'outliers' (precios ridículamente bajos/altos),
    los marca como null para forzar una re-verificación en el frontend.
    """
    if not raw_items: return [], "Sin resultados", None
    
    # Pre-cálculo simple para ayudar a Gemini
    valid_prices = []
    for i in raw_items:
        try:
            p = float(str(i['price_raw']).replace('$','').replace(',',''))
            if p > 0: valid_prices.append(p)
        except: pass
    
    avg_price = statistics.median(valid_prices) if valid_prices else 0

    if not GEMINI_API_KEY:
        # Sin Gemini, devolvemos tal cual
        return raw_items, "Análisis local (Sin IA)", {"avg": avg_price}

    try:
        model = genai.GenerativeModel("gemini-1.5-flash", generation_config={"response_mime_type": "application/json"})
        
        prompt = f"""
        Actúa como auditor de precios.
        Producto buscado UPC: {upc}.
        Precio Mediano estimado: ${avg_price}

        LISTA DE CANDIDATOS:
        {json.dumps(raw_items[:20], ensure_ascii=False)}

        INSTRUCCIONES:
        1. Estandariza el 'seller' (ej: 'www.walmart.com.mx' -> 'Walmart').
        2. ANALISIS DE PRECIO (CRITICO):
           - Si un precio es MUY bajo comparado con la mediana (ej: $7 vs $100), es probable que sea un accesorio, costo de envío o error.
           - Si detectas esto, pon "price": null y "flag": "suspicious_low".
           - Si el precio parece correcto, mantenlo como número.
        3. Elimina resultados duplicados o irrelevantes (PDFs, blogs).

        OUTPUT JSON:
        {{
            "verified_items": [
                {{ "title": "...", "price": 120.00, "currency": "MXN", "seller": "Walmart", "link": "...", "flag": "ok" }},
                {{ "title": "...", "price": null, "currency": "MXN", "seller": "Amazon", "link": "...", "flag": "suspicious_low" }}
            ],
            "summary": "Resumen del análisis (ej: 'Se detectaron 2 precios erróneos que requieren verificación')."
        }}
        """
        
        resp = model.generate_content(prompt)
        data = json.loads(resp.text)
        return data.get("verified_items", []), data.get("summary", ""), {"avg": avg_price}

    except Exception as e:
        print(f"Error Gemini: {e}")
        # Fallback: devolver lo que teníamos
        return raw_items, "Error en IA, mostrando datos crudos", None

# ===================== Handler =====================
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            
            query = data.get("query", "").strip()
            upc = _clean_upc(data.get("upc", ""))
            
            # 1. Búsqueda Fuerte (SerpApi)
            search_query = f"{query} {upc}".strip()
            raw_results = _fetch_serpapi(search_query)
            
            # 2. Auditoría Inteligente (Gemini)
            # Aquí es donde Gemini decide si el precio es real o basura
            verified_items, summary, meta = _audit_with_gemini(raw_results, upc)
            
            # Nota: Los items que Gemini marque con price: null serán procesados
            # automáticamente por 'enrichOneSlow' en tu background.js, 
            # haciendo esa "búsqueda específica" que pediste.

            payload = {
                "organic_results": verified_items,
                "gemini_summary": summary,
                "gemini_metadata": meta,
                "powered_by": "serpapi-gemini-auditor"
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))