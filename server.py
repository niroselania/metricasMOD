"""Servidor del tablero de ventas: sirve el sitio y guarda la última planilla subida.

Todo pide sesión iniciada salvo /login, /api/login y /api/health.

GET  /login           -> pantalla de ingreso
POST /api/login       -> {"user","password","remember"} -> cookie de sesión
POST /api/logout      -> cierra la sesión
GET  /api/me          -> usuario actual y permisos
GET  /                -> tablero
GET  /api/data        -> datos procesados de la última planilla (404 si nunca se subió)
POST /api/upload      -> cuerpo = archivo .xlsx (solo usuarios admin); encabezado X-Filename
GET  /api/history     -> últimas cargas (fecha, archivo, meses)
GET  /api/health      -> estado (detalle completo solo con sesión)
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
import gzip
import hmac
import json
import os
import tempfile
import threading
import time

from ventas_processor import build_payload
from telegram_bot import start_bot
from auth import Auth

TELEGRAM_BOT = None

VERSION = "3.1.0"
APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
HISTORY_DIR = DATA_DIR / "history"
DATA_FILE = DATA_DIR / "ventas-data.json"
HISTORY_INDEX = DATA_DIR / "history-index.json"
HTML_FILE = APP_DIR / "index.html"
LOGIN_FILE = APP_DIR / "login.html"
AUTH: Auth | None = None
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
HISTORY_MAX = int(os.environ.get("HISTORY_MAX", "30"))
UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "").strip()
LOCK = threading.Lock()


def write_json_atomic(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = f"ventas-dashboard/{VERSION}"

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.address_string()} {fmt % args}", flush=True)

    def _ip(self) -> str:
        # Solo se confía en X-Forwarded-For detrás de un proxy propio (TRUST_PROXY=1)
        if os.environ.get("TRUST_PROXY", "").strip() in ("1", "true"):
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if fwd:
                return fwd
        return self.client_address[0]

    def _user(self):
        return AUTH.user_from_cookie(self.headers.get("Cookie")) if AUTH else None

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _security_headers(self):
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", cookie=None):
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "") and len(body) > 1024
        if gz:
            body = gzip.compress(body, 6)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj, cookie=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), cookie=cookie)

    def _drain(self, size):
        """Lee y descarta el cuerpo para que el navegador reciba la respuesta de error."""
        remaining = min(size, MAX_UPLOAD_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(1 << 20, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def do_GET(self):
        path = urlparse(self.path).path
        user = self._user()
        if path == "/login":
            if user:
                return self._redirect("/")
            return self._send(200, LOGIN_FILE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/health" and not user:
            return self._json(200, {"ok": True, "version": VERSION, "login": AUTH.enabled,
                                    "telegram": bool(TELEGRAM_BOT and TELEGRAM_BOT.status.get("connected"))})
        if not user:
            if path.startswith("/api/"):
                return self._json(401, {"error": "Sesión vencida. Volvé a ingresar."})
            return self._redirect("/login")
        if path in ("/", "/index.html"):
            return self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/me":
            info = AUTH.users[user]
            return self._json(200, {"user": user, "admin": info["admin"], "uploadToken": bool(UPLOAD_TOKEN)})
        if path == "/api/data":
            if not DATA_FILE.exists():
                return self._json(404, {"error": "Todavía no se subió ninguna planilla."})
            return self._send(200, DATA_FILE.read_bytes())
        if path == "/api/history":
            return self._json(200, read_json(HISTORY_INDEX, []))
        if path == "/api/health":
            telegram = dict(TELEGRAM_BOT.status) if TELEGRAM_BOT else {"enabled": False, "reason": "TELEGRAM_BOT_TOKEN vacío o no llegó al contenedor"}
            if TELEGRAM_BOT:
                telegram["notifyChats"] = len(TELEGRAM_BOT.notify_targets())
            return self._json(200, {"ok": True, "version": VERSION, "hasData": DATA_FILE.exists(),
                                    "uploadProtected": bool(UPLOAD_TOKEN), "telegram": telegram})
        self._json(404, {"error": "No encontrado"})

    def _read_json_body(self, limit=4096):
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0 or size > limit:
            self._drain(size)
            return None
        try:
            return json.loads(self.rfile.read(size).decode("utf-8"))
        except ValueError:
            return None

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            ip = self._ip()
            wait = AUTH.blocked_for(ip)
            body = self._read_json_body()
            if wait:
                return self._json(429, {"error": f"Demasiados intentos. Probá de nuevo en {wait // 60 + 1} min."})
            if not AUTH.enabled:
                return self._json(503, {"error": "No hay usuarios configurados. Cargá DASH_USERS en Portainer."})
            if not isinstance(body, dict):
                return self._json(400, {"error": "Solicitud inválida."})
            user = AUTH.check(str(body.get("user", "")), str(body.get("password", "")), ip)
            if not user:
                print(f"[login] intento fallido para '{str(body.get('user', ''))[:40]}' desde {ip}", flush=True)
                return self._json(401, {"error": "Usuario o contraseña incorrectos."})
            print(f"[login] {user} ingresó desde {ip}", flush=True)
            return self._json(200, {"ok": True, "user": user}, cookie=AUTH.make_cookie(user, bool(body.get("remember"))))
        if path == "/api/logout":
            self._drain(int(self.headers.get("Content-Length") or 0))
            return self._json(200, {"ok": True}, cookie=AUTH.clear_cookie())
        size = int(self.headers.get("Content-Length") or 0)
        user = self._user()
        if not user:
            self._drain(size)
            return self._json(401, {"error": "Sesión vencida. Volvé a ingresar."})
        if path != "/api/upload":
            self._drain(size)
            return self._json(404, {"error": "No encontrado"})
        if not AUTH.users[user]["admin"]:
            self._drain(size)
            return self._json(403, {"error": "Tu usuario no tiene permiso para subir planillas."})
        if UPLOAD_TOKEN and not hmac.compare_digest(self.headers.get("X-Upload-Token", ""), UPLOAD_TOKEN):
            self._drain(size)
            return self._json(401, {"error": "Código de carga incorrecto."})
        if size <= 0:
            return self._json(400, {"error": "El archivo está vacío."})
        if size > MAX_UPLOAD_BYTES:
            self.close_connection = True
            return self._json(413, {"error": "El archivo es demasiado grande."})
        filename = unquote(self.headers.get("X-Filename") or "planilla.xlsx")[:200]
        with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=DATA_DIR, delete=False) as tmp:
            remaining = size
            while remaining:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                tmp.write(chunk)
                remaining -= len(chunk)
            tmp_path = Path(tmp.name)
        try:
            t0 = time.time()
            with LOCK:
                nuevo = build_payload(tmp_path, filename)
                # Combina: los meses de la planilla nueva reemplazan a los guardados;
                # los meses que no vienen en esta planilla se conservan.
                previo = read_json(DATA_FILE, {}) or {}
                meses = dict(previo.get("meses", {}))
                for key, mes in previo.get("meses", {}).items():
                    mes.setdefault("archivo", previo.get("archivo", ""))
                    mes.setdefault("actualizado", previo.get("actualizado", ""))
                meses.update(nuevo["meses"])
                payload = {"archivo": nuevo["archivo"], "actualizado": nuevo["actualizado"],
                           "mesesActualizados": sorted(nuevo["meses"]), "meses": dict(sorted(meses.items()))}
                write_json_atomic(DATA_FILE, payload)
                hid = time.strftime("%Y%m%d-%H%M%S")
                write_json_atomic(HISTORY_DIR / f"{hid}.json", payload)
                index = [{"id": hid, "archivo": filename, "actualizado": payload["actualizado"],
                          "meses": payload["mesesActualizados"]}] + read_json(HISTORY_INDEX, [])
                for old in index[HISTORY_MAX:]:
                    (HISTORY_DIR / f"{old['id']}.json").unlink(missing_ok=True)
                write_json_atomic(HISTORY_INDEX, index[:HISTORY_MAX])
            print(f"[{user}] Planilla '{filename}' procesada en {time.time() - t0:.1f}s: actualizó {payload['mesesActualizados']}", flush=True)
            if TELEGRAM_BOT:
                TELEGRAM_BOT.notify_update(payload)
            self._json(200, payload)
        except Exception as exc:  # error de formato de planilla
            print(f"Error procesando '{filename}': {exc}", flush=True)
            self._json(422, {"error": f"No se pudo procesar la planilla: {exc}"})
        finally:
            tmp_path.unlink(missing_ok=True)


def main():
    global TELEGRAM_BOT, AUTH
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    AUTH = Auth(DATA_DIR)
    if AUTH.enabled:
        admins = [u for u, i in AUTH.users.items() if i["admin"]]
        print(f"Acceso con usuario: {len(AUTH.users)} usuario(s) · pueden subir planillas: {', '.join(admins)}", flush=True)
    else:
        print("ATENCIÓN: no hay usuarios (DASH_USERS vacío). Nadie puede ingresar hasta cargarlos en Portainer.", flush=True)
    TELEGRAM_BOT = start_bot(DATA_FILE, DATA_DIR)
    if TELEGRAM_BOT is None:
        print("Bot de Telegram DESACTIVADO: falta TELEGRAM_BOT_TOKEN (Portainer > Stack > Environment variables).", flush=True)
    port = int(os.environ.get("PORT", "8000"))
    print(f"Tablero de ventas v{VERSION} en el puerto {port} (carga protegida: {bool(UPLOAD_TOKEN)})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
