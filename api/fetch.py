from http.server import BaseHTTPRequestHandler
import json, urllib.request, urllib.parse, urllib.error, re, html

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
    # 1.234,56 -> 1234.56 | 1,234.56 -> 1234.56
    if not s: return None
    s = s.strip()
    s = re.sub(r'\.(?=\d{3}\b)', '', s)  # quita separador de miles con punto
    s = s.replace(',', '.')               # coma decimal
    try:
        v = float(s)
        return v if 1 <= v < 1_000_000 else None
    except: return None

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
            if not url:
                return self._send_error(400, 'url requerida')

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

            # title
            mtitle = re.search(r'<title[^>]*>(.*?)</title>', html_text, re.I|re.S)
            title = html.unescape(mtitle.group(1)).strip() if mtitle else None

            # seller
            mseller = re.search(r'"seller"\s*:\s*"?([^",}{]+)"?', html_text, re.I)
            seller = mseller.group(1).strip() if mseller else None

            # currency
            mcurr = re.search(r'"priceCurrency"\s*:\s*"?([A-Z]{3})"?', html_text, re.I)
            currency = (mcurr.group(1).strip() if mcurr else None) or 'MXN'

            price = None
            for p in PRICE_PATTERNS:
                for m in re.finditer(p, html_text, re.I):
                    price = _normalize_price(m.group(1))
                    if price: break
                if price: break

            if price is None:
                return self._send_success({'title': title, 'price': None})

            return self._send_success({
                'title': title, 'price': price,
                'currency': currency, 'seller': seller
            })

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
