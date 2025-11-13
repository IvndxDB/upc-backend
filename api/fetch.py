from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.parse
import urllib.error
import re
import html
import os
import google.generativeai as genai

# Configurar Gemini
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Límites de validación
PRICE_MIN = 10
PRICE_MAX = 100000

PRICE_PATTERNS = [
    r'"offers"\s*:\s*{[^}]*?"price"\s*:\s*"?([0-9.,]+)"?',
    r'"priceAmount"\s*:\s*"?([0-9.,]+)"?',
    r'"currentPrice"\s*:\s*"?([0-9.,]+)"?',
    r'"salePrice"\s*:\s*"?([0-9.,]+)"?',
    r'data-price\s*=\s*"?([0-9.,]+)"?',
    r'\$\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)',
    r'(?:precio|price)["\s:]*\$?\s*([0-9.,]+)'
]

def _normalize_price(s):
    """Normaliza precios a formato decimal"""
    if not s: return None
    s = s.strip()
    s = re.sub(r'\.(?=\d{3}\b)', '', s)  # quita separador de miles con punto
    s = s.replace(',', '.')               # coma decimal
    try:
        v = float(s)
        # NUEVO: Validar rango
        return v if PRICE_MIN <= v <= PRICE_MAX else None
    except: 
        return None

def _extract_with_regex(html_text: str) -> dict:
    """Extrae información usando regex (método original)"""
    result = {}
    
    # Title
    mtitle = re.search(r'<title[^>]*>(.*?)</title>', html_text, re.I|re.S)
    result['title'] = html.unescape(mtitle.group(1)).strip() if mtitle else None
    
    # Seller
    mseller = re.search(r'"seller"\s*:\s*"?([^",}{]+)"?', html_text, re.I)
    result['seller'] = mseller.group(1).strip() if mseller else None
    
    # Currency
    mcurr = re.search(r'"priceCurrency"\s*:\s*"?([A-Z]{3})"?', html_text, re.I)
    result['currency'] = (mcurr.group(1).strip() if mcurr else None) or 'MXN'
    
    # Price
    price = None
    for p in PRICE_PATTERNS:
        for m in re.finditer(p, html_text, re.I):
            price = _normalize_price(m.group(1))
            if price: break
        if price: break
    result['price'] = price
    
    return result

def _enhance_with_gemini(html_text: str, url: str, regex_result: dict) -> dict:
    """
    Usa Gemini para mejorar la extracción de información del producto
    """
    try:
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        
        # Truncar HTML si es muy largo
        html_preview = html_text[:8000] if len(html_text) > 8000 else html_text
        
        prompt = f"""Analiza el siguiente HTML de una página de producto y extrae información estructurada.

URL: {url}

Información ya extraída con regex:
{json.dumps(regex_result, indent=2, ensure_ascii=False)}

HTML (primeros 8000 caracteres):
{html_preview}

Tu tarea:
1. Si el precio en regex_result es null, intenta encontrar el precio en el HTML
2. Mejora el título del producto (hazlo más limpio y descriptivo)
3. Identifica el vendedor/tienda si no se encontró
4. Extrae información adicional útil: categoría, marca, disponibilidad, rating
5. Valida que el precio sea correcto

REGLAS CRÍTICAS PARA PRECIOS:
- El precio debe estar entre ${PRICE_MIN} MXN y ${PRICE_MAX} MXN
- Si regex ya encontró un precio válido, úsalo (no lo cambies)
- Solo busca precio en el HTML si regex_result.price es null
- NO uses precios por unidad (ej: "$2.00 por tableta")
- USA el precio de VENTA ACTUAL, no precios tachados o de promociones viejas
- Si el precio parece sospechoso (muy bajo o muy alto), déjalo como null

FORMATO:
{{
  "title": "Título limpio del producto",
  "price": 125.50,
  "currency": "MXN",
  "seller": "Nombre de la tienda",
  "brand": "Marca del producto",
  "category": "Categoría",
  "availability": "in_stock",
  "rating": 4.5,
  "review_count": 120,
  "description": "Descripción breve",
  "confidence": "high"
}}

IMPORTANTE:
- Responde SOLO con JSON válido, sin markdown
- confidence puede ser: "high", "medium", "low"
- Si no estás seguro de un campo, déjalo como null"""

        response = model.generate_content(prompt)
        result_text = response.text.strip()
        
        # Limpiar markdown
        result_text = result_text.replace('```json', '').replace('```', '').strip()
        
        gemini_result = json.loads(result_text)
        
        # VALIDACIÓN: Priorizar regex para precio si ya lo encontró
        final_price = regex_result.get('price')
        if final_price is None:
            final_price = gemini_result.get('price')
        
        # VALIDAR que el precio esté en rango
        if final_price is not None:
            try:
                final_price = float(final_price)
                if not (PRICE_MIN <= final_price <= PRICE_MAX):
                    final_price = None
            except:
                final_price = None
        
        # Combinar resultados
        final_result = {
            'title': gemini_result.get('title') or regex_result.get('title'),
            'price': final_price,
            'currency': gemini_result.get('currency') or regex_result.get('currency', 'MXN'),
            'seller': gemini_result.get('seller') or regex_result.get('seller'),
            'brand': gemini_result.get('brand'),
            'category': gemini_result.get('category'),
            'availability': gemini_result.get('availability'),
            'rating': gemini_result.get('rating'),
            'review_count': gemini_result.get('review_count'),
            'description': gemini_result.get('description'),
            'extraction_method': 'gemini_enhanced',
            'confidence': gemini_result.get('confidence', 'medium')
        }
        
        return final_result
        
    except Exception as e:
        print(f"Error con Gemini en fetch: {e}")
        # Fallback a resultados de regex
        regex_result['extraction_method'] = 'regex_only'
        regex_result['confidence'] = 'low' if not regex_result.get('price') else 'medium'
        return regex_result

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

            url = (data.get('url') or '').strip()
            use_gemini = data.get('use_gemini', True)
            
            if not url:
                return self._send_error(400, 'url requerida')

            # Fetch HTML
            req = urllib.request.Request(
                url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                    'Accept-Language': 'es-MX,es;q=0.9,en;q=0.8'
                }
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                html_bytes = resp.read()
                encoding = resp.headers.get_content_charset() or 'utf-8'
                html_text = html_bytes.decode(encoding, errors='ignore')

            # 1. Extracción con regex (método original)
            regex_result = _extract_with_regex(html_text)
            
            # 2. Si hay Gemini configurado y use_gemini=True, mejorar extracción
            if GEMINI_API_KEY and use_gemini:
                final_result = _enhance_with_gemini(html_text, url, regex_result)
            else:
                regex_result['extraction_method'] = 'regex_only'
                final_result = regex_result
            
            # Si no se encontró precio, informar
            if final_result.get('price') is None:
                final_result['message'] = 'No se pudo extraer el precio de esta página'
            
            # NUEVO: Agregar información de validación
            final_result['url'] = url
            final_result['powered_by'] = 'gemini-2.0-flash' if (GEMINI_API_KEY and use_gemini) else 'regex'
            final_result['validation'] = {
                'price_range_filter': f'{PRICE_MIN}-{PRICE_MAX} MXN',
                'price_valid': final_result.get('price') is not None
            }
            
            return self._send_success(final_result)

        except json.JSONDecodeError:
            return self._send_error(400, 'JSON invalido')
        except urllib.error.HTTPError as e:
            return self._send_error(e.code, f'Fetch HTTPError {e.code}')
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
