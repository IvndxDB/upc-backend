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

# Límites de validación
PRICE_MIN = 10
PRICE_MAX = 100000

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
            'tbm': 'shop',
            'hl': hl,
            'gl': gl
        }
        
        url = "https://www.google.com/search"
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'lxml')
        results = []
        
        # Buscar productos en Google Shopping
        product_divs = soup.find_all('div', class_=['sh-dgr__content', 'sh-dlr__content'])
        
        for product in product_divs[:30]:
            try:
                title_elem = product.find(['h3', 'h4', 'a'])
                title = title_elem.get_text(strip=True) if title_elem else ''
                
                link_elem = product.find('a', href=True)
                link = link_elem.get('href', '') if link_elem else ''
                if link and not link.startswith('http'):
                    link = 'https://www.google.com' + link
                
                price_elem = product.find(['span', 'b'], class_=re.compile(r'.*price.*', re.I))
                if not price_elem:
                    price_elem = product.find(['span', 'b'], string=re.compile(r'[$€£]\s*[\d,\.]+'))
                price_text = price_elem.get_text(strip=True) if price_elem else ''
                
                seller_elem = product.find(['div', 'span'], class_=re.compile(r'.*seller.*|.*store.*|.*merchant.*', re.I))
                seller = seller_elem.get_text(strip=True) if seller_elem else ''
                
                # ACEPTAR TODO
                if title and link:
                    results.append({
                        'title': title,
                        'price_text': price_text,
                        'seller': seller,
                        'link': link
                    })
            except Exception as e:
                continue
        
        # Fallback
        if not results:
            all_links = soup.find_all('a', href=re.compile(r'/shopping/product/'))
            for link_elem in all_links[:20]:
                try:
                    title = link_elem.get_text(strip=True)
                    link = link_elem.get('href', '')
                    if link and not link.startswith('http'):
                        link = 'https://www.google.com' + link
                    
                    if title and len(title) > 5:
                        results.append({
                            'title': title,
                            'price_text': '',
                            'seller': '',
                            'link': link
                        })
                except:
                    continue
        
        return results
    except Exception as e:
        print(f"Error scraping Google Shopping: {e}")
        return []

def _analyze_shopping_with_gemini(query: str, shopping_results: list) -> dict:
    """Usa Gemini para analizar resultados de shopping"""
    try:
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        
        context = f"""Analiza los siguientes resultados de Google Shopping para: "{query}"

Productos encontrados:
"""
        for idx, result in enumerate(shopping_results[:20], 1):
            context += f"\n{idx}. Producto: {result['title']}\n"
            if result.get('price_text'):
                context += f"   Precio mencionado: {result['price_text']}\n"
            if result.get('seller'):
                context += f"   Vendedor: {result['seller']}\n"
            context += f"   Link: {result['link']}\n"
        
        prompt = f"""{context}

Tu tarea es extraer y estructurar las ofertas de productos de estos resultados de shopping.

Para cada producto:
1. **Extrae el nombre limpio** del producto
2. **Extrae el precio numérico** del texto (IMPORTANTE: debe estar entre ${PRICE_MIN} y ${PRICE_MAX} MXN)
3. **Identifica la moneda** (MXN, USD, EUR, etc.)
4. **Identifica el vendedor/tienda** del sitio web
5. **Incluye el link**

REGLAS PARA PRECIOS:
- Precio MÍNIMO: ${PRICE_MIN} MXN
- Precio MÁXIMO: ${PRICE_MAX} MXN
- Si ves "$2.00" o "$7.00", probablemente es PRECIO POR UNIDAD - ignóralo
- Si ves descuentos del 98%, probablemente es un error - ignóralo
- SOLO incluye precios que parezcan razonables para el producto
- Si el precio no es claro, déjalo como null

FORMATO (JSON válido sin markdown):
{{
  "query": "{query}",
  "offers": [
    {{
      "title": "Nombre limpio del producto",
      "price": 125.50,
      "currency": "MXN",
      "seller": "Nombre de la tienda",
      "link": "URL completa"
    }}
  ],
  "total_offers": 10,
  "price_range": {{
    "min": 100.00,
    "max": 200.00,
    "avg": 150.00,
    "currency": "MXN"
  }},
  "summary": "Resumen de las ofertas encontradas"
}}

IMPORTANTE:
- Responde SOLO con JSON válido, sin markdown ni texto adicional
- Incluye TODOS los productos con información relevante
- Si hay múltiples ofertas del mismo producto, inclúyelas todas
- Calcula el price_range solo con precios válidos"""

        response = model.generate_content(prompt)
        result_text = response.text.strip()
        
        # Limpiar markdown
        result_text = result_text.replace('```json', '').replace('```', '').strip()
        
        parsed = json.loads(result_text)
        
        # Validación MÍNIMA
        validated_offers = []
        for offer in parsed.get('offers', []):
            # Solo validar campos mínimos
            if not offer.get('title') or not offer.get('link'):
                continue
            
            # Validar precio solo si existe
            price = offer.get('price')
            if price is not None and not _validate_price(price):
                continue
            
            validated_offers.append(offer)
        
        parsed['offers'] = validated_offers
        parsed['total_offers'] = len(validated_offers)
        
        # Recalcular price_range
        if validated_offers:
            valid_prices = [o['price'] for o in validated_offers if o.get('price') is not None]
            if valid_prices:
                parsed['price_range'] = {
                    'min': min(valid_prices),
                    'max': max(valid_prices),
                    'avg': sum(valid_prices) / len(valid_prices),
                    'currency': 'MXN'
                }
        
        return parsed
        
    except Exception as e:
        print(f"Error con Gemini en shopping: {e}")
        return {
            'query': query,
            'offers': [
                {
                    'title': r['title'],
                    'price': None,
                    'currency': 'MXN',
                    'seller': r.get('seller', ''),
                    'link': r['link']
                }
                for r in shopping_results[:20]
            ],
            'total_offers': len(shopping_results),
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
            if not GEMINI_API_KEY:
                return self._send_error(500, 'API Key de Gemini no configurada')
            
            body_len = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(body_len).decode('utf-8'))
            
            query = (payload.get('query') or '').strip()
            hl = payload.get('hl', 'es')
            gl = payload.get('gl', 'mx')
            
            if not query:
                return self._send_error(400, 'query es requerido')

            # 1. Scrapear Google Shopping
            shopping_results = _scrape_google_shopping(query, hl, gl)
            
            if not shopping_results:
                return self._send_error(404, 'No se encontraron productos en Google Shopping')

            # 2. Analizar con Gemini
            analyzed_data = _analyze_shopping_with_gemini(query, shopping_results)
            
            # 3. Agregar metadata
            analyzed_data['search_engine'] = 'google_shopping_scraping'
            analyzed_data['powered_by'] = 'gemini-2.0-flash'
            analyzed_data['raw_count'] = len(shopping_results)
            analyzed_data['validation'] = {
                'total_scraped': len(shopping_results),
                'total_validated': len(analyzed_data.get('offers', [])),
                'price_range_filter': f'{PRICE_MIN}-{PRICE_MAX} MXN'
            }

            return self._send_success(analyzed_data)

        except json.JSONDecodeError:
            return self._send_error(400, 'JSON inválido')
        except requests.RequestException as e:
            return self._send_error(500, f'Error de scraping: {str(e)}')
        except Exception as e:
            return self._send_error(500, f'Error: {str(e)}')

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
