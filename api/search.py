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

# Límites solo para calcular rangos "razonables" de precio
PRICE_MIN = 1
PRICE_MAX = 200000


# ===================== Helpers =====================

def _clean_upc(s: str) -> str:
    """Deja solo dígitos en el UPC."""
    return re.sub(r"\D+", "", s or "")


def _extract_price_from_text(text: str):
    """Busca un precio numérico dentro de un texto (snippet, etc.)."""
    if not text:
        return None
    patterns = [
        r"\$\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)",
        r"(?:mxn|\$)\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)",
        r"(?:precio|price)[:\s]*\$?\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m and m.group(1):
            raw = m.group(1).strip()
            # Quitar separadores de miles y manejar coma/punto decimal
            raw = raw.replace(".", "").replace(",", ".")
            try:
                value = float(raw)
                if PRICE_MIN <= value <= PRICE_MAX:
                    return value
            except Exception:
                continue
    return None


def _root_domain(host: str) -> str:
    """Obtiene dominio raíz tipo 'walmart.com.mx' o 'amazon.com'."""
    if not host:
        return ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) == "com.mx":
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])

def _scrape_google_search(query: str, num: int = 10, hl: str = "es", gl: str = "mx") -> list:
    """
    Versión robusta: No depende de clases CSS específicas (como div.g).
    Busca patrones estructurales (H3 dentro de A, o A conteniendo H3).
    """
    try:
        # Headers rotativos básicos para evitar bloqueos inmediatos
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": f"{hl}-{gl},{hl};q=0.9",
        }

        params = {"q": query, "num": num + 5, "hl": hl, "gl": gl} # Pedimos un poco más por si acaso
        
        # URL limpia sin parámetros extraños
        resp = requests.get("https://www.google.com/search", headers=headers, params=params, timeout=12)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        seen_links = set()

        # ESTRATEGIA: Buscar cualquier H3 que sea título, y encontrar su link padre o vecino
        all_h3 = soup.find_all("h3")
        
        for h3 in all_h3:
            if len(results) >= num: break
            
            title = h3.get_text(strip=True)
            if not title: continue

            # Buscar el <a> contenedor o adyacente
            link_elem = h3.find_parent("a")
            if not link_elem:
                # A veces el <a> está dentro del h3 o justo al lado
                link_elem = h3.find("a")
            
            if not link_elem:
                # Intento final: buscar en el padre del h3
                parent = h3.parent
                if parent:
                    link_elem = parent.find("a", href=True)

            if not link_elem or not link_elem.get("href"):
                continue

            link = link_elem["href"]
            
            # Limpieza de links sucios de Google (/url?q=...)
            if link.startswith("/url?"):
                try:
                    from urllib.parse import parse_qs, urlparse
                    q = parse_qs(urlparse(link).query).get("q")
                    if q: link = q[0]
                except: pass

            # Descartar enlaces internos de Google
            if "google.com" in link or not link.startswith("http"):
                continue

            if link in seen_links: continue
            seen_links.add(link)

            # Buscar snippet: Generalmente es un div o span después del título
            # Buscamos el bloque de texto más cercano
            snippet = ""
            container = link_elem.find_parent("div")
            if container:
                # El texto del contenedor menos el título suele ser el snippet/metadata
                full_text = container.get_text(" ", strip=True)
                snippet = full_text.replace(title, "").strip()

            # Extraer precio del snippet
            price = _extract_price_from_text(snippet)
            
            # Extraer vendedor (Seller) del dominio
            seller = ""
            try:
                from urllib.parse import urlparse
                domain = urlparse(link).netloc
                seller = _root_domain(domain)
            except: pass

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
        print(f"Error scraping Google: {e}")
        return []

def _summarize_with_gemini(query: str, upc: str, results: list):
    """
    Usa Gemini SOLO para generar un texto de resumen.
    No filtra ni transforma los resultados.
    """
    summary = None
    price_range = None
    powered_by = None

    # Calcular rango de precios a partir de los resultados scrapeados
    prices = [r.get("price") for r in results if isinstance(r.get("price"), (int, float))]
    if prices:
        p_min = min(prices)
        p_max = max(prices)
        p_avg = sum(prices) / len(prices) if prices else None
        price_range = {
            "min": p_min,
            "max": p_max,
            "avg": p_avg,
            "currency": "MXN",
        }

    # Si no hay API key, devolver un resumen básico
    if not GEMINI_API_KEY:
        powered_by = "local"
        summary = f"Se encontraron {len(results)} resultados para \"{query}\"."
        if price_range:
            summary += f" Los precios detectados van de aproximadamente ${p_min:.2f} a ${p_max:.2f} MXN."
        return summary, price_range, powered_by

    try:
        powered_by = "gemini-2.0-flash"
        model = genai.GenerativeModel(
            "gemini-1.5-flash",
            generation_config={
                "temperature": 0.3,
                "top_p": 0.8,
                "top_k": 40,
                "max_output_tokens": 256,
            },
        )

        # Construimos un contexto ligero
        context = f"""Eres un asistente de análisis de precios.
Te doy resultados de búsqueda de un producto y quiero que generes un breve resumen en español (2-4 oraciones).
Incluye si es posible una idea general de rango de precios y qué tipo de tiendas aparecen.

Consulta:
- Query: "{query}"
- UPC (si aplica): "{upc or 'N/A'}"
- Número de resultados: {len(results)}

Algunos resultados:
"""
        for idx, r in enumerate(results[:8], 1):
            context += f"\n{idx}. {r.get('title','')}\n   {r.get('seller','')}  ·  {r.get('price','?')} MXN\n"

        prompt = context + "\nEscribe solo el resumen, sin bullets, sin formato markdown."

        resp = model.generate_content(prompt)
        txt = (resp.text or "").strip()
        if txt:
            summary = txt
        else:
            summary = f"Se encontraron {len(results)} resultados para \"{query}\"."

    except Exception as e:
        print(f"Error generando resumen con Gemini: {e}")
        powered_by = "local"
        summary = f"Se encontraron {len(results)} resultados para \"{query}\"."

    return summary, price_range, powered_by


# ===================== Handler HTTP (Vercel) =====================

class handler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                self._send_json(400, {"error": "Body vacío"})
                return

            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))

            query = (data.get("query") or "").strip()
            upc_raw = data.get("upc") or ""
            upc = _clean_upc(upc_raw)
            num = int(data.get("num") or 10)
            hl = data.get("hl") or "es"
            gl = data.get("gl") or "mx"

            if not query and not upc:
                self._send_json(400, {"error": "Se requiere 'query' o 'upc'"})
                return

            # Construir query final flexible
            final_query = query
            if upc and upc not in (query or ""):
                final_query = f"{query} {upc}".strip() if query else upc

            results = _scrape_google_search(final_query, num=num, hl=hl, gl=gl)

            gem_summary, gem_price_range, powered_by = _summarize_with_gemini(
                final_query, upc, results
            )

            payload = {
                "organic_results": results,
                "shopping_results": [],  # aquí no usamos Shopping
                "gemini_summary": gem_summary,
                "gemini_price_range": gem_price_range,
                "powered_by": powered_by,
            }

            self._send_json(200, payload)

        except json.JSONDecodeError:
            self._send_json(400, {"error": "JSON inválido"})
        except Exception as e:
            print(f"Error en handler /api/search: {e}")
            self._send_json(500, {"error": "Error interno del servidor"})
