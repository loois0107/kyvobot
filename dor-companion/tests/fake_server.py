"""
cogs/dor.py의 handle_upload_webhook과 같은 통신 규격(헤더/필드명/상태코드)으로
동작하는 최소 로컬 테스트 서버. 실제 서버 코드를 그대로 흉내 내서, 컴패니언 앱이
보내는 요청이 실제 서버가 기대하는 형식과 맞는지 end-to-end로 검증하기 위한 것.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

VALID_TOKEN = "test-token-abc123"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        server: FakeDorServer = self.server.fake  # type: ignore[attr-defined]
        server.received_requests.append(self.path)

        if server.force_down:
            # 연결 자체를 끊어서 네트워크 장애를 흉내낸다.
            self.connection.close()
            return

        if self.path != "/internal/dor/upload":
            self.send_response(404)
            self.end_headers()
            return

        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else auth
        if token != VALID_TOKEN:
            self._respond(401, {"error": "invalid token"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        fields = _parse_multipart(body, content_type)

        if "video" not in fields or "clip_events" not in fields or "basic_game_data" not in fields:
            self._respond(400, {"error": "missing field"})
            return

        try:
            json.loads(fields["clip_events"].decode("utf-8"))
            json.loads(fields["basic_game_data"].decode("utf-8"))
        except Exception:
            self._respond(400, {"error": "invalid json"})
            return

        server.uploaded_calls.append(
            {
                "video_bytes": fields["video"],
                "clip_events": json.loads(fields["clip_events"].decode("utf-8")),
                "basic_game_data": json.loads(fields["basic_game_data"].decode("utf-8")),
            }
        )
        server.candidate_counter += 1
        self._respond(200, {"status": "ok", "candidate_id": str(server.candidate_counter)})

    def _respond(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _parse_multipart(body: bytes, content_type: str) -> dict:
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"').encode("utf-8")
    if boundary is None:
        return {}
    delimiter = b"--" + boundary
    fields = {}
    for chunk in body.split(delimiter):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        header_blob, content = chunk.split(b"\r\n\r\n", 1)
        content = content.rstrip(b"\r\n")
        headers = header_blob.decode("utf-8", errors="replace")
        name = None
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                for piece in line.split(";"):
                    piece = piece.strip()
                    if piece.startswith("name="):
                        name = piece[len("name="):].strip('"')
        if name:
            fields[name] = content
    return fields


class FakeDorServer:
    def __init__(self):
        self.received_requests: list[str] = []
        self.uploaded_calls: list[dict] = []
        self.candidate_counter = 0
        self.force_down = False
        self._httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.fake = self  # type: ignore[attr-defined]
        self.port = self._httpd.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
