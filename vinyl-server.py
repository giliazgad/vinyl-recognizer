#!/usr/bin/env python3
"""
Vinyl Record Recognizer - Server
Serves the HTML frontend and proxies Anthropic API calls.

Local usage:
    ANTHROPIC_API_KEY=sk-ant-... python3 vinyl-server.py

The server reads the API key from the ANTHROPIC_API_KEY environment variable.
"""

import json
import os
import secrets
import urllib.request
import urllib.error
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8765))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
APPLE_CLIENT_ID = os.environ.get("APPLE_CLIENT_ID", "")
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vinyl-recognizer.html")

VALID_TOKENS: set = set()


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"  {self.address_string()} - {format % args}")

    def _authed(self):
        """Return True if auth is disabled or the request carries a valid token."""
        if not APP_PASSWORD:
            return True
        token = self.headers.get("X-Auth-Token", "")
        return token in VALID_TOKENS

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(HTML_FILE, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_error(404, "vinyl-recognizer.html not found next to server")
        elif self.path == "/api/auth-required":
            self._json_response({"required": bool(APP_PASSWORD or GOOGLE_CLIENT_ID or APPLE_CLIENT_ID)})
        elif self.path == "/api/config":
            self._json_response({
                "googleClientId": GOOGLE_CLIENT_ID,
                "appleClientId": APPLE_CLIENT_ID,
                "passwordEnabled": bool(APP_PASSWORD),
                "hasApiKey": bool(ANTHROPIC_API_KEY),
            })
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/login":
            self._handle_login()
        elif self.path == "/api/login-google":
            self._handle_login_google()
        elif self.path == "/api/login-apple":
            self._handle_login_apple()
        elif self.path == "/api/logout":
            self._handle_logout()
        elif self.path == "/api/recognize":
            if not self._authed():
                self._json_error(401, "Unauthorized")
                return
            self._handle_recognize()
        elif self.path == "/api/discogs-price":
            if not self._authed():
                self._json_error(401, "Unauthorized")
                return
            self._handle_discogs_price()
        else:
            self.send_error(404)

    def _handle_login(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._json_error(400, "Invalid JSON body")
            return
        if not APP_PASSWORD or payload.get("password") == APP_PASSWORD:
            token = secrets.token_hex(32)
            VALID_TOKENS.add(token)
            self._json_response({"ok": True, "token": token})
        else:
            self._json_error(401, "Incorrect password")

    def _handle_login_google(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._json_error(400, "Invalid JSON body"); return

        credential = payload.get("credential", "")
        if not credential:
            self._json_error(400, "Missing credential"); return

        # Verify with Google's tokeninfo endpoint
        verify_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={urllib.parse.quote(credential)}"
        try:
            req = urllib.request.Request(verify_url)
            with urllib.request.urlopen(req, timeout=8) as resp:
                info = json.loads(resp.read())
        except Exception as e:
            self._json_error(401, f"Google token verification failed: {e}"); return

        if GOOGLE_CLIENT_ID and info.get("aud") != GOOGLE_CLIENT_ID:
            self._json_error(401, "Token audience mismatch"); return

        token = secrets.token_hex(32)
        VALID_TOKENS.add(token)
        self._json_response({"ok": True, "token": token,
                             "name": info.get("name", ""), "email": info.get("email", "")})

    def _handle_login_apple(self):
        import base64, json as _json
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._json_error(400, "Invalid JSON body"); return

        id_token = payload.get("id_token", "")
        if not id_token:
            self._json_error(400, "Missing id_token"); return

        # Decode payload without signature verification (basic claims check)
        # Full RS256 verification requires a JWT library; add PyJWT to requirements.txt for production
        try:
            parts = id_token.split(".")
            padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(padded))
        except Exception as e:
            self._json_error(401, f"Could not decode Apple token: {e}"); return

        aud = claims.get("aud", "")
        if APPLE_CLIENT_ID and aud != APPLE_CLIENT_ID:
            self._json_error(401, "Token audience mismatch"); return

        token = secrets.token_hex(32)
        VALID_TOKENS.add(token)
        name = payload.get("name", claims.get("email", "Apple User"))
        self._json_response({"ok": True, "token": token, "name": name, "email": claims.get("email", "")})

    def _handle_logout(self):
        token = self.headers.get("X-Auth-Token", "")
        VALID_TOKENS.discard(token)
        self._json_response({"ok": True})

    def _handle_recognize(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._json_error(400, "Invalid JSON body")
            return

        # Prefer server-side key; fall back to key sent from browser (local use)
        api_key = ANTHROPIC_API_KEY or payload.pop("apiKey", None)
        if not api_key:
            self._json_error(400, "No API key configured. Set ANTHROPIC_API_KEY on the server or enter it in the UI.")
            return
        payload.pop("apiKey", None)  # strip it if present

        req_body = json.dumps(payload).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=req_body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as resp:
                resp_body = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp_body)

        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(err_body)

        except urllib.error.URLError as e:
            self._json_error(502, f"Could not reach Anthropic API: {e.reason}")

    def _handle_discogs_price(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._json_error(400, "Invalid JSON body")
            return

        artist = payload.get("artist", "")
        title = payload.get("title", "")
        barcode = payload.get("barcode", "")
        catno = payload.get("catno", "")
        ua = "VinylRecognizer/1.0"

        # 1. Search Discogs — prefer barcode > catno > text query
        if barcode:
            search_url = f"https://api.discogs.com/database/search?barcode={urllib.parse.quote(barcode)}&type=release&per_page=3"
        elif catno:
            search_url = f"https://api.discogs.com/database/search?catno={urllib.parse.quote(catno)}&type=release&per_page=3"
        else:
            query = urllib.parse.quote(f"{artist} {title}".strip())
            search_url = f"https://api.discogs.com/database/search?q={query}&type=release&per_page=3"
        try:
            req = urllib.request.Request(search_url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=8) as resp:
                search_data = json.loads(resp.read())
        except Exception as e:
            self._json_error(502, f"Discogs search failed: {e}")
            return

        results = search_data.get("results", [])
        if not results:
            self._json_response({"found": False, "message": "Not found on Discogs"})
            return

        release = results[0]
        release_id = release.get("id")
        release_uri = release.get("uri", "")
        discogs_url = f"https://www.discogs.com{release_uri}" if release_uri else \
                      f"https://www.discogs.com/search/?q={query}&type=release"

        # 2. Fetch marketplace stats (lowest price + count)
        stats_url = f"https://api.discogs.com/marketplace/stats/{release_id}?curr_abbr=USD"
        try:
            req2 = urllib.request.Request(stats_url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req2, timeout=8) as resp:
                stats = json.loads(resp.read())
        except Exception:
            stats = {}

        # 3. Fetch listings to compute average and highest price
        highest_price = None
        avg_price = None
        listings_url = (
            f"https://api.discogs.com/marketplace/search"
            f"?release_id={release_id}&status=For+Sale&per_page=100&sort=price&sort_order=asc"
        )
        try:
            req3 = urllib.request.Request(listings_url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req3, timeout=8) as resp:
                listings_data = json.loads(resp.read())
            prices = [
                l["price"]["value"]
                for l in listings_data.get("results", [])
                if isinstance(l.get("price"), dict) and l["price"].get("value") is not None
            ]
            if prices:
                currency = stats.get("lowest_price", {}).get("currency", "USD") if stats.get("lowest_price") else "USD"
                highest_price = {"value": max(prices), "currency": currency}
                avg_price = {"value": round(sum(prices) / len(prices), 2), "currency": currency}
        except Exception:
            pass  # listings endpoint may require auth; silently skip

        label_list = release.get("label", [])
        format_list = release.get("format", [])
        self._json_response({
            "found": True,
            "release_id": release_id,
            "release_title": release.get("title", ""),
            "year": release.get("year", ""),
            "country": release.get("country", ""),
            "label": label_list[0] if label_list else "",
            "catno": release.get("catno", ""),
            "format": ", ".join(format_list) if format_list else "",
            "lowest_price": stats.get("lowest_price"),
            "highest_price": highest_price,
            "avg_price": avg_price,
            "num_for_sale": stats.get("num_for_sale", 0),
            "discogs_url": discogs_url,
        })

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.end_headers()

    def _json_error(self, code, message):
        body = json.dumps({"error": {"message": message}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("WARNING: ANTHROPIC_API_KEY is not set. Set it before starting the server.")
        print("  Local:  ANTHROPIC_API_KEY=sk-ant-... python3 vinyl-server.py")
        print("  Render: set it in the Environment Variables dashboard\n")
    host = "0.0.0.0"
    server = HTTPServer((host, PORT), Handler)
    print(f"\n  Vinyl Recognizer running at http://localhost:{PORT}")
    print(f"  Serving: {HTML_FILE}")
    print(f"  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
