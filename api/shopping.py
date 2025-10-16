from http.server import BaseHTTPRequestHandler
import json, os, re, urllib.request, urllib.parse

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

def _clean(s): return (s or "").strip()

class handler(BaseHTTPRequestHandler):
    def _ok(self, data):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _err(self, code, msg):
        self.send_response(code)
        self.send_header("Content-type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            if not SERPAPI_KEY:
                return self._err(500, "API Key no configurada")

            ln = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(ln)
            data = json.loads(body.decode("utf-8"))

            # Puedes mandar 'query' (nombre comercial) o 'upc'
            query = _clean(data.get("query") or "")
            upc = _clean(data.get("upc") or "")

            hl = _clean(data.get("hl") or "es")
            gl = _clean(data.get("gl") or "mx")
            domain = _clean(data.get("domain") or "google.com.mx")

            q = query or upc
            if not q:
                return self._err(400, "Se requiere 'query' o 'upc'")

            # 1) Buscar en Google Shopping para obtener product_id
            params1 = {
                "engine": "google_shopping",
                "q": q,
                "api_key": SERPAPI_KEY,
                "hl": hl, "gl": gl, "google_domain": domain,
                "num": "10"
            }
            url1 = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params1)
            req1 = urllib.request.Request(url1, headers={"User-Agent": "UPC-Price-Finder/1.0"})
            with urllib.request.urlopen(req1, timeout=12) as resp1:
                data1 = json.loads(resp1.read().decode("utf-8", errors="ignore"))

            # intenta tomar un product_id de shopping_results / product_results
            product_id = None
            for it in (data1.get("shopping_results") or []):
                if it.get("product_id"):
                    product_id = it["product_id"]; break
            if not product_id:
                for it in (data1.get("product_results") or []):
                    if it.get("product_id"):
                        product_id = it["product_id"]; break

            # Si no hubo product_id, al menos devuelve shopping_results “plan B”
            if not product_id:
                return self._ok({
                    "type": "shopping_results",
                    "items": data1.get("shopping_results") or []
                })

            # 2) Con product_id, pedir el panel de producto (sellers_results)
            params2 = {
                "engine": "google_product",
                "product_id": product_id,
                "api_key": SERPAPI_KEY,
                "hl": hl, "gl": gl, "google_domain": domain
            }
            url2 = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params2)
            req2 = urllib.request.Request(url2, headers={"User-Agent": "UPC-Price-Finder/1.0"})
            with urllib.request.urlopen(req2, timeout=12) as resp2:
                data2 = json.loads(resp2.read().decode("utf-8", errors="ignore"))

            # Estructura final
            out = {
                "type": "product_offers",
                "product": {
                    "title": (data2.get("title") or data1.get("search_metadata", {}).get("id") or ""),
                    "product_id": product_id,
                    "source": "google_product"
                },
                "offers": data2.get("sellers_results") or []
            }
            return self._ok(out)

        except json.JSONDecodeError:
            return self._err(400, "JSON inválido")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            return self._err(e.code, f"SerpAPI HTTP {e.code}: {body[:200]}")
        except Exception as e:
            return self._err(500, f"Error: {str(e)}")
