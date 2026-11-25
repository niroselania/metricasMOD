"""Bot de Telegram del tablero de ventas.

Corre como un hilo dentro de server.py (mismo contenedor). Usa long polling,
así que no necesita abrir puertos ni tener dominio/HTTPS.

Consultas:
  /resumen [mes]   alcance por canal del mes (por defecto el más reciente)
  /canal RETIRO    detalle de un canal (también sirve escribir el nombre: "retiro")
  /dia [dd/mm]     ventas de un día por canal (por defecto, el último día con ventas)
  /meses           meses cargados
  /avisos          activar / desactivar el aviso cuando se sube una planilla
  /id, /ayuda

Variables de entorno:
  TELEGRAM_BOT_TOKEN      token que da @BotFather (obligatorio para activarlo)
  TELEGRAM_ALLOWED_CHATS  ids de chat permitidos separados por coma (recomendado)
  TELEGRAM_NOTIFY_CHATS   ids que reciben el aviso de planilla nueva
                          (si queda vacío se usan los TELEGRAM_ALLOWED_CHATS)
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

NOMBRES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
           "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
BASE = ["RETIRO", "BARILOCHE", "E-COMMERCE", "MAYORISTA"]
ALIAS = {"ECOMMERCE": "E-COMMERCE", "ECOM": "E-COMMERCE", "WEB": "E-COMMERCE", "ONLINE": "E-COMMERCE",
         "MAYOR": "MAYORISTA", "WHOLESALE": "MAYORISTA", "BRC": "BARILOCHE", "POPUP": "POP UP",
         "TOTAL": "TOTAL", "TOTALES": "TOTAL", "RETAIL": "RETAIL"}


def env(name: str, default: str = "") -> str:
    """Lee una variable y limpia espacios y comillas (Portainer las deja tal cual si se pegan)."""
    return (os.environ.get(name, default) or "").strip().strip("\"'").strip()


def parse_ids(value: str) -> set[int]:
    return {int(p) for p in re.split(r"[,; \n]+", value) if p.strip().lstrip("-").isdigit()}


def plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", text.upper())


API_BASE = env("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")


# ---------- formato ----------
def money(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return "$ " + f"{v / 1e9:.2f}".replace(".", ",") + " MM"
    if a >= 1e6:
        return "$ " + f"{v / 1e6:.1f}".replace(".", ",") + " M"
    return "$ " + f"{v:,.0f}".replace(",", ".")


def num(v: float) -> str:
    return f"{v:,.0f}".replace(",", ".")


def pct(v: float) -> str:
    return f"{v * 100:.1f}".replace(".", ",") + "%"


def light(ratio: float, dias: float) -> str:
    if ratio >= dias:
        return "🟢"
    if ratio >= dias - 0.1:
        return "🟡"
    return "🔴"


# ---------- cálculo (mismo criterio que la web) ----------
class Ventas:
    def __init__(self, data_file: Path):
        self.data_file = data_file
        self._mtime = None
        self._data: dict = {}
        self._lock = threading.Lock()

    def data(self) -> dict:
        try:
            mtime = self.data_file.stat().st_mtime
        except FileNotFoundError:
            return {}
        with self._lock:
            if mtime != self._mtime:
                self._data = json.loads(self.data_file.read_text(encoding="utf-8"))
                self._mtime = mtime
            return self._data

    def months(self) -> list[str]:
        return sorted(self.data().get("meses", {}))

    def month(self, query: str = "") -> tuple[str, dict] | None:
        """Busca un mes por nombre ("oct", "octubre", "octubre 2026") o número ("10", "10/2026")."""
        keys = self.months()
        if not keys:
            return None
        meses = self.data()["meses"]
        if not query.strip():
            return keys[-1], meses[keys[-1]]
        q = plain(query)
        year = None
        mt = re.search(r"(20\d{2})$", q)
        if mt and len(q) > 4:
            year, q = int(mt.group(1)), q[: -4]
        month = None
        if q.isdigit() and 1 <= int(q) <= 12:
            month = int(q)
        elif len(q) >= 3:
            month = next((i for i in range(1, 13) if plain(NOMBRES[i]).startswith(q) or (q.startswith(plain(NOMBRES[i])[:3]) and plain(NOMBRES[i]).startswith(q[:4]))), None)
        if not month:
            return None
        for k in reversed(keys):
            if meses[k]["month"] == month and (year is None or meses[k]["year"] == year):
                return k, meses[k]
        return None

    @staticmethod
    def canales(m: dict) -> list[str]:
        present = [c for c, x in m["canales"].items()
                   if c in BASE or x.get("fct") or x.get("fctU") or any(d[0] or d[1] for d in x["diario"])]
        extras = sorted(c for c in present if c not in BASE)
        return [c for c in ["RETIRO", "BARILOCHE", *extras, "E-COMMERCE", "MAYORISTA"] if c in m["canales"]]

    def group(self, m: dict, key: str) -> list[str]:
        canales = self.canales(m)
        if key == "TOTAL":
            return canales
        if key == "RETAIL":
            return [c for c in canales if c not in ("E-COMMERCE", "MAYORISTA")]
        return [key] if key in m["canales"] else []

    def agg(self, m: dict, key: str) -> dict:
        venta = u = tk = fct = fct_u = 0.0
        for c in self.group(m, key):
            x = m["canales"][c]
            fct += x.get("fct") or 0
            fct_u += x.get("fctU") or 0
            for d in x["diario"]:
                venta += d[0]
                u += d[1]
                tk += d[2]
        return {"venta": venta, "u": u, "tk": tk, "fct": fct, "fctU": fct_u,
                "alc": venta / fct if fct else 0, "alcU": u / fct_u if fct_u else 0}

    @staticmethod
    def dias(m: dict) -> tuple[float, int]:
        today = dt.date.today()
        first = dt.date(m["year"], m["month"], 1)
        d = (today - first).days
        if d >= m["days"]:
            return 1.0, m["days"]
        if d < 0:
            return 0.0, 0
        return d / m["days"], d

    def resolve_channel(self, m: dict, text: str) -> str | None:
        q = plain(text)
        if not q:
            return None
        q = plain(ALIAS.get(q, q))
        options = ["TOTAL", "RETAIL", *self.canales(m)]
        for c in options:
            if plain(c) == q:
                return c
        starts = [c for c in options if plain(c).startswith(q)]
        return starts[0] if len(starts) == 1 else None

    # ---------- textos ----------
    def resumen(self, query: str = "") -> str:
        found = self.month(query)
        if not found:
            return "⚠️ Todavía no hay planilla cargada." if not self.months() else f"❓ No tengo cargado el mes «{html.escape(query)}». Mirá /meses."
        _, m = found
        ratio, day = self.dias(m)
        lines = [f"📊 <b>{html.escape(m['mes'])}</b> · Alcance días <b>{pct(ratio)}</b> (día {day} de {m['days']})", ""]
        for key in ["TOTAL", "RETAIL", *self.canales(m)]:
            a = self.agg(m, key)
            name = "TOTALES" if key == "TOTAL" else key
            bold = key in ("TOTAL", "RETAIL")
            title = f"<b>{name}</b>" if bold else name
            lines.append(f"{light(a['alc'], ratio)} {title}: {money(a['venta'])} · Alc. $ <b>{pct(a['alc'])}</b> · Alc. U <b>{pct(a['alcU'])}</b>")
            if key == "RETAIL":
                lines.append("")
        lines += ["", "🟢 en ritmo · 🟡 leve atraso · 🔴 bajo ritmo", "Detalle: <code>/canal retiro</code> · Día: <code>/dia</code>"]
        return "\n".join(lines)

    def canal(self, text: str) -> str:
        parts = text.split()
        found = None
        # "/canal retiro oct" -> último token puede ser el mes
        if len(parts) > 1 and self.month(parts[-1]) and not self.resolve_channel(self.month()[1], parts[-1]):
            found = self.month(parts[-1])
            text = " ".join(parts[:-1])
        found = found or self.month()
        if not found:
            return "⚠️ Todavía no hay planilla cargada."
        _, m = found
        key = self.resolve_channel(m, text)
        if not key:
            return "❓ No conozco ese canal. Opciones: " + ", ".join(["TOTALES", "RETAIL", *self.canales(m)])
        a = self.agg(m, key)
        ratio, day = self.dias(m)
        name = "TOTALES" if key == "TOTAL" else key
        falta = max(0.0, a["fct"] - a["venta"])
        falta_u = max(0.0, a["fctU"] - a["u"])
        lines = [
            f"🏬 <b>{html.escape(name)}</b> · {html.escape(m['mes'])}",
            "",
            f"{light(a['alc'], ratio)} <b>Alcance ($): {pct(a['alc'])}</b>",
            f"   Venta {money(a['venta'])} de FCT {money(a['fct'])}" + (f" · faltan {money(falta)}" if falta else ""),
            f"⏱ <b>Alcance días: {pct(ratio)}</b> (día {day} de {m['days']})",
            f"🎯 <b>FCT (U): {num(a['fctU'])}</b>" + (f" · faltan {num(falta_u)} U" if falta_u else " · cumplido"),
            f"{light(a['alcU'], ratio)} <b>Alcance (U): {pct(a['alcU'])}</b> ({num(a['u'])} U)",
            "",
            f"🧾 Tickets {num(a['tk'])} · Precio prom. {money(a['venta'] / a['u']) if a['u'] else '–'}"
            + (f" · Ticket prom. {money(a['venta'] / a['tk'])}" if a["tk"] and key != "MAYORISTA" else ""),
        ]
        days_left = m["days"] - day
        if falta and 0 < days_left and ratio < 1:
            lines.append(f"📈 Para llegar al FCT: {money(falta / days_left)} por día los {days_left} días que quedan")
        return "\n".join(lines)

    def dia(self, query: str = "") -> str:
        found = self.month()
        if not found:
            return "⚠️ Todavía no hay planilla cargada."
        q = query.strip()
        day = None
        if q:
            mt = re.match(r"^(\d{1,2})(?:[/\-.](\d{1,2}))?(?:[/\-.](\d{2,4}))?$", q)
            if not mt:
                return "Escribí la fecha así: <code>/dia 24/11</code>"
            day, month = int(mt.group(1)), mt.group(2)
            if month:
                cand = [k for k in self.months() if self.data()["meses"][k]["month"] == int(month)]
                if not cand:
                    return f"❓ No tengo cargado el mes {month}. Mirá /meses."
                found = (cand[-1], self.data()["meses"][cand[-1]])
        _, m = found
        canales = self.canales(m)
        if day is None:
            con_ventas = [i for i in range(m["days"]) if any(m["canales"][c]["diario"][i][0] for c in canales)]
            if not con_ventas:
                return f"Sin ventas cargadas en {html.escape(m['mes'])}."
            day = con_ventas[-1] + 1
        if not 1 <= day <= m["days"]:
            return "Ese día no existe en el mes."
        i = day - 1
        tot_v = tot_u = tot_t = 0
        lines = [f"📅 <b>{day:02d}/{m['month']:02d}/{m['year']}</b>", ""]
        for c in canales:
            v, u, t = m["canales"][c]["diario"][i]
            tot_v += v
            tot_u += u
            tot_t += t
            lines.append(f"• {c}: {money(v)} · {num(u)} U · {num(t)} tickets")
        lines += ["", f"<b>Total: {money(tot_v)} · {num(tot_u)} U · {num(tot_t)} tickets</b>"]
        return "\n".join(lines)

    def meses(self) -> str:
        d = self.data()
        if not d.get("meses"):
            return "⚠️ Todavía no hay planilla cargada."
        lines = ["🗓 <b>Meses cargados</b>", ""]
        for k in reversed(self.months()):
            m = d["meses"][k]
            a = self.agg(m, "TOTAL")
            lines.append(f"• {html.escape(m['mes'])}: {money(a['venta'])} · Alc. $ {pct(a['alc'])}")
        lines += ["", f"Última planilla: {html.escape(d.get('archivo', ''))}", "Ver uno: <code>/resumen oct</code>"]
        return "\n".join(lines)

    def aviso_actualizacion(self, payload: dict) -> str:
        meses = payload.get("mesesActualizados") or []
        nombres = [payload["meses"][k]["mes"] for k in meses if k in payload.get("meses", {})]
        lines = ["🔄 <b>Datos de ventas actualizados</b>",
                 f"📁 {html.escape(payload.get('archivo', ''))}",
                 f"🗓 Meses: {html.escape(', '.join(nombres) or '—')}", ""]
        if meses:
            lines.append(self.resumen_payload(payload["meses"][meses[-1]]))
            lines += ["", "Más detalle: /resumen · <code>/canal retiro</code> · /dia"]
        return "\n".join(lines)

    def resumen_payload(self, m: dict) -> str:
        ratio, day = self.dias(m)
        canales = self.canales(m)
        con_ventas = [i for i in range(m["days"]) if any(m["canales"][c]["diario"][i][0] for c in canales)]
        hasta = f" · ventas hasta el {con_ventas[-1] + 1:02d}/{m['month']:02d}" if con_ventas else ""
        out = [f"📊 <b>{html.escape(m['mes'])}</b> · Alcance días {pct(ratio)}{hasta}"]
        for key in ["TOTAL", "RETAIL", *self.canales(m)]:
            a = self.agg(m, key)
            name = "TOTALES" if key == "TOTAL" else key
            out.append(f"{light(a['alc'], ratio)} {name}: {money(a['venta'])} · Alc. $ {pct(a['alc'])} · Alc. U {pct(a['alcU'])}")
        return "\n".join(out)


HELP_TEXT = (
    "📊 <b>Ventas · Centro de control</b>\n\n"
    "• /resumen → alcance de todos los canales del mes actual\n"
    "• <code>/resumen oct</code> → otro mes\n"
    "• <code>/canal retiro</code> → detalle de un canal (o escribí directamente <code>retiro</code>)\n"
    "• /dia → ventas del último día · <code>/dia 24/11</code> → un día puntual\n"
    "• /meses → meses cargados\n"
    "• /avisos → activar o desactivar el aviso cuando se sube una planilla nueva\n"
    "• /id → id de este chat"
)


class TelegramBot:
    def __init__(self, token: str, ventas: Ventas, allowed: set[int], notify: set[int], subs_file: Path,
                 log: Callable[[str], None] = print):
        self.token = token
        self.ventas = ventas
        self.allowed = allowed
        self.notify_env = notify
        self.subs_file = subs_file
        self.log = log
        self.offset = 0
        self.status: dict[str, Any] = {"enabled": True, "connected": False, "username": None, "lastError": None,
                                       "allowedChats": len(allowed), "lastMessageAt": None, "lastNotify": None}

    # --- suscripciones (/avisos) ---
    def _subs(self) -> dict:
        try:
            return json.loads(self.subs_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"on": [], "off": []}

    def _save_subs(self, subs: dict) -> None:
        tmp = self.subs_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(subs), encoding="utf-8")
        tmp.replace(self.subs_file)

    def notify_targets(self) -> set[int]:
        subs = self._subs()
        base = self.notify_env or self.allowed
        return (set(base) | set(subs.get("on", []))) - set(subs.get("off", []))

    def toggle_avisos(self, chat_id: int) -> str:
        subs = self._subs()
        on, off = set(subs.get("on", [])), set(subs.get("off", []))
        if chat_id in self.notify_targets():
            on.discard(chat_id)
            off.add(chat_id)
            msg = "🔕 Avisos desactivados para este chat. Mandá /avisos para volver a activarlos."
        else:
            off.discard(chat_id)
            on.add(chat_id)
            msg = "🔔 Avisos activados: te escribo cada vez que se sube una planilla nueva."
        self._save_subs({"on": sorted(on), "off": sorted(off)})
        return msg

    # --- API ---
    def call(self, method: str, payload: dict | None = None, timeout: float = 30) -> Any:
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(f"{API_BASE}/bot{self.token}/{method}", data=data,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(body.get("description", "error de Telegram"))
        return body.get("result")

    def send(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
            payload["allow_sending_without_reply"] = True
        self.call("sendMessage", payload)

    def notify_update(self, payload: dict) -> None:
        """Avisa en segundo plano a los chats suscriptos que se subió una planilla."""
        def work():
            targets = self.notify_targets()
            if not targets:
                self.log("[telegram] planilla nueva, pero no hay chats para avisar (TELEGRAM_NOTIFY_CHATS / /avisos)")
                return
            text = self.ventas.aviso_actualizacion(payload)
            ok = 0
            for chat_id in targets:
                try:
                    self.send(chat_id, text)
                    ok += 1
                except Exception as error:
                    self.log(f"[telegram] no pude avisar a {chat_id}: {error}")
            self.status["lastNotify"] = {"at": int(time.time()), "chats": ok}
            self.log(f"[telegram] aviso de planilla nueva enviado a {ok} chat(s)")
        threading.Thread(target=work, name="telegram-notify", daemon=True).start()

    # --- lógica ---
    def handle(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        if not text:
            return
        parts = text.split(maxsplit=1)
        command = parts[0].split("@")[0].lower() if text.startswith("/") else ""
        arg = parts[1].strip() if command and len(parts) > 1 else ""

        if command == "/id":
            return self.send(chat_id, f"🆔 El id de este chat es <code>{chat_id}</code>")
        if self.allowed and chat_id not in self.allowed:
            self.log(f"[telegram] chat no autorizado {chat_id}")
            return self.send(chat_id, f"⛔ Este chat no está autorizado.\nPasale este id al administrador: <code>{chat_id}</code>")
        self.status["lastMessageAt"] = int(time.time())
        if command in ("/start", "/ayuda", "/help"):
            answer = HELP_TEXT
        elif command in ("/resumen", "/r", "/alcance"):
            answer = self.ventas.resumen(arg)
        elif command in ("/canal", "/c"):
            answer = self.ventas.canal(arg) if arg else "Escribí el canal, ej: <code>/canal retiro</code>"
        elif command in ("/dia", "/día", "/d", "/hoy", "/ayer"):
            answer = self.ventas.dia(arg)
        elif command == "/meses":
            answer = self.ventas.meses()
        elif command == "/avisos":
            answer = self.toggle_avisos(chat_id)
        elif command:
            answer = "No conozco ese comando. Mirá /ayuda."
        else:
            # Texto libre: nombre de canal ("retiro", "ecommerce oct") o de mes ("octubre")
            m = self.ventas.month()
            if m and self.ventas.resolve_channel(m[1], text.split()[0]):
                answer = self.ventas.canal(text)
            elif self.ventas.month(text):
                answer = self.ventas.resumen(text)
            else:
                answer = "No entendí 🤔. Probá con /resumen, el nombre de un canal (ej. <code>retiro</code>) o /ayuda."
        self.log(f"[telegram] '{text[:40]}' de {chat_id}")
        self.send(chat_id, answer, reply_to=message.get("message_id"))

    def run(self) -> None:
        backoff = 2
        while True:
            try:
                if not self.status["connected"]:
                    me = self.call("getMe")
                    self.call("deleteWebhook", {"drop_pending_updates": False})
                    try:
                        self.call("setMyCommands", {"commands": [
                            {"command": "resumen", "description": "Alcance de todos los canales"},
                            {"command": "canal", "description": "Detalle de un canal (ej. /canal retiro)"},
                            {"command": "dia", "description": "Ventas de un día (ej. /dia 24/11)"},
                            {"command": "meses", "description": "Meses cargados"},
                            {"command": "avisos", "description": "Activar/desactivar aviso de planilla nueva"},
                            {"command": "ayuda", "description": "Cómo usar el bot"},
                            {"command": "id", "description": "Ver el id de este chat"},
                        ]})
                    except Exception:
                        pass
                    self.status.update(connected=True, username=me.get("username"), lastError=None)
                    self.log(f"[telegram] bot @{me.get('username')} conectado"
                             + (f" · {len(self.allowed)} chat(s) autorizados" if self.allowed else " · SIN lista de chats (cualquiera puede consultar)"))
                updates = self.call("getUpdates", {"offset": self.offset, "timeout": 50, "allowed_updates": ["message"]}, timeout=65)
                backoff = 2
                for update in updates:
                    self.offset = update["update_id"] + 1
                    if update.get("message"):
                        try:
                            self.handle(update["message"])
                        except Exception as error:  # un mensaje malo no tumba el bot
                            self.log(f"[telegram] error respondiendo: {error}")
            except urllib.error.HTTPError as error:
                self.status.update(connected=False, lastError=f"HTTP {error.code}")
                if error.code == 401:
                    self.log("[telegram] token inválido: revisá TELEGRAM_BOT_TOKEN. Bot detenido.")
                    self.status["enabled"] = False
                    return
                if error.code == 409:
                    self.log("[telegram] otro proceso usa este bot (¿mismo token que el bot de stock?). Creá un bot aparte.")
                else:
                    self.log(f"[telegram] HTTP {error.code}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as error:
                self.status.update(connected=False, lastError=str(error)[:200])
                self.log(f"[telegram] sin conexión ({error}); reintento en {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


def start_bot(data_file: Path, data_dir: Path) -> TelegramBot | None:
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    if ":" not in token:
        print("[telegram] OJO: el token no tiene el formato 123456789:AAxxxx. Revisá TELEGRAM_BOT_TOKEN.", flush=True)
    bot = TelegramBot(token, Ventas(data_file), parse_ids(env("TELEGRAM_ALLOWED_CHATS")),
                      parse_ids(env("TELEGRAM_NOTIFY_CHATS")), data_dir / "telegram-avisos.json",
                      log=lambda m: print(m, flush=True))
    threading.Thread(target=bot.run, name="telegram-bot", daemon=True).start()
    return bot
