from http.server import BaseHTTPRequestHandler
import json
import os
import re
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

# ===================== Configuración =====================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

PRICE_MIN = 1
PRICE_MAX = 200000

# ===================== Helpers =====================
def _clean_upc(s: str) -> str:
    return re.sub(r"\D+", "", s or "")

def _extract_price_from_text(text: str):
    if not text: return None
    # Patrones para detectar precios ($1,200.00 o 1200.00)
    patterns = [
        r"\$\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)",
        r"(?:mxn|\$)\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)",
        r"(?:precio|price)[:\s]*\$?\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m and m.group(1):
            raw = m.group(1).strip().replace(".", "").replace(",", ".") # Normalizar a 1200.50
            try:
                value = float(raw)
                if PRICE_MIN <= value <= PRICE_MAX:
                    return value
            except: continue
    return None

def _root_domain(host: str) -> str:
    if not host: return ""
    host = host.lower().replace("www.", "")
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) == "com.mx":
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])

def _scrape_google_search_robust(query: str, num: int = 10, hl: str = "es", gl: str = "mx") -> list:
    """
    Versión 'Todo Terreno': No busca clases CSS (div.g). 
    Busca etiquetas H3 (títulos) y sus enlaces cercanos.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": f"{hl}-{gl},{hl};q=0.9",
        }
        
        # Pedimos un poco más de resultados para filtrar basura
        params = {"q": query, "num": num + 5, "hl": hl, "gl": gl}
        
        resp = requests.get("https://www.google.com/search", headers=headers, params=params, timeout=10)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        seen_links = set()

        # ESTRATEGIA: Buscar todos los H3 (títulos)
        all_h3 = soup.find_all("h3")
        
        for h3 in all_h3:
            if len(results) >= num: break
            
            title = h3.get_text(strip=True)
            if not title: continue

            # Buscar el <a> padre, hijo o vecino
            link_elem = h3.find_parent("a") or h3.find("a")
            if not link_elem:
                parent = h3.parent
                if parent: link_elem = parent.find("a", href=True)

            if not link_elem or not link_elem.get("href"): continue

            link = link_elem["href"]
            
            # Limpiar redirecciones de Google (/url?q=...)
            if link.startswith("/url?"):
                try:
                    from urllib.parse import parse_qs, urlparse
                    q = parse_qs(urlparse(link).query).get("q")
                    if q: link = q[0]
                except: pass

            # Filtros de calidad
            if "google." in link or not link.startswith("http"): continue
            if link in seen_links: continue
            
            seen_links.add(link)

            # Buscar snippet (texto descriptivo cercano)
            snippet = ""
            container = link_elem.find_parent("div")
            if container:
                snippet = container.get_text(" ", strip=True).replace(title, "").strip()

            price = _extract_price_from_text(snippet)
            
            # Obtener seller del dominio si no hay mejor info
            try:
                from urllib.parse import urlparse
                domain = urlparse(link).netloc
                seller = _root_domain(domain)
            except: seller = ""

            results.append({
                "title": title,
                "link": link,
                "snippet": snippet,
                "price": price,
                "currency": "MXN",
                "seller": seller,
            })

        return results

    except Exception as e:
        print(f"Error scraping: {e}")
        return []

def _summarize_with_gemini(query, results):
    # (Mantener lógica ligera para no gastar tokens innecesarios)
    if not results: return None, None, "local"
    
    prices = [r['price'] for r in results if r.get('price')]
    price_range = None
    if prices:
        price_range = {"min": min(prices), "max": max(prices), "currency": "MXN"}

    if not GEMINI_API_KEY:
        return f"Se encontraron {len(results)} resultados.", price_range, "regex"

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        context = f"Resumen breve (2 lineas) de estos productos para '{query}':\n"
        for r in results[:5]:
            context += f"- {r['title']} (${r['price']})\n"
        
        resp = model.generate_content(context)
        return resp.text.strip(), price_range, "gemini-2.0-flash"
    except:
        return f"Se encontraron {len(results)} resultados.", price_range, "regex"

# ===================== Handler =====================
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))

            query = data.get("query", "").strip()
            upc = _clean_upc(data.get("upc", ""))
            
            # Construir query inteligente
            final_query = query
            if upc and upc not in query:
                final_query = f"{query} {upc}".strip() if query else upc

            # USAR LA NUEVA FUNCIÓN ROBUSTA
            results = _scrape_google_search_robust(final_query, num=15)
            
            summary, prange, powered = _summarize_with_gemini(final_query, results)

            payload = {
                "organic_results": results,
                "gemini_summary": summary,
                "gemini_price_range": prange,
                "powered_by": powered
            }
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))