#!/usr/bin/env python3
"""Minimal HTTP wrapper for mempalace-mcp — exposes stdio MCP server as POST /mcp."""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

_proc = None
_lock = threading.Lock()

# Response cache for tools whose payload is dominated by static content
# (protocol text, AAAK spec, full taxonomy). The dynamic parts (counts,
# wing/room lists) are tolerated as stale within the TTL — AI callers use
# these to learn palace structure, not to track second-by-second changes.
# Call mempalace_reconnect to flush.
_CACHEABLE_TOOLS = frozenset({"mempalace_status", "mempalace_get_taxonomy"})
_CACHE_TTL_SECONDS = 300  # 5 minutes
_cache_lock = threading.Lock()
_response_cache: dict = {}  # tool_name -> (timestamp, parsed_response_dict)


def _request_tool_name(parsed_body: dict) -> str | None:
    """Extract the tool name from a JSON-RPC tools/call request, or None."""
    if not isinstance(parsed_body, dict) or parsed_body.get("method") != "tools/call":
        return None
    params = parsed_body.get("params") or {}
    name = params.get("name")
    return name if isinstance(name, str) else None


def _serve_from_cache(tool_name: str, request_id) -> str | None:
    """Return a serialized response with the request's id, or None on miss/expiry."""
    with _cache_lock:
        entry = _response_cache.get(tool_name)
        if entry is None:
            return None
        ts, cached = entry
        if time.time() - ts > _CACHE_TTL_SECONDS:
            _response_cache.pop(tool_name, None)
            return None
        # Shallow copy + id swap so concurrent callers don't see each other's ids
        response = dict(cached)
        response["id"] = request_id
    return json.dumps(response)


def _store_in_cache(tool_name: str, response_str: str) -> None:
    """Parse and cache a response. Silently drops malformed/error responses."""
    try:
        parsed = json.loads(response_str)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(parsed, dict) or "error" in parsed:
        return
    with _cache_lock:
        _response_cache[tool_name] = (time.time(), parsed)


def _flush_cache() -> None:
    with _cache_lock:
        _response_cache.clear()


def get_proc(palace: str) -> subprocess.Popen:
    global _proc
    if _proc is None or _proc.poll() is not None:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        _proc = subprocess.Popen(
            ["mempalace-mcp", "--palace", palace],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
            env=env,
        )
    return _proc


def prewarm(palace: str) -> None:
    """Spawn the subprocess and block until the search path is fully warm.

    Three-step handshake:
    1. initialize (request/response) — loads ChromaDB client + ONNX model via
       mcp_server._client_cache.
    2. notifications/initialized (notification, no response) — completes the
       MCP session so the server will accept tool calls.
    3. mempalace_search (request/response) — forces palace._DEFAULT_BACKEND to
       open its own PersistentClient and load the HNSW index. Without this,
       the first real search triggers a cold client build (5-8 s) because
       _DEFAULT_BACKEND uses a separate cache from mcp_server._client_cache.
    """
    print("[mcp-http] Pre-warming mempalace-mcp...", file=sys.stderr)
    proc = get_proc(palace)

    # 1. initialize handshake
    proc.stdin.write(json.dumps({
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "mcp-http-prewarm", "version": "0"},
        },
    }) + "\n")
    proc.stdin.flush()
    proc.stdout.readline()  # block for initialize response

    # 2. notifications/initialized — server does not respond, no readline needed
    proc.stdin.write(json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }) + "\n")
    proc.stdin.flush()

    # 3. no-op search — warms palace._DEFAULT_BACKEND client + ONNX embed path
    proc.stdin.write(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": {"query": "prewarm"}},
    }) + "\n")
    proc.stdin.flush()
    proc.stdout.readline()  # block until search completes (result discarded)

    print("[mcp-http] mempalace-mcp ready", file=sys.stderr)


class MCPHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()

        parsed_body = None
        try:
            parsed_body = json.loads(body)
            is_notification = "id" not in parsed_body
        except json.JSONDecodeError:
            is_notification = False

        tool_name = _request_tool_name(parsed_body) if parsed_body else None

        # Cache hit path bypasses the subprocess lock entirely.
        response = None
        if tool_name in _CACHEABLE_TOOLS and not is_notification:
            response = _serve_from_cache(tool_name, parsed_body.get("id"))

        if response is None:
            with _lock:
                proc = get_proc(self.server.palace)
                proc.stdin.write(body + "\n")
                proc.stdin.flush()
                response = None if is_notification else proc.stdout.readline().strip()
            if tool_name == "mempalace_reconnect":
                _flush_cache()
            elif tool_name in _CACHEABLE_TOOLS and response:
                _store_in_cache(tool_name, response)

        if is_notification:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        else:
            encoded = response.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)

    def do_GET(self):
        # Health check
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[mcp-http] {fmt % args}", file=sys.stderr)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    palace: str


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--palace", required=True)
    parser.add_argument("--port", type=int, default=3001)
    args = parser.parse_args()

    server = ThreadedHTTPServer(("0.0.0.0", args.port), MCPHandler)
    server.palace = args.palace

    prewarm(args.palace)

    print(f"[mcp-http] Listening on port {args.port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
