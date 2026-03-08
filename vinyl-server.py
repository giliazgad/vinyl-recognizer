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
import sys
import urllib.request
import urllib.error
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8765))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vinyl-recognizer.html")


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"  {self.address_string()} - {format % args}")

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
                self.send_error(404, f"vinyl-recognizer.html not found next to server")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/recognize":
            self._handle_recognize()
        elif self.path == "/api/discogs-price":
            self._handle_discogs_price()
        else:
            self.send_error(404)

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
        query = urllib.parse.quote(f"{artist} {title}".strip())
        ua = "VinylRecognizer/1.0"

        # 1. Search Discogs for the release
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

        self._json_response({
            "found": True,
            "release_id": release_id,
            "release_title": release.get("title", ""),
            "year": release.get("year", ""),
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
