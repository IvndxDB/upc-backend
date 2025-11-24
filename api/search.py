from http.server import BaseHTTPRequestHandler
import json
import os
import re
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

# Configurar Gemini
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Límites de validación (más flexibles)
PRICE_MIN = 1
PRICE_MAX = 200000

# NUEVO: Dominios mexicanos conocidos (más flexible)
MEXICAN_DOMAINS = ['mx', 'com.mx']
KNOWN_RETAILERS = [
    'walmart', 'amazon', 'mercadolibre', 'linio', 'ebay',
    'chedraui', 'soriana', 'heb', 'lacomer', 'citymarket',
    'liverpool', 'suburbia', 'palaciodehierro', 'claroshop',
    'fahorro', 'farmaciasdelahorro', 'sanpablo', 'guadalajara',
    'benavides', 'yza', 'farmaciasguadalajara'
]

def _clean_upc(s: str) -> str:
    """Limpia UPC dejando solo dígitos"""
    return re.sub(r'\D+', '', s or '')

def _is_valid_domain(url: str) -> bool:
    """Verifica si es un dominio válido (mexicano O retailer conocido)"""
    try:
        domain = url.lower()
        
        # Prioridad 1: Dominios mexicanos
        if any(country in domain for country in MEXICAN_DOMAINS):
            return True
        
        # Prioridad 2: Retailers conocidos (incluso si son .com)
        if any(retailer in domain for retailer in KNOWN_RETAILERS):
            return True
        
        return False
    except:
        return False

def _validate_price(price) -> bool:
    """Valida que el precio esté en rango razonable"""
    if price is None:
        return False
    try:
        p = float(price)
        return PRICE_MIN <= p <= PRICE_MAX
    except:
        return False

def _scrape_google_search(query: str, num: int = 10, hl: str = 'es', gl: str = 'mx') -> list:
    """Scrapea resultados de Google Search"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': f'{hl},{hl[:2]};q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        
        params = {
            'q': query,
            'num': min(num, 20),
            'hl': hl,
            'gl': gl
        }
        
        url = f"https://www.google.com/search"
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        # Resultados orgánicos
        for g in soup.select('div.g'):
            title_elem = g.select_one('h3')
            link_elem = g.select_one('a')
            if not title_elem or not link_elem:
                continue
            
            title = title_elem.get_text(strip=True)
            link = link_elem.get('href')
            
            # Extraer snippet
            snippet_elem = g.select_one('.VwiC3b, .IsZvec')
            snippet = snippet_elem.get_text(" ", strip=True) if snippet_elem else ''
            
            if not link or not title:
                continue
            
            results.append({
                'title': title,
                'link': link,
                'snippet': snippet
            })
        
        return results[:num]
    except Exception as e:
        print(f"Error scraping Google: {e}")
        return []

def _analyze_with_gemini(query: str, upc: str, search_results: list) -> dict:
    """Envía resultados a Gemini para estructurar los productos relevantes"""
    try:
        if not GEMINI_API_KEY:
            raise ValueError("Gemini API key no configurada")
        
        model = genai.GenerativeModel(
            'gemini-1.5-flash',
            generation_config={
                "temperature": 0.3,
                "top_p": 0.8,
                "top_k": 40,
                "max_output_tokens": 1024,
            }
        )
        
        # Construir contexto con más resultados
        context = f"""Eres un asistente experto en análisis de resultados de búsqueda de productos.

Recibirás:
1. Un término de búsqueda (query)
2. Un posible código UPC
3. Una lista de resultados de Google (título, link, snippet)

Tu tarea es:
- Identificar cuáles resultados corresponden claramente a PRODUCTOS específicos relacionados con la búsqueda
- Extraer la mejor estimación de precio cuando sea posible (si no estás seguro, usa null)
- Identificar el vendedor / tienda (seller)
- Devolver SIEMPRE un JSON con una lista "products" y un campo "total_found"

FORMATO DE RESPUESTA (JSON válido, sin texto adicional):
{{
  "products": [
    {{
      "title": "Nombre del producto",
      "price": 1234.56,
      "currency": "MXN",
      "seller": "Nombre de la tienda o marketplace",
      "link": "https://...",
      "snippet": "Texto corto del resultado o descripción"
    }}
  ],
  "total_found": 1,
  "query_type": "search"
}}

REGLAS PARA PRECIOS:
- El precio debe estar entre ${PRICE_MIN} MXN y ${PRICE_MAX} MXN
- Si ves precios muy bajos ($2, $5, $7), NO los descartes automáticamente; si dudas, deja "price": null
- Si el precio no es claro, déjalo como null
- Es preferible incluir más productos aunque algunos tengan "price": null

IMPORTANTE: 
- Si un resultado es claramente un producto pero no tiene precio claro, inclúyelo con "price": null
- NO inventes precios si no aparecen en el texto
- No incluyas resultados que sean noticias, blogs, PDF o contenido que no sea claramente un producto
"""
        # Meter más resultados en el contexto
        context += "\nResultados encontrados:\n"
        for idx, result in enumerate(search_results[:25], 1):
            context += f"\n{idx}. Título: {result['title']}\n   Link: {result['link']}\n   Descripción: {result['snippet']}\n"
        
        prompt = f"""{context}

Ahora, analiza los resultados anteriores para la búsqueda:
- Query: "{query}"
- UPC (si se proporcionó): "{upc or 'N/A'}"

Devuelve ÚNICAMENTE el JSON solicitado (sin explicaciones, sin comentarios, sin markdown).
"""
        
        response = model.generate_content(prompt)
        result_text = response.text.strip()
        
        # Intentar limpiar si viene envuelto en ```json ...
        if result_text.startswith("```"):
            result_text = re.sub(r"^```json", "", result_text, flags=re.IGNORECASE).strip()
            result_text = re.sub(r"^```", "", result_text).strip()
            result_text = re.sub(r"```$", "", result_text).strip()
        
        # Intentar convertir a JSON directamente
        parsed = json.loads(result_text)
        
        # Validación MÁS PERMISIVA: casi nunca tiramos productos
        validated_products = []
        for product in parsed.get('products', []):
            # Campos mínimos obligatorios
            if not product.get('title') or not product.get('link'):
                continue

            # Si el precio existe pero se sale de rango, lo anulamos pero conservamos el producto
            price = product.get('price')
            if price is not None and not _validate_price(price):
                product['price'] = None

            validated_products.append(product)

        parsed['products'] = validated_products
        parsed['total_found'] = len(validated_products)
        
        return parsed
        
    except Exception as e:
        print(f"Error con Gemini: {e}")
        # Fallback - ACEPTAR casi todo lo scrapeado
        fallback_products = [
            {
                'title': r['title'],
                'price': None,
                'currency': 'MXN',
                'seller': r['link'].split('/')[2] if '/' in r['link'] else '',
                'link': r['link'],
                'snippet': r['snippet']
            }
            for r in search_results[:30]  # Más resultados
        ]
        return {
            'products': fallback_products,
            'total_found': len(fallback_products),
            'query_type': 'search',
            'summary': f'Se encontraron {len(search_results)} resultados para "{query}" (fallback sin Gemini)'
        }

class handler(BaseHTTPRequestHandler):
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_POST(self):
        try:
            content_len = int(self.headers.get('Content-Length', 0))
            if content_len <= 0:
                self._send_error(400, 'Body vacío')
                return
            
            body = self.rfile.read(content_len)
            data = json.loads(body.decode('utf-8'))
            
            query = data.get('query', '')
            upc = _clean_upc(data.get('upc', ''))
            
            if not query and not upc:
                self._send_error(400, 'Se requiere query o upc')
                return
            
            # Construir query final
            final_query = query
            if upc and upc not in query:
                final_query = f"{query} {upc}" if query else upc
            
            # Scraping de Google
            results = _scrape_google_search(final_query, num=20)
            
            # Enviar a Gemini
            analysis = _analyze_with_gemini(final_query, upc, results)
            
            self._send_success(analysis)
            
        except json.JSONDecodeError:
            self._send_error(400, 'JSON inválido')
        except Exception as e:
            print(f"Error en handler search: {e}")
            self._send_error(500, 'Error interno del servidor')
    
    def _send_success(self, data):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _send_error(self, code, message):
        self.send_response(code)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps({'error': message}, ensure_ascii=False).encode('utf-8'))
