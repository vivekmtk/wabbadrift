"""Local server for the Wabbadrift UI.

    python -m wabbadrift.server            # opens http://localhost:8765
    python -m wabbadrift.server --port 8123 --no-browser

Listens on 127.0.0.1 only and serves the files in ``web/`` plus a JSON API:

    GET  /api/pairs                          configured pairs (data/pairs/*.json)
    GET  /api/pairconfig?pair=<p>            a pair's setup, for editing
    POST /api/inspect                        {mo2_dir} or {wabbajack_file}, for the setup form
    POST /api/pairs/save                     {create, pair}  checks, writes data/pairs/<name>.json
    POST /api/pairs/delete                   {pair}  removes the pair config only
    GET  /api/scan?pair=<p>                  the last scan, scanning on first use
    GET  /api/mod?pair=<p>&name=<mod>        one mod, every file hashed
    GET  /api/filediff?pair=<p>&name=<mod>&path=<file>   key or line diff of a text file
    GET  /api/fomod?pair=<p>&name=<mod>      FOMOD choices of one mod
    GET  /api/fomods?pair=<p>                every FOMOD-installed baseline mod
    GET  /api/snapshots?pair=<p>             baseline snapshots, oldest first
    GET  /api/drift?pair=<p>&old=<id>&new=<id>  update drift between two snapshots
    POST /api/rescan                         {pair}
    POST /api/snapshot                       {pair, path?}  snapshots the pair's .wabbajack or path
    GET  /api/runs?pair=<p>                  the undo log, newest first
    POST /api/plan                           {pair, copy, replace}  dry-run preview of the actions
    POST /api/apply                          {pair, copy, replace, fingerprint}  runs a preview
    POST /api/undo                           {pair, run}

Only /api/apply and /api/undo write to an instance: the custom one (see actions.py). Everything
else writes only data/ (pair configs, hash cache, snapshots).
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .actions import ActionError
from . import config
from .config import PAIRS_DIR, PROJECT_ROOT, load_pair
from .report import PairState

WEB_DIR = PROJECT_ROOT / "web"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/web/app.css": ("app.css", "text/css; charset=utf-8"),
    "/web/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/web/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
}
MAX_BODY = 100_000

_states: dict[str, PairState] = {}
_states_lock = threading.Lock()


def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """KEY=value lines from .env, without overriding the real environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


def pair_names() -> list[str]:
    if not PAIRS_DIR.is_dir():
        return []
    return sorted(p.stem for p in PAIRS_DIR.glob("*.json"))


def known(name: str) -> str:
    """Only configured pair names are accepted (the name never becomes a path otherwise)."""
    if name not in pair_names():
        raise LookupError(f"unknown pair: {name}")
    return name


def forget(name: str) -> None:
    """Drop a pair's state after its config changed; the next request loads it again."""
    with _states_lock:
        _states.pop(name, None)


def pair_state(name: str) -> PairState:
    known(name)
    with _states_lock:
        if name not in _states:
            _states[name] = PairState(load_pair(name, PAIRS_DIR))
        return _states[name]


class Handler(BaseHTTPRequestHandler):
    server_version = "Wabbadrift"

    def log_message(self, fmt, *args):
        if "/api/" in str(args[0] if args else "") or fmt.startswith("code"):
            super().log_message(fmt, *args)

    def host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("localhost", "127.0.0.1")  # blocks DNS rebinding

    def send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send(code, body, "application/json; charset=utf-8")

    def do_GET(self):
        if not self.host_ok():
            return self.send_error(403)
        url = urlparse(self.path)
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            f = WEB_DIR / name
            return (
                self.send(200, f.read_bytes(), ctype)
                if f.is_file()
                else self.send_error(404)
            )
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        routes = {
            "/api/pairs": lambda: {"pairs": pair_names()},
            "/api/pairconfig": lambda: config.pair_form(
                known(q.get("pair", "")), PAIRS_DIR
            ),
            "/api/scan": lambda: self.locked(q, lambda s: s.ensure_scan()),
            "/api/mod": lambda: self.locked(q, lambda s: s.mod_files(q["name"])),
            "/api/filediff": lambda: self.locked(
                q, lambda s: s.file_diff(q["name"], q["path"])
            ),
            "/api/fomod": lambda: self.locked(q, lambda s: s.fomod(q["name"])),
            "/api/fomods": lambda: self.locked(q, lambda s: {"mods": s.all_fomods()}),
            "/api/snapshots": lambda: self.locked(
                q,
                lambda s: {
                    "snapshots": s.snapshots(),
                    "wabbajack_file": str(s.pair.wabbajack_file),
                },
            ),
            "/api/runs": lambda: self.locked(q, lambda s: {"runs": s.runs()}),
            "/api/drift": lambda: self.locked(
                q, lambda s: s.update_drift(q["old"], q["new"])
            ),
        }
        return self.answer(routes.get(url.path))

    def do_POST(self):
        if not self.host_ok():
            return self.send_error(403)
        body = self.read_body()
        if body is None:
            return self.send_json(400, {"error": "Bad request."})
        routes = {
            "/api/rescan": lambda: self.locked(body, lambda s: s.run_scan()),
            "/api/inspect": lambda: (
                config.inspect_wabbajack(body["wabbajack_file"])
                if "wabbajack_file" in body
                else config.inspect_instance(body.get("mo2_dir"))
            ),
            "/api/pairs/save": lambda: self.save_pair(body),
            "/api/pairs/delete": lambda: self.delete_pair(body),
            "/api/snapshot": lambda: self.locked(
                body, lambda s: s.take_snapshot(self.wabbajack_path(body.get("path")))
            ),
            "/api/plan": lambda: self.locked(
                body, lambda s: s.plan_actions(*self.selection(body))
            ),
            "/api/apply": lambda: self.locked(
                body,
                lambda s: s.apply_actions(
                    *self.selection(body), str(body.get("fingerprint"))
                ),
            ),
            "/api/undo": lambda: self.locked(
                body, lambda s: s.undo(str(body.get("run")))
            ),
        }
        return self.answer(routes.get(urlparse(self.path).path))

    @staticmethod
    def save_pair(body: dict) -> dict:
        form = body.get("pair")
        if not isinstance(form, dict):
            raise ValueError("'pair' must be an object.")
        saved = config.save_pair(form, bool(body.get("create")), PAIRS_DIR)
        forget(saved["name"])
        return saved

    @staticmethod
    def delete_pair(body: dict) -> dict:
        name = known(str(body.get("pair", "")))
        config.delete_pair(name, PAIRS_DIR)
        forget(name)
        return {"pairs": pair_names()}

    @staticmethod
    def selection(body: dict) -> tuple[list[str], list[str]]:
        out = []
        for key in ("copy", "replace"):
            names = body.get(key) or []
            if not isinstance(names, list) or not all(
                isinstance(n, str) for n in names
            ):
                raise ValueError(f"'{key}' must be a list of mod names.")
            out.append(names)
        return out[0], out[1]

    def read_body(self) -> dict | None:
        # A custom header + JSON content type force a CORS preflight, which is never answered,
        # so other websites can't drive these endpoints from the user's browser.
        if self.headers.get("X-Wabbadrift") != "1" or not (
            self.headers.get("Content-Type") or ""
        ).startswith("application/json"):
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    @staticmethod
    def wabbajack_path(value) -> Path | None:
        if not value:
            return None
        p = Path(str(value).strip().strip('"'))
        if p.suffix.lower() != ".wabbajack" or not p.is_file():
            raise ValueError("Not a .wabbajack file.")
        return p

    @staticmethod
    def locked(params: dict, work):
        state = pair_state(str(params.get("pair", "")))
        with state.lock:
            return work(state)

    def answer(self, route) -> None:
        if route is None:
            return self.send_error(404)
        try:
            return self.send_json(200, route())
        except ActionError as e:
            return self.send_json(409, {"error": str(e), "files": e.files})
        except (LookupError, ValueError) as e:
            return self.send_json(400, {"error": str(e).strip("'\"")})
        except OSError as e:
            return self.send_json(500, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 - report, keep serving
            traceback.print_exc()
            return self.send_json(500, {"error": f"{type(e).__name__}: {e}"})


class Server(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True


def make_server(port: int) -> ThreadingHTTPServer:
    return Server(("127.0.0.1", port), Handler)


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Serve Wabbadrift locally.")
    ap.add_argument(
        "--port", type=int, default=int(os.environ.get("WABBADRIFT_PORT", 8765))
    )
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    server = make_server(args.port)
    url = f"http://localhost:{args.port}/"
    print(f"Wabbadrift running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
