# Ventas · Centro de control (v3.0.1)

## Novedades v3.0.1
- Paleta azul noche: mejor contraste en porcentajes, etiquetas de estado y textos sobre las tarjetas de vidrio.

## Novedades v3.0.0
- **Ingreso con usuario y contraseña** antes de ver cualquier dato (también protege `/api/data`).
- Diseño nuevo **"liquid glass"**: tarjetas de vidrio esmerilado, barra lateral, botón naranja.
- Usuarios **admin** (suben planillas) y de **consulta** (solo ven).

### Usuarios (Portainer > Stacks > ventas-dashboard > Environment variables)
| Variable | Ejemplo | Para qué |
|---|---|---|
| `DASH_USERS` | `ariel:MiClave2026:admin, comercial:OtraClave` | Lista de usuarios. `:admin` = puede subir planillas. Si nadie tiene `:admin`, todos pueden subir. |
| `SESSION_HOURS` | `12` | Horas que dura la sesión (con "Recordarme": 30 días). |
| `UPLOAD_TOKEN` | *(vacío)* | Opcional: código extra al subir. Con usuarios admin ya no hace falta. |

- Separá usuarios con coma. La clave no puede tener comas; evitá `:` y `$`.
- El usuario no distingue mayúsculas; la clave sí.
- Para **agregar, sacar o cambiar la clave** de alguien: editá `DASH_USERS` y hacé *Update the stack*.
  Las sesiones abiertas de ese usuario se cierran solas.
- 5 claves erróneas seguidas bloquean esa IP 5 minutos.
- Si lo publicás con HTTPS detrás de un proxy, agregá `COOKIE_SECURE=1` (y `TRUST_PROXY=1`).
- `http://IP:8086/api/health` sigue respondiendo sin login, pero sin datos de ventas.


Tablero de ventas sin IVA con gráficos circulares de **Alcance ($)**, **Alcance Días**,
**FCT (U)** y **Alcance (U)** por canal (Retail, Retiro, Bariloche, E-commerce, Mayorista).

La planilla que se sube queda **guardada en el servidor**: cualquier navegador o
computadora ve siempre la última versión, y se puede elegir el mes.

| Archivo | Qué hace |
|---|---|
| `index.html` | El tablero |
| `auth.py` | Usuarios, contraseñas y sesiones |
| `login.html` | Pantalla de ingreso |
| `server.py` | Sirve el sitio, recibe la planilla y guarda los datos en `/data` |
| `telegram_bot.py` | Bot de consultas y aviso de planilla nueva |
| `ventas_processor.py` | Lee las pestañas `DB <MES>` (detalle) y `<MES>` (FCT) |
| `Dockerfile`, `requirements.txt`, `docker-compose.yml` | Contenedor y stack para Portainer (puerto **8086**) |

> ⚠️ Repositorio **Private**: no subas la planilla a GitHub (el `.gitignore` ya la excluye).

## Bot de Telegram (v2.2.0)

Consultas:
| Mensaje | Respuesta |
|---|---|
| `/resumen` · `/resumen oct` | Alcance ($) y (U) de todos los canales, con semáforo vs Alcance días |
| `/canal retiro` o solo `retiro` | Alcance ($), Alcance días, FCT (U), Alcance (U), tickets y cuánto falta por día |
| `/dia` · `/dia 24/11` | Ventas del último día cargado o de un día puntual, por canal |
| `/meses` | Meses cargados con su venta y alcance |
| `/avisos` | Activa/desactiva el aviso de planilla nueva en ese chat |
| `/id` | Id del chat (para autorizarlo) |

Cada vez que se sube una planilla, el bot avisa a los chats configurados con el
archivo, los meses actualizados y el resumen del último mes.

Activarlo:
1. En Telegram hablale a **@BotFather** > `/newbot` y creá un bot **nuevo**
   (no uses el token del bot de stock: dos contenedores con el mismo token se pisan).
2. Portainer > Stacks > ventas-dashboard > **Environment variables**:
   `TELEGRAM_BOT_TOKEN` = el token. Update the stack.
3. Escribile `/id` al bot (desde tu chat o un grupo donde lo agregues) y cargá
   `TELEGRAM_ALLOWED_CHATS` = `123456789,-1001234567890`. Update the stack.
   Sin esta lista cualquiera que encuentre el bot puede ver las ventas.
4. (Opcional) `TELEGRAM_NOTIFY_CHATS` = quiénes reciben el aviso de planilla nueva.
   Si queda vacío, avisa a todos los `TELEGRAM_ALLOWED_CHATS`. Cada chat puede
   prender/apagar el aviso con `/avisos`.
5. Verificación: `http://IP:8086/api/health` muestra `"telegram": {"connected": true, ...}`.

No hace falta abrir puertos: el contenedor sale a Telegram (necesita internet).

## Novedades v2.1.0
- **Las cargas se combinan por mes**: cada planilla reemplaza solo los meses que trae;
  los demás quedan como estaban. Se puede subir una planilla con todo el año y después
  otra solo con noviembre actualizado.
- **Tiendas extra** (ej. POP UP en julio–septiembre) aparecen solas como pestaña y
  se suman a RETAIL y TOTALES.
- Debajo del título se ve de qué planilla y cuándo se cargó el mes que estás mirando.

Para pasar de 2.0 a 2.1: reemplazá `index.html`, `server.py`, `ventas_processor.py`,
`docker-compose.yml` y `README.md` en GitHub y hacé **Pull and redeploy**. Los datos ya
cargados se conservan.

## Actualizar desde la v1 (la versión con nginx)
1. En GitHub reemplazá `index.html`, `Dockerfile`, `docker-compose.yml`, `README.md`,
   `.gitignore`, `.dockerignore` y agregá `server.py`, `ventas_processor.py`, `requirements.txt`.
2. **Borrá `nginx.conf`** del repositorio (ya no se usa).
3. Portainer > Stacks > ventas-dashboard > **Pull and redeploy** (con *Re-pull image* / rebuild).

## Instalación nueva
1. GitHub > **New repository** > `ventas-dashboard` > **Private** > subí estos archivos.
2. Portainer > **Stacks > Add stack** > Build method **Repository**:
   - URL `https://github.com/TU-USUARIO/ventas-dashboard`, reference `refs/heads/main`,
     compose path `docker-compose.yml`.
   - **Authentication**: usuario de GitHub + Personal Access Token (lectura de *Contents*).
3. **Environment variables** > agregá `UPLOAD_TOKEN` = un código a elección (recomendado).
4. **Deploy the stack** y abrí `http://IP-DE-TU-SERVIDOR:8086`.

## Uso
- **Subir planilla**: la primera vez pide el código de carga (queda recordado en ese navegador).
  Procesa todos los meses que tengan pestaña `DB <MES>` con datos + `<MES>` (≈15 s con la planilla FY27).
  Un mes que viene en la planilla nueva **reemplaza** al guardado (gana la última carga);
  no subas una planilla vieja después de una nueva.
- **Mes**: selector arriba a la derecha (por defecto, el más reciente).
- Alcance Días = días transcurridos / días del mes; en meses cerrados es 100%.
- El sitio se refresca solo cada 5 minutos.

## Datos y verificación
- Volumen `ventas-data` (`/data`): `ventas-data.json` (vigente), `history/` (últimas 30 cargas).
- `http://IP:8086/api/health` → estado · `/api/history` → últimas cargas.
- Si el código de carga se pierde: cambiá `UPLOAD_TOKEN` en Portainer y hacé Redeploy.
