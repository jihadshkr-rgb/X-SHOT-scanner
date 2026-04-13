from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

_PORT = int(os.environ.get('PORT', 8080))

class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, *a): pass

Thread(target=lambda: HTTPServer(('0.0.0.0', _PORT), _H).serve_forever(), daemon=True).start()
