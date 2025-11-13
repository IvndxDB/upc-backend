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

def _scrape_google_search(query: str, num: int = 20, hl: str = 'es', gl: str = 'mx') -> list:
    """Scrapea resultados de búsqueda de Google"""
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
        
        soup = BeautifulSoup(response.text, 'lxml')
        results = []
        
        # Buscar divs de resultados orgánicos
        search_results = soup.find_all('div', class_='g')
        
        for result in search_results[:num]:
            try:
                # Título
                title_elem = result.find('h3')
                title = title_elem.get_text() if title_elem else ''
                
                # Link
                link_elem = result.find('a')
                link = link_elem.get('href', '') if link_elem else ''
                
                # Snippet
                snippet_elem = result.find('div', class_=['VwiC3b', 'yXK7lf'])
                snippet = snippet_elem.get_text() if snippet_elem else ''
                
                # NUEVO: Aceptar TODOS los resultados, dejar que Gemini filtre
                if title and link:
                    results.append({
                        'title': title,
                        'link': link,
                        'snippet': snippet
                    })
            except Exception as e:
                continue
        
        return results
    except Exception as e:
        print(f"Error scraping Google: {e}")
        return []

def _analyze_with_gemini(query: str, search_results: list) -> dict:
    """Usa Gemini para analizar los resultados de búsqueda"""
    try:
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        
        # Preparar contexto para Gemini
        context = f"""Analiza los siguientes resultados de búsqueda para la query: "{query}"

Resultados encontrados:
"""
        for idx, result in enumerate(search_results[:15], 1):
            context += f"\n{idx}. Título: {result['title']}\n   Link: {result['link']}\n   Descripción: {result['snippet']}\n"
        
        prompt = f"""{context}

Tu tarea es extraer y estructurar información de productos de estos resultados.

REGLAS PARA PRECIOS:
- El precio debe estar entre ${PRICE_MIN} MXN y ${PRICE_MAX} MXN
- Si ves precios muy bajos ($2, $5, $7), probablemente es precio POR UNIDAD - ignóralo
- Si el precio no es claro, déjalo como null
- Incluye productos de sitios conocidos de e-commerce

IMPORTANTE: 
- Si un resultado es claramente un producto pero no tiene precio visible, inclúyelo con price: null
- Prioriza resultados de tiendas mexicanas conocidas (Walmart, Amazon MX, Chedraui, La Comer, etc.)
- Responde SOLO con JSON válido, sin markdown

{{
  "products": [
    {{
      "title": "Nombre del producto",
      "price": 125.50,
      "currency": "MXN",
      "seller": "Nombre de la tienda",
      "link": "URL completa",
      "snippet": "Descripción breve"
    }}
  ],
  "total_found": 10,
  "query_type": "product_search",
  "summary": "Resumen breve de lo encontrado"
}}

Responde SOLO con JSON, sin texto adicional."""

        response = model.generate_content(prompt)
        result_text = response.text.strip()
        
        # Limpiar markdown
        result_text = result_text.replace('```json', '').replace('```', '').strip()
        
        parsed = json.loads(result_text)
        
        # NUEVO: Validación MÁS PERMISIVA (solo rechaza basura obvia)
        validated_products = []
        for product in parsed.get('products', []):
            # Solo validar que tenga campos mínimos
            if not product.get('title') or not product.get('link'):
                continue
            
            # Validar precio solo si existe (permitir null)
            price = product.get('price')
            if price is not None and not _validate_price(price):
                continue
            
            # ACEPTAR TODO LO DEMÁS (sin filtro de dominio aquí)
            validated_products.append(product)
        
        parsed['products'] = validated_products
        parsed['total_found'] = len(validated_products)
        
        return parsed
        
    except Exception as e:
        print(f"Error con Gemini: {e}")
        # Fallback - ACEPTAR TODOS
        return {
            'products': [
                {
                    'title': r['title'],
                    'price': None,
                    'currency': 'MXN',
                    'seller': r['link'].split('/')[2] if '/' in r['link'] else '',
                    'link': r['link'],
                    'snippet': r['snippet']
                }
                for r in search_results[:15]  # Más resultados
            ],
            'total_found': len(search_results),
            'query_type': 'search',
            'summary': f'Se encontraron {len(search_results)} resultados para "{query}"'
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
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode('utf-8'))

            query_raw = (data.get('query') or '').strip()
            upc_raw = data.get('upc', '')
            upc = _clean_upc(upc_raw)

            num = int(data.get('num', 20))
            hl = data.get('hl', 'es')
            gl = data.get('gl', 'mx')

            if not GEMINI_API_KEY:
                return self._send_error(500, 'API Key de Gemini no configurada')

            # Determinar query
            if query_raw:
                q = query_raw
            else:
                if not upc:
                    return self._send_error(400, 'UPC es requerido (o usa "query")')
                q = upc

            # 1. Scrapear Google
            search_results = _scrape_google_search(q, num, hl, gl)
            
            if not search_results:
                return self._send_error(404, 'No se encontraron resultados')

            # 2. Analizar con Gemini
            analyzed_data = _analyze_with_gemini(q, search_results)
            
            # 3. Agregar metadata
            analyzed_data['raw_results'] = search_results[:5]
            analyzed_data['search_engine'] = 'google_scraping'
            analyzed_data['powered_by'] = 'gemini-2.0-flash'
            analyzed_data['validation'] = {
                'total_scraped': len(search_results),
                'total_validated': len(analyzed_data.get('products', [])),
                'price_range_filter': f'{PRICE_MIN}-{PRICE_MAX} MXN',
                'domain_filter': 'MX + Known Retailers'
            }

            return self._send_success(analyzed_data)

        except json.JSONDecodeError:
            return self._send_error(400, 'JSON invalido')
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
