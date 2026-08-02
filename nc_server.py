from __future__ import annotations

import io
import json
import mimetypes
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import nc

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
MAX_BODY_BYTES = 512_000


def _safe_join(root: str, rel: str) -> str:
    rel = (rel or "").lstrip("/").replace("\\", "/")
    rel = os.path.normpath(rel)
    full = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if full != root_abs and not full.startswith(root_abs + os.sep):
        raise ValueError("blocked path traversal")
    return full


def _build_search_paths(project_root: str) -> list[str]:
    return [
        project_root,
        os.path.join(project_root, "libs"),
        os.path.join(project_root, "api"),
        os.path.join(project_root, "api", "libs"),
        os.path.join(project_root, "www"),
        os.path.join(project_root, "www", "libs"),
    ]


def _server_policy(project_root: str) -> nc.NCPolicy:
    return nc.NCPolicy(
        allow_http=False,
        allow_private_hosts=True,
        data_dir=os.path.join(project_root, "_data"),
    )


def _run_nc_text_capture(
    nc_text: str,
    project_root: str,
    source_name: str = "<text>",
    base: str | None = None,
) -> tuple[str, dict]:
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        nc.run_text(
            nc_text,
            base=base or project_root,
            extra_paths=_build_search_paths(project_root),
            policy=_server_policy(project_root),
            enable_ui=False,
            source_name=source_name,
        )
    return buf.getvalue(), {"source": source_name}


def _run_nc_file(path: str, request_obj: dict, project_root: str) -> tuple[int, dict[str, str], bytes]:
    injected = f"let request = {repr(request_obj)}\n"
    source_name = path
    base = project_root
    if path.lower().endswith(".nce"):
        code, base, source_name = nc.nce_read_entry_text(path, policy=_server_policy(project_root))
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()

    output, _meta = _run_nc_text_capture(
        injected + "\n" + code,
        project_root=project_root,
        source_name=source_name,
        base=base,
    )
    lines = output.splitlines()

    status = 200
    headers: dict[str, str] = {"Content-Type": "text/html; charset=utf-8"}
    body_lines = lines

    if lines and lines[0].startswith("__HTTP__"):
        meta_raw = lines[0][len("__HTTP__"):].strip()
        try:
            meta = json.loads(meta_raw)
            status = int(meta.get("status", 200))
            raw_headers = meta.get("headers", {})
            if isinstance(raw_headers, dict):
                for key, value in raw_headers.items():
                    headers[str(key)] = str(value)
            body_lines = lines[1:]
        except Exception:
            pass

    body = "\n".join(body_lines).encode("utf-8", errors="replace")
    return status, headers, body


class Handler(BaseHTTPRequestHandler):
    server_version = "NCServer/1.0"

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(str(key), str(value))
        self.end_headers()
        self.wfile.write(body)

    def _parse_request_body(self) -> tuple[bytes, str, dict | None, dict | None]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY_BYTES:
            raise ValueError(f"Request body too large ({length} bytes)")

        body = self.rfile.read(length) if length > 0 else b""
        body_text = body.decode("utf-8", errors="replace")
        content_type = (self.headers.get("Content-Type", "") or "").lower()

        req_json = None
        req_form = None

        if "application/json" in content_type:
            try:
                req_json = json.loads(body_text) if body_text.strip() else None
            except Exception:
                req_json = None

        if "application/x-www-form-urlencoded" in content_type:
            try:
                raw_form = urllib.parse.parse_qs(body_text, keep_blank_values=True)
                req_form = {k: (v[0] if len(v) == 1 else v) for k, v in raw_form.items()}
            except Exception:
                req_form = None

        return body, body_text, req_json, req_form

    def _handle(self) -> None:
        root = self.server.root_dir
        parsed = urllib.parse.urlparse(self.path)
        url_path = parsed.path or "/"

        try:
            _body, body_text, req_json, req_form = self._parse_request_body()
        except ValueError as e:
            self._send_bytes(413, str(e).encode("utf-8", errors="replace"))
            return

        if url_path.startswith("/api/"):
            rel_fs = url_path.lstrip("/")
        else:
            rel_fs = os.path.join("www", url_path.lstrip("/"))

        try:
            full = _safe_join(root, rel_fs)
        except Exception:
            self.send_error(403)
            return

        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        query = {k: (v[0] if len(v) == 1 else v) for k, v in qs.items()}
        request_obj = {
            "method": self.command,
            "path": url_path,
            "query": query,
            "headers": {k: v for k, v in self.headers.items()},
            "body": body_text,
            "json": req_json,
            "form": req_form,
        }

        if os.path.isdir(full):
            index_path = os.path.join(full, "index.html")
            if os.path.isfile(index_path):
                full = index_path
            else:
                self.send_error(404)
                return

        if full.lower().endswith((".nc", ".nce")) and os.path.isfile(full):
            try:
                status, headers, resp = _run_nc_file(full, request_obj, project_root=root)
                content_type = headers.pop("Content-Type", "text/html; charset=utf-8")
                self._send_bytes(status, resp, content_type, headers)
            except Exception as e:
                self._send_bytes(
                    500,
                    nc.format_exception(e).encode("utf-8", errors="replace"),
                )
            return

        if os.path.isfile(full):
            content_type, _ = mimetypes.guess_type(full)
            if (content_type or "").startswith("text/") and "charset=" not in (content_type or ""):
                content_type = (content_type or "text/plain") + "; charset=utf-8"
            content_type = content_type or "application/octet-stream"
            try:
                with open(full, "rb") as f:
                    data = f.read()
                self._send_bytes(200, data, content_type)
            except Exception:
                self.send_error(500)
            return

        self.send_error(404)


def main() -> None:
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
    host = DEFAULT_HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.root_dir = root

    print(f"NC server running: http://{host}:{port}/  (root={root})")
    print("Routes: / -> www/, /api/* -> api/*")
    print("Data dir: _data/ (json.save/load)")

    httpd.serve_forever()


if __name__ == "__main__":
    main()
