from http.server import BaseHTTPRequestHandler
import json
import os
import re
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import time

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
            raw = m.group(1).strip().replace(".", "").replace(",", ".") 
            try:
                value = float(raw)
                if PRICE_MIN <= value <= PRICE_MAX:
                    return value
            except: continue
    return None

def _root_domain(link: str) -> str:
    """Extrae el dominio limpio (ej: walmart, amazon)"""
    if not link: return ""
    try:
        from urllib.parse import urlparse
        host = urlparse(link).netloc.lower().replace("www.", "")
        parts = host.split(".")
        # Lógica para .com.mx o .mx
        if len(parts) >= 3 and (parts[-2] == "com" or parts[-2] == "org") and parts[-1] == "mx":
             return parts[-3]
        if len(parts) >= 2:
            return parts[-2]
        return host
    except:
        return ""

# ===================== Scrapers =====================

def _scrape_duckduckgo(query: str, num: int = 15) -> list:
    """
    PLAN B: Scraper de DuckDuckGo (versión HTML).
    Es mucho más permisivo con servidores como Vercel.
    Usa 'kl=mx-es' para forzar resultados de MÉXICO.
    """
    try:
        print(f"Intento con DuckDuckGo para: {query}")
        url = "https://html.duckduckgo.com/html/"
        data = {
            'q': query,
            'kl': 'mx-es',  # Región México
            'df': 'w'       # Filtro de fecha (opcional)
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://html.duckduckgo.com/'
        }
        
        # DDG usa POST para la versión HTML a veces
        resp = requests.post(url, data=data, headers=headers, timeout=10)
        
        # Si falla el POST, intentar GET
        if resp.status_code != 200:
             resp = requests.get(url, params=data, headers=headers, timeout=10)

        if resp.status_code != 200: return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        results = []
        
        # Selectores de DDG HTML
        for result in soup.select('.result'):
            if len(results) >= num: break
            
            a_tag = result.select_one('.result__a')
            if not a_tag: continue
            
            title = a_tag.get_text(strip=True)
            link = a_tag.get('href')
            
            # Limpiar redirecciones de DDG
            if link and "uddg=" in link:
                from urllib.parse import unquote
                try:
                    link = unquote(link.split("uddg=")[1].split("&")[0])
                except: pass

            if not link or not link.startswith('http'): continue
            
            # Snippet
            snippet_tag = result.select_one('.result__snippet')
            snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
            
            # Precio y Vendedor
            price = _extract_price_from_text(snippet)
            seller = _root_domain(link)
            
            results.append({
                "title": title,
                "link": link,
                "snippet": snippet,
                "price": price,
                "currency": "MXN",
                "seller": seller,
                "origin": "ddg_fallback"
            })
            
        return results
    except Exception as e:
        print(f"Error DDG: {e}")
        return []

def _scrape_google_robust(query: str, num: int = 10) -> list:
    """
    PLAN A: Scraper de Google con esteroides.
    Usa google.com.mx y cookies de consentimiento para intentar pasar.
    """
    try:
        # Usamos google.com.mx directo
        url = "https://www.google.com.mx/search"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "es-MX,es;q=0.9",
            # Cookie mágica para evitar el popup de 'Antes de ir a Google'
            "Cookie": "CONSENT=YES+Cb.20210720-07-p0.en+FX+410;" 
        }
        
        params = {
            "q": query,
            "num": num + 5,
            "hl": "es",
            "gl": "mx",
            "pws": "0" # Desactivar personalización
        }
        
        resp = requests.get(url, headers=headers, params=params, timeout=8)
        
        # Si nos bloquean (429) o redirigen al login, fallamos rápido para ir a DDG
        if resp.status_code != 200 or "google.com/sorry" in resp.url:
            print("Google bloqueó la petición o pidió captcha.")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Verificar si es una página de "Antes de continuar"
        if "consent" in soup.get_text().lower() and len(soup.find_all("a")) < 10:
             print("Detectada página de consentimiento Google.")
             return []

        results = []
        seen_links = set()
        
        # Búsqueda agnóstica de clases (busca H3 títulos)
        all_h3 = soup.find_all("h3")
        
        for h3 in all_h3:
            if len(results) >= num: break
            
            title = h3.get_text(strip=True)
            if not title: continue

            # Buscar link asociado
            link_elem = h3.find_parent("a") or h3.find("a")
            if not link_elem:
                parent = h3.parent
                if parent: link_elem = parent.find("a", href=True)

            if not link_elem or not link_elem.get("href"): continue
            link = link_elem["href"]
            
            # Limpieza url?q=
            if "/url?q=" in link:
                link = link.split("/url?q=")[1].split("&")[0]

            if "google." in link or not link.startswith("http"): continue
            if link in seen_links: continue
            seen_links.add(link)

            snippet = ""
            container = link_elem.find_parent("div")
            if container:
                snippet = container.get_text(" ", strip=True).replace(title, "").strip()

            price = _extract_price_from_text(snippet)
            seller = _root_domain(link)

            results.append({
                "title": title,
                "link": link,
                "snippet": snippet,
                "price": price,
                "currency": "MXN",
                "seller": seller,
                "origin": "google"
            })

        return results
    except Exception as e:
        print(f"Error Google: {e}")
        return []

def _summarize_with_gemini(query, results):
    if not results: return None, None, "local"
    
    prices = [r['price'] for r in results if r.get('price')]
    price_range = None
    if prices:
        price_range = {
            "min": min(prices),
            "max": max(prices),
            "avg": sum(prices)/len(prices),
            "currency": "MXN"
        }

    if not GEMINI_API_KEY:
        return f"Se encontraron {len(results)} resultados.", price_range, "regex"

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        # Contexto simple para no gastar tokens
        context = f"""Resumen en 1 oración (Español) para '{query}'.
        Resultados: {len(results)}. 
        Tiendas: {', '.join(list(set([r['seller'] for r in results[:5]])))}
        """
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
            
            final_query = query
            if upc and upc not in query:
                final_query = f"{query} {upc}".strip() if query else upc

            # 1. INTENTO GOOGLE (Plan A)
            results = _scrape_google_robust(final_query, num=12)
            
            # 2. INTENTO DUCKDUCKGO (Plan B - Si Google falla)
            # Si Google devuelve 0 o muy pocos resultados, vamos con DDG
            if len(results) < 2:
                print("Switching to DuckDuckGo fallback...")
                ddg_results = _scrape_duckduckgo(final_query, num=15)
                # Combinar evitando duplicados de link
                seen = set(r['link'] for r in results)
                for dr in ddg_results:
                    if dr['link'] not in seen:
                        results.append(dr)

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