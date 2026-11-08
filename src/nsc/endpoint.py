"""Bounded loopback benchmark endpoint, reached through the experimental engine."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import time


def serve(port, proof, size, lifetime, listen='127.0.0.1', allow_client=None):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()
        def log_message(self, *args):
            pass
        def do_GET(self):
            data = proof.encode() if self.path == '/proof' else b'\x00' * size
            if self.path not in ('/proof', '/download'):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            if self.path != '/upload' or length != size:
                self.send_error(400)
                return
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()
    class Server(HTTPServer):
        def verify_request(self, request, address):
            return address[0] in ('127.0.0.1', allow_client)
    server = Server((listen, port), Handler)
    server.timeout = 1
    deadline = time.monotonic() + lifetime
    try:
        while time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
