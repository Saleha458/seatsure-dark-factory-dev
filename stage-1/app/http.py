"""HTTP boundary. Network reads and writes never hold the state lock."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import parse_qsl, unquote, urlsplit

from .store import Store
from .validation import APIError, finite_json, require


class Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True

    def __init__(self, address):
        self.store = Store()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Do not log tokens, request bodies, or exported credentials.
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {"error": {"code": "malformed_request", "message": "Invalid HTTP request"}})

    def respond(self, status, body):
        encoded = b"" if status == 204 else json.dumps(body, ensure_ascii=True, allow_nan=False,
                                                      separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if encoded:
            self.wfile.write(encoded)

    def run_request(self):
        try:
            target = urlsplit(self.path)
            body = {}
            if self.command in ("POST", "PATCH", "PUT"):
                try:
                    require("Transfer-Encoding" not in self.headers, 400, "malformed_request")
                    length = int(self.headers.get("Content-Length", "0"))
                    require(length >= 0, 400, "malformed_request")
                    raw = self.rfile.read(length)
                    # Cancel has no required request body. Other writes require an object.
                    optional = self.command == "POST" and target.path.endswith("/cancel")
                    body = {} if optional and not raw else json.loads(raw.decode("utf-8"))
                    require(type(body) is dict and finite_json(body), 400, "malformed_request")
                except (ValueError, UnicodeError, RecursionError):
                    raise APIError(400, "malformed_request") from None
            status, result = self.server.store.dispatch(self.command, unquote(target.path),
                                                        dict(parse_qsl(target.query, keep_blank_values=True)),
                                                        body, self.headers)
        except APIError as exc:
            status, result = exc.status, {"error": {"code": exc.code, "message": exc.code.replace("_", " ")}}
        self.respond(status, result)

    do_GET = run_request
    do_POST = run_request
    do_PATCH = run_request
    do_DELETE = run_request
    do_PUT = run_request
