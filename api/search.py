from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.parse
import os
import re

SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '')

def _clean_upc(s: str) -> str:
    # deja solo dígitos, quita espacios/guiones
    return re.sub(r'\D+', '', s or '')

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

            # Nuevo: query libre opcional
            query_raw = (data.get('query') or '').strip()

            upc_raw = data.get('upc', '')
            upc = _clean_upc(upc_raw)

            num = int(data.get('num', 20))
            hl = data.get('hl', 'es')
            gl = data.get('gl', 'mx')
            domain = data.get('domain', 'google.com.mx')

            if not SERPAPI_KEY:
                return self._send_error(500, 'API Key no configurada')

            # Si viene query, úsala; si no, exige UPC
            if query_raw:
                q = query_raw
            else:
                if not upc:
                    return self._send_error(400, 'UPC es requerido (o usa "query")')
                q = upc

            params = {
                'engine': 'google',
                'q': q,
                'api_key': SERPAPI_KEY,
                'hl': hl,
                'gl': gl,
                'google_domain': domain,
                'num': str(max(1, min(num, 20)))  # límite práctico
            }

            url = f'https://serpapi.com/search.json?{urllib.parse.urlencode(params)}'
            req = urllib.request.Request(url, headers={'User-Agent': 'UPC-Price-Finder/1.0'})
            with urllib.request.urlopen(req, timeout=12) as resp:
                result = json.loads(resp.read().decode('utf-8', errors='ignore'))

            return self._send_success(result)

        except json.JSONDecodeError:
            return self._send_error(400, 'JSON invalido')
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8', errors='ignore')
            return self._send_error(e.code, f'Error de SerpAPI: {error_body[:200]}')
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
