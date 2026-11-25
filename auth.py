"""Acceso con usuario y contraseña para el tablero.

Los usuarios se definen en variables de entorno (Portainer > Stack > Environment variables):

  DASH_USERS     lista "usuario:clave" separada por comas o renglones.
                 Agregá ":admin" para que ese usuario pueda subir planillas.
                   ariel:MiClave2026:admin, comercial:OtraClave
                 Si nadie tiene ":admin", todos los usuarios pueden subir.
  DASH_USER / DASH_PASS   alternativa para un único usuario (queda como admin).
  SESSION_HOURS  duración de la sesión (default 12). Con "Recordarme": SESSION_REMEMBER_DAYS (default 30).
  COOKIE_SECURE  "1" si el sitio se publica por HTTPS (proxy inverso).

Seguridad:
  - La cookie de sesión es HttpOnly + SameSite=Lax y va firmada con HMAC-SHA256.
  - La firma incluye la clave del usuario: si cambiás la clave o sacás al usuario
    en Portainer, sus sesiones abiertas dejan de valer en el próximo redeploy.
  - 5 intentos fallidos desde la misma IP la bloquean 5 minutos.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip().strip("\"'").strip()


def load_users() -> dict[str, dict]:
    users: dict[str, dict] = {}
    for item in re.split(r"[,\n;]+", _env("DASH_USERS")):
        item = item.strip()
        if not item or ":" not in item:
            continue
        user, rest = item.split(":", 1)
        role = ""
        if rest.lower().endswith(":admin"):
            rest, role = rest[:-6], "admin"
        user, password = user.strip().lower(), rest.strip()
        if user and password:
            users[user] = {"password": password, "admin": role == "admin"}
    single_user, single_pass = _env("DASH_USER").lower(), _env("DASH_PASS")
    if single_user and single_pass:
        users[single_user] = {"password": single_pass, "admin": True}
    if users and not any(u["admin"] for u in users.values()):
        for u in users.values():
            u["admin"] = True
    return users


class Auth:
    COOKIE = "ventas_session"

    def __init__(self, data_dir: Path):
        self.users = load_users()
        self.hours = float(_env("SESSION_HOURS", "12") or 12)
        self.remember_days = float(_env("SESSION_REMEMBER_DAYS", "30") or 30)
        self.secure = _env("COOKIE_SECURE") in ("1", "true", "si", "yes")
        self.secret = self._load_secret(data_dir)
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _load_secret(data_dir: Path) -> bytes:
        env_secret = _env("SESSION_SECRET")
        if env_secret:
            return env_secret.encode()
        path = data_dir / ".session-secret"
        try:
            return path.read_bytes()
        except FileNotFoundError:
            data_dir.mkdir(parents=True, exist_ok=True)
            value = secrets.token_bytes(32)
            path.write_bytes(value)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return value

    @property
    def enabled(self) -> bool:
        return bool(self.users)

    # --- intentos fallidos ---
    def blocked_for(self, ip: str) -> int:
        with self.lock:
            now = time.time()
            recent = [t for t in self.failures.get(ip, []) if now - t < 300]
            self.failures[ip] = recent
            if len(recent) >= 5:
                return int(300 - (now - recent[0])) + 1
            return 0

    def _fail(self, ip: str) -> None:
        with self.lock:
            self.failures.setdefault(ip, []).append(time.time())

    # --- login ---
    def check(self, user: str, password: str, ip: str) -> str | None:
        user = (user or "").strip().lower()
        info = self.users.get(user)
        # compara siempre (aunque el usuario no exista) para no dar pistas por tiempo
        expected = info["password"] if info else secrets.token_hex(16)
        ok = hmac.compare_digest(password.encode(), expected.encode()) and info is not None
        if not ok:
            self._fail(ip)
            time.sleep(0.8)
            return None
        with self.lock:
            self.failures.pop(ip, None)
        return user

    def _sig(self, user: str, exp: int) -> str:
        pw_hash = hashlib.sha256(self.users[user]["password"].encode()).hexdigest()
        msg = f"{user}|{exp}|{pw_hash}".encode()
        return hmac.new(self.secret, msg, hashlib.sha256).hexdigest()

    def make_cookie(self, user: str, remember: bool) -> str:
        ttl = self.remember_days * 86400 if remember else self.hours * 3600
        exp = int(time.time() + ttl)
        raw = f"{user}|{exp}|{self._sig(user, exp)}".encode()
        value = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        attrs = ["Path=/", "HttpOnly", "SameSite=Lax"]
        if remember:
            attrs.append(f"Max-Age={int(ttl)}")
        if self.secure:
            attrs.append("Secure")
        return f"{self.COOKIE}={value}; " + "; ".join(attrs)

    def clear_cookie(self) -> str:
        return f"{self.COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" + ("; Secure" if self.secure else "")

    def user_from_cookie(self, header: str | None) -> str | None:
        if not header:
            return None
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if name != self.COOKIE or not value:
                continue
            try:
                raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
                user, exp, sig = raw.split("|")
                exp_i = int(exp)
            except Exception:
                return None
            if user not in self.users or exp_i < time.time():
                return None
            if hmac.compare_digest(sig, self._sig(user, exp_i)):
                return user
        return None
