from http.server import BaseHTTPRequestHandler
import json
import urllib.request
import urllib.parse
import os

SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '')

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
            
            upc = data.get('upc', '')
            num = data.get('num', 20)
            hl = data.get('hl', 'es')
            gl = data.get('gl', 'mx')
            domain = data.get('domain', 'google.com.mx')
            
            if not upc:
                self._send_error(400, 'UPC es requerido')
                return
            
            if not upc.isdigit():
                self._send_error(400, 'UPC debe contener solo numeros')
                return
            
            if not SERPAPI_KEY:
                self._send_error(500, 'API Key no configurada')
                return
            
            params = {
                'engine': 'google',
                'q': upc,
                'api_key': SERPAPI_KEY,
                'hl': hl,
                'gl': gl,
                'google_domain': domain,
                'num': str(min(int(num), 20))
            }
            
            url = f'https://serpapi.com/search.json?{urllib.parse.urlencode(params)}'
            request = urllib.request.Request(url)
            request.add_header('User-Agent', 'UPC-Price-Finder/1.0')
            
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode('utf-8'))
            
            self._send_success(result)
            
        except json.JSONDecodeError:
            self._send_error(400, 'JSON invalido')
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8', errors='ignore')
            self._send_error(e.code, f'Error de SerpAPI: {error_body[:200]}')
        except Exception as e:
            self._send_error(500, f'Error: {str(e)}')
    
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
        error_response = {'error': message}
        self.wfile.write(json.dumps(error_response, ensure_ascii=False).encode('utf-8'))