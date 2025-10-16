# api/shopping.py
from http.server import BaseHTTPRequestHandler
import json, urllib.request, urllib.parse, os

SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '')

def _get(url, timeout=12):
    req = urllib.request.Request(url, headers={'User-Agent': 'UPC-Price-Finder/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8', errors='ignore'))

def _ok(handler, data):
    handler.send_response(200)
    handler.send_header('Content-type', 'application/json')
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.end_headers()
    handler.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

def _err(handler, code, msg):
    handler.send_response(code)
    handler.send_header('Content-type', 'application/json')
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.end_headers()
    handler.wfile.write(json.dumps({'error': msg}, ensure_ascii=False).encode('utf-8'))

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        if not SERPAPI_KEY:
            return _err(self, 500, 'API Key no configurada')
        try:
            body_len = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(body_len).decode('utf-8'))
            # query puede ser nombre de producto o UPC
            query = (payload.get('query') or '').strip()
            hl = payload.get('hl', 'es')
            gl = payload.get('gl', 'mx')
            domain = payload.get('domain', 'google.com.mx')
            if not query:
                return _err(self, 400, 'query es requerido')

            # 1) Buscar en "Google Shopping" (tbm=shop) para obtener product_id / sellers rápidos
            params_shop = urllib.parse.urlencode({
                'engine': 'google',
                'q': query,
                'api_key': SERPAPI_KEY,
                'hl': hl, 'gl': gl, 'google_domain': domain,
                'tbm': 'shop',  # shopping vertical
                'num': '20'
            })
            shop_url = f'https://serpapi.com/search.json?{params_shop}'
            shop = _get(shop_url)

            offers = []

            # 1a) Si algunos resultados ya traen precio + tienda, úsalo
            for it in (shop.get('shopping_results') or []):
                title = it.get('title') or ''
                link  = it.get('link') or ''
                store = it.get('source') or ''  # nombre de la tienda
                price = it.get('extracted_price') or it.get('price')  # SerpAPI ya lo extrae a veces
                currency = it.get('currency') or 'MXN'
                if link and (price or store):
                    offers.append({
                        'title': title, 'seller': store, 'price': price,
                        'currency': currency, 'link': link, 'origin': 'shopping_results'
                    })

            # 1b) Si hay product_id, usa "google_product" para listar sellers
            product_ids = [it.get('product_id') for it in (shop.get('shopping_results') or []) if it.get('product_id')]
            seen_links = set(o['link'] for o in offers if o.get('link'))
            for pid in product_ids[:2]:  # limita para costos
                params_prod = urllib.parse.urlencode({
                    'engine': 'google_product',
                    'api_key': SERPAPI_KEY,
                    'product_id': pid,
                    'hl': hl, 'gl': gl, 'google_domain': domain
                })
                prod_url = f'https://serpapi.com/search.json?{params_prod}'
                prod = _get(prod_url)

                # sellers_results → lista de tiendas con precio
                for s in (prod.get('sellers_results') or []):
                    store = s.get('name') or s.get('seller') or ''
                    price = s.get('extracted_price') or s.get('price')
                    currency = s.get('currency') or 'MXN'
                    link = s.get('link') or s.get('product_link') or ''
                    title = (prod.get('product_results') or {}).get('title') or (s.get('title') or '')
                    if link and link not in seen_links:
                        offers.append({
                            'title': title, 'seller': store, 'price': price,
                            'currency': currency, 'link': link, 'origin': 'google_product'
                        })
                        seen_links.add(link)

            # Normaliza: filtra precios no válidos, limita tamaño
            norm = []
            for o in offers:
                p = o.get('price')
                try:
                    if isinstance(p, str):  # "MXN$172.00"
                        import re
                        m = re.search(r'(\d+(?:[.,]\d{2})?)', p)
                        p = float(m.group(1).replace(',', '.')) if m else None
                    p = float(p) if p is not None else None
                except:
                    p = None
                norm.append({
                    'title': o.get('title') or '',
                    'seller': o.get('seller') or '',
                    'price': p,
                    'currency': o.get('currency') or 'MXN',
                    'link': o.get('link') or '',
                    'origin': o.get('origin')
                })

            return _ok(self, { 'query': query, 'offers': norm[:40] })

        except Exception as e:
            return _err(self, 500, f'Error: {e}')
