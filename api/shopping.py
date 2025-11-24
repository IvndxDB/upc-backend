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

def _validate_price(price) -> bool:
    """Valida que el precio esté en rango razonable"""
    if price is None:
        return False
    try:
        p = float(price)
        return PRICE_MIN <= p <= PRICE_MAX
    except:
        return False

def _scrape_google_shopping(query: str, hl: str = 'es', gl: str = 'mx') -> list:
    """Scrapea resultados de Google Shopping"""
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
            'hl': hl,
            'gl': gl,
            'tbm': 'shop'
        }
        
        url = "https://www.google.com/search"
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        # Google Shopping blocks
        for item in soup.select('div.sh-dgr__grid-result'):
            title_elem = item.select_one('h3, span.OSrXXb')
            link_elem = item.select_one('a.shntl, a.eIuuYe')
            price_elem = item.select_one('span.a8Pemb, span.dD8iuc')
            seller_elem = item.select_one('div.aULzUe, span.aULzUe')
            
            if not title_elem or not link_elem:
                continue
            
            title = title_elem.get_text(strip=True)
            link = link_elem.get('href')
            if link and link.startswith('/url?'):
                # Quitar prefijo /url?q=...
                m = re.search(r'/url\?q=([^&]+)', link)
                if m:
                    link = m.group(1)
            
            price_text = price_elem.get_text(strip=True) if price_elem else ''
            seller = seller_elem.get_text(strip=True) if seller_elem else ''
            
            # Intentar extraer número de precio
            price_value = None
            if price_text:
                m_price = re.search(r'([\d.,]+)', price_text)
                if m_price:
                    try:
                        raw = m_price.group(1).replace('.', '').replace(',', '.')
                        price_value = float(raw)
                    except:
                        price_value = None
            
            results.append({
                'title': title,
                'link': link,
                'price_text': price_text,
                'price': price_value,
                'currency': 'MXN',
                'seller': seller
            })
        
        return results
    except Exception as e:
        print(f"Error scraping Google Shopping: {e}")
        return []

def _analyze_with_gemini(query: str, upc: str, shopping_results: list) -> dict:
    """Envía resultados de Google Shopping a Gemini para estructurarlos"""
    try:
        if not GEMINI_API_KEY:
            raise ValueError("Gemini API key no configurada")
        
        model = genai.GenerativeModel(
            'gemini-1.5-flash',
            generation_config={
                "temperature": 0.25,
                "top_p": 0.8,
                "top_k": 40,
                "max_output_tokens": 1024,
            }
        )
        
        context = f"""Eres un asistente experto en análisis de resultados de Google Shopping.

Recibirás:
1. Un término de búsqueda (query)
2. Un posible código UPC
3. Una lista de resultados de Google Shopping (título, link, precio detectado, vendedor)

Tu tarea es:
- Identificar cuáles resultados corresponden claramente a PRODUCTOS específicos relacionados con la búsqueda
- Normalizar el precio en formato numérico (cuando sea posible)
- Identificar el vendedor / tienda (seller)
- Devolver SIEMPRE un JSON con una lista "offers" y un campo "total_offers"

FORMATO DE RESPUESTA (JSON válido, sin texto adicional):
{{
  "offers": [
    {{
      "title": "Nombre del producto",
      "price": 1234.56,
      "currency": "MXN",
      "seller": "Nombre de la tienda o marketplace",
      "link": "https://...",
      "origin": "google_shopping",
      "price_text": "texto original de precio si aplica"
    }}
  ],
  "total_offers": 1,
  "query_type": "shopping",
  "price_range": {{
    "min": 123.45,
    "max": 234.56
  }}
}}

INSTRUCCIONES:
- Si el resultado parece claramente un producto que coincide con la búsqueda y/o UPC, inclúyelo.
- Si no ves un precio claro, deja "price": null pero NO inventes el valor.
- Usa "currency": "MXN" para México, a menos que el precio claramente sea en otra moneda.

REGLAS PARA PRECIOS:
- Precio MÍNIMO: ${PRICE_MIN} MXN
- Precio MÁXIMO: ${PRICE_MAX} MXN
- Si ves precios muy bajos como "$2.00" o "$7.00", NO los descartes; si dudas, deja "price": null
- Si ves descuentos extremos (ej. 95%+), puedes dejar "price": null si no estás seguro
- Es preferible incluir más ofertas aunque algunas tengan "price": null
- Si el precio no es claro, déjalo como null

Ejemplo de respuesta válida:
{{
  "offers": [
    {{
      "title": "Shampoo XYZ 750ml",
      "price": 89.90,
      "currency": "MXN",
      "seller": "Walmart",
      "link": "https://www.walmart.com.mx/...",
      "origin": "google_shopping",
      "price_text": "$89.90"
    }}
  ],
  "total_offers": 1,
  "query_type": "shopping",
  "price_range": {{
    "min": 89.9,
    "max": 89.9
  }}
}}

Productos encontrados:
"""
        for idx, result in enumerate(shopping_results[:30], 1):
            context += f"\n{idx}. Producto: {result['title']}\n"
            if result.get('price_text'):
                context += f"   Precio mencionado: {result['price_text']}\n"
            if result.get('price') is not None:
                context += f"   Precio numérico detectado: {result['price']}\n"
            if result.get('seller'):
                context += f"   Vendedor: {result['seller']}\n"
            context += f"   Link: {result['link']}\n"
        
        prompt = f"""{context}

Ahora, analiza estos resultados de Google Shopping para la búsqueda:
- Query: "{query}"
- UPC (si se proporcionó): "{upc or 'N/A'}"

Devuelve ÚNICAMENTE el JSON especificado, sin explicación adicional, sin comentarios y sin markdown.
"""
        
        response = model.generate_content(prompt)
        result_text = response.text.strip()
        
        # Limpiar si viene con ```json
        if result_text.startswith("```"):
            result_text = re.sub(r"^```json", "", result_text, flags=re.IGNORECASE).strip()
            result_text = re.sub(r"^```", "", result_text).strip()
            result_text = re.sub(r"```$", "", result_text).strip()
        
        parsed = json.loads(result_text)
        
        # Validación MÍNIMA pero permisiva con el precio
        validated_offers = []
        for offer in parsed.get('offers', []):
            # Solo validar campos mínimos obligatorios
            if not offer.get('title') or not offer.get('link'):
                continue

            price = offer.get('price')
            if price is not None and not _validate_price(price):
                # Conservamos la oferta pero anulamos el precio dudoso
                offer['price'] = None

            validated_offers.append(offer)

        parsed['offers'] = validated_offers
        parsed['total_offers'] = len(validated_offers)
        
        # Recalcular price_range
        if validated_offers:
            valid_prices = [o['price'] for o in validated_offers if o.get('price') is not None]
            if valid_prices:
                parsed['price_range'] = {
                    'min': min(valid_prices),
                    'max': max(valid_prices)
                }
        
        return parsed
    
    except Exception as e:
        print(f"Error con Gemini Shopping: {e}")
        # Fallback - devolver lo scrapeado en formato estándar
        fallback_offers = []
        for r in shopping_results[:40]:
            fallback_offers.append({
                'title': r['title'],
                'price': r.get('price'),
                'currency': r.get('currency', 'MXN'),
                'seller': r.get('seller', ''),
                'link': r.get('link'),
                'origin': 'shopping_fallback',
                'price_text': r.get('price_text', '')
            })
        
        return {
            'offers': fallback_offers,
            'total_offers': len(fallback_offers),
            'query_type': 'shopping',
            'summary': f'Se encontraron {len(shopping_results)} productos para "{query}"'
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
            upc = re.sub(r'\D+', '', data.get('upc', '') or '')
            
            if not query and not upc:
                self._send_error(400, 'Se requiere query o upc')
                return
            
            final_query = query
            if upc and upc not in query:
                final_query = f"{query} {upc}" if query else upc
            
            shopping_results = _scrape_google_shopping(final_query)
            analysis = _analyze_with_gemini(final_query, upc, shopping_results)
            
            self._send_success(analysis)
        
        except json.JSONDecodeError:
            self._send_error(400, 'JSON inválido')
        except Exception as e:
            print(f"Error en handler shopping: {e}")
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
