"""
MCP de alertas de X (Twitter) — v0.3
=====================================

Le da a Claude cuatro herramientas:

  1. buscar_en_x          -> busca posts recientes sobre un tema, para ver qué se está diciendo
  2. reacciones_a_posts   -> qué le responde la gente a los posts de una cuenta (ej. @fedesturze) o a
                             un post puntual: respuestas, citas, discusiones entre usuarios y si se
                             armó un debate
  3. revisar_alertas      -> revisa los temas y cuentas configurados y avisa si hay algo
  4. estado_del_scraper   -> dice si la cuenta de X está andando o si hay que renovar las cookies

De dónde saca los datos
-----------------------
X no deja buscar sin iniciar sesión. Este servidor usa twscrape (open source y gratis), que
le habla a X igual que la página web, usando las cookies de una cuenta de X.

    USÁ UNA CUENTA SECUNDARIA. El uso automatizado va contra las reglas de X y la cuenta
    que uses puede quedar suspendida. Nunca pongas acá las cookies de tu cuenta principal.

Qué monitorea
-------------
Los temas y cuentas están más abajo, en TEMAS_POR_DEFECTO y CUENTAS_POR_DEFECTO. Se pueden
reemplazar sin tocar el código con las variables TEMAS y CUENTAS (ver README).

Variables de entorno (en Render: pestaña Environment)
-----------------------------------------------------
  X_COOKIES     cookies de la cuenta secundaria, así: auth_token=XXXX; ct0=YYYY    (obligatoria)
  X_USUARIO     usuario de esa cuenta, solo como etiqueta                           (opcional)
  CLAVE_ACCESO  clave secreta que va en la URL del conector (mín. 16 caracteres)    (obligatoria con --http)
  TEMAS         reemplaza la lista de temas:   Nombre | búsqueda | siempre ; ...    (opcional)
  CUENTAS       reemplaza la lista de cuentas: usuario | Nombre | menciones ; ...   (opcional)
  ZONA_HORARIA  por defecto America/Argentina/Buenos_Aires                          (opcional)

Uso:
    python server.py            # modo local (stdio)
    python server.py --http     # modo remoto (HTTP): https://TU-APP.onrender.com/<CLAVE_ACCESO>/mcp
"""

from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
from collections import Counter, deque
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

os.environ.setdefault("TWS_TELEMETRY", "0")  # que twscrape no mande estadísticas de uso

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from twscrape import API, NoAccountError
from twscrape.api import OP_SearchTimeline, OP_TweetDetail
from twscrape.logger import logger as log_twscrape

# ---------------------------------------------------------------------------
# Qué monitorear
# ---------------------------------------------------------------------------
# Cada tema: (nombre, búsqueda en X, avisar_siempre). Las búsquedas usan los operadores de X:
# "frase exacta", OR, paréntesis para agrupar. Se ponen las palabras con y sin tilde por las dudas.
# avisar_siempre=True: avisa cada vez que aparece algo, aunque no haya pico ni posts virales.

TEMAS_POR_DEFECTO: list[tuple[str, str, bool]] = [
    ("Empleo público",
     '"empleo público" OR "empleo publico" OR "empleados públicos" OR "empleados publicos"', False),
    ("Despidos en el Estado",
     '(despidos OR despedidos OR cesantías OR cesantias) '
     '(estatales OR "en el Estado" OR "sector público" OR "sector publico" OR "administración pública")', False),
    ("Conflictos gremiales",
     '("conflicto gremial" OR "conflictos gremiales" OR paro OR "medida de fuerza" OR "plan de lucha") '
     '(estatales OR "sector público" OR "sector publico" OR "administración pública" OR ATE OR UPCN)', False),
    ("Retiro voluntario", '"retiro voluntario" OR "retiros voluntarios"', False),
    ("ATE", "ATE (estatales OR paro OR gremio OR sindicato OR trabajadores OR despidos OR Estado OR movilización)", False),
    ("UPCN", "UPCN", False),
    ("Rosana Reggi", '"Rosana Reggi"', True),
]

# Cada cuenta: (usuario de X, nombre, otras formas de mencionarla además del @).
CUENTAS_POR_DEFECTO: list[tuple[str, str, str]] = [
    ("fedesturze", "Federico Sturzenegger", "Sturzenegger OR Sturze"),
    ("MaxiFarina", "Maximiliano Fariña",
     '"Maxi Fariña" OR "Maximiliano Fariña" OR "Maxi Farina" OR "Maximiliano Farina"'),
]

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

Orden = Literal["recientes", "destacados"]
Incluir = Literal["todo", "temas", "cuentas"]
MAX_POSTS = 200              # tope por búsqueda, para no gastar de más la cuenta de X
MAX_POSTS_ALERTA = 100       # tope por ventana en revisar_alertas
MAX_ITEMS_ALERTA = 12        # temas + cuentas por revisión
MIN_POSTS_PICO = 10          # con menos posts que esto no se considera "pico"
MIN_CONVERSACION = 10        # posts con menos respuestas + citas que esto no se analizan en las alertas
CUENTA_GRANDE = 50000        # seguidores para considerar "grande" a una cuenta
ESPERA_CUENTA = 10           # segundos que espera si la cuenta está ocupada con otra búsqueda
COLA_BUSQUEDA = OP_SearchTimeline.split("/")[-1]  # nombres con los que twscrape anota los límites
COLA_RESPUESTAS = OP_TweetDetail.split("/")[-1]
DB_CUENTAS = os.environ.get("X_DB", "/tmp/x_cuentas.db")
ZONA = ZoneInfo(os.environ.get("ZONA_HORARIA", "America/Argentina/Buenos_Aires"))

# Los tests reemplazan esto por una API falsa para no salir a internet.
_api: Any = None
_preparada = False


@dataclass
class Tema:
    nombre: str
    consulta: str
    avisar_siempre: bool = False


@dataclass
class Cuenta:
    usuario: str
    nombre: str
    menciones: str = ""

    @property
    def arroba(self) -> str:
        return f"@{self.usuario}"

    def consulta_menciones(self) -> str:
        extra = f" OR {self.menciones}" if self.menciones else ""
        return f"(@{self.usuario}{extra}) -from:{self.usuario}"


def _sin_tildes(texto: str) -> str:
    plano = unicodedata.normalize("NFKD", texto.casefold())
    return "".join(c for c in plano if not unicodedata.combining(c)).strip()


def _partes(texto: str) -> list[str]:
    return [p.strip() for p in texto.split("|")]


def _entradas(variable: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;\n]", os.environ.get(variable, "")) if p.strip()]


def parsear_tema(texto: str) -> Tema:
    """'Nombre | búsqueda | siempre', 'Nombre | búsqueda' o solo 'búsqueda'."""
    p = _partes(texto)
    if len(p) == 1:
        return Tema(p[0], p[0])
    return Tema(p[0], p[1] or p[0], len(p) > 2 and _sin_tildes(p[2]) in ("siempre", "si", "true"))


def parsear_cuenta(texto: str) -> Cuenta:
    """'usuario | Nombre | otras menciones', 'usuario | Nombre' o solo 'usuario'."""
    p = _partes(texto)
    usuario = p[0].lstrip("@")
    if not re.fullmatch(r"\w{1,15}", usuario):
        raise ToolError(f"'{p[0]}' no parece un usuario de X (ej. @fedesturze).")
    return Cuenta(usuario, p[1] if len(p) > 1 and p[1] else f"@{usuario}", p[2] if len(p) > 2 else "")


def temas_configurados() -> list[Tema]:
    if entradas := _entradas("TEMAS"):
        return [parsear_tema(e) for e in entradas]
    return [Tema(*t) for t in TEMAS_POR_DEFECTO]


def cuentas_configuradas() -> list[Cuenta]:
    if entradas := _entradas("CUENTAS"):
        return [parsear_cuenta(e) for e in entradas]
    return [Cuenta(*c) for c in CUENTAS_POR_DEFECTO]


def resolver_tema(texto: str) -> Tema:
    """Si el texto es el nombre de un tema configurado ('despidos en el estado'), usa su búsqueda."""
    buscado = _sin_tildes(texto)
    for t in temas_configurados():
        if _sin_tildes(t.nombre) == buscado:
            return t
    return parsear_tema(texto)


def resolver_cuenta(texto: str) -> Cuenta:
    """Acepta '@fedesturze', 'fedesturze' o un nombre configurado ('Sturzenegger', 'Fariña')."""
    buscado = _sin_tildes(texto.lstrip("@"))
    for c in cuentas_configuradas():
        if buscado == _sin_tildes(c.usuario) or (len(buscado) >= 4 and buscado in _sin_tildes(c.nombre)):
            return c
    return parsear_cuenta(texto)


def _instrucciones() -> str:
    try:
        temas = ", ".join(t.nombre for t in temas_configurados())
        cuentas = ", ".join(f"{c.nombre} ({c.arroba})" for c in cuentas_configuradas())
    except ToolError:  # CUENTAS mal escrita: el error se ve al usar las herramientas
        temas = cuentas = "(ver estado_del_scraper)"
    return (
        "Herramientas para monitorear qué se dice en X (Twitter). Temas configurados: "
        f"{temas}. Cuentas seguidas: {cuentas}. buscar_en_x trae los posts recientes de un tema o "
        "búsqueda (si le pasás el nombre de un tema configurado usa su búsqueda armada). "
        "reacciones_a_posts lee qué le responde la gente a los posts de una cuenta (respuestas, citas, "
        "discusiones entre usuarios) y si se armó un debate. revisar_alertas revisa todo lo configurado. "
        "Al resumir la reacción a un post, contá qué argumentos y críticas aparecen, el tono "
        "(apoyo, rechazo, burla, preguntas), quiénes participan y si hay discusión entre usuarios; citá "
        "ejemplos con su enlace y no presentes lo que dice un post como un hecho comprobado. "
        "Si algo falla, usá estado_del_scraper."
    )


mcp = MCPServer(name="x-alertas", title="Alertas de X (Twitter)", instructions=_instrucciones())


@mcp.custom_route("/", methods=["GET", "HEAD"])
async def salud(request: Request) -> PlainTextResponse:
    """Para que Render (o vos) pueda ver que el servidor está prendido. No expone nada."""
    return PlainTextResponse("ok")


# ---------------------------------------------------------------------------
# Conexión con X (twscrape)
# ---------------------------------------------------------------------------


async def cliente_x() -> Any:
    """Devuelve la API de twscrape con la cuenta cargada desde X_COOKIES (una sola vez)."""
    global _api, _preparada
    if _api is None:
        _api = API(DB_CUENTAS, raise_when_no_account=True, wait_timeout=ESPERA_CUENTA, wait_interval=2)
    if not _preparada:
        cookies = os.environ.get("X_COOKIES", "").strip()
        if not cookies:
            raise ToolError(
                "Falta la variable X_COOKIES en el servidor. Cargá las cookies de la cuenta secundaria "
                "de X (auth_token=...; ct0=...) en Render → Environment y reiniciá el servicio."
            )
        usuario = os.environ.get("X_USUARIO", "").strip().lstrip("@") or "cuenta_secundaria"
        try:
            await _api.pool.add_account_cookies(usuario, cookies)
        except ValueError as e:
            raise ToolError(
                "Las cookies de X_COOKIES no son válidas: tienen que incluir auth_token y ct0, "
                "con este formato: auth_token=XXXX; ct0=YYYY"
            ) from e
        await _api.pool.reset_locks()  # cookies nuevas: arranca sin pausas viejas
        _preparada = True
    return _api


def _minutos_hasta(hora: str | None) -> int | None:
    """twscrape avisa cuándo se libera la cuenta como 'HH:MM:SS' (hora del servidor)."""
    if not hora or hora == "now":
        return None
    try:
        ahora = datetime.now()
        objetivo = datetime.combine(ahora.date(), datetime.strptime(hora, "%H:%M:%S").time())
    except ValueError:
        return None
    if objetivo < ahora:
        objetivo += timedelta(days=1)
    return max(1, round((objetivo - ahora).total_seconds() / 60))


# twscrape no siempre avisa con una excepción: a veces solo escribe un aviso en el log y
# devuelve cero resultados. Guardamos sus avisos para poder explicar qué pasó.
_avisos: deque[tuple[float, str]] = deque(maxlen=50)


def _guardar_aviso(mensaje: Any) -> None:
    _avisos.append((time.monotonic(), mensaje.record["message"]))


log_twscrape.add(_guardar_aviso, level="WARNING")

MSG_COOKIES = (
    "X no reconoce la sesión de la cuenta secundaria: las cookies vencieron, son incorrectas o se "
    "cerró la sesión. Entrá a x.com con esa cuenta, copiá de nuevo auth_token y ct0, actualizá "
    "X_COOKIES en Render y reiniciá el servicio. (Ojo: si tocás 'Cerrar sesión' en X, las cookies dejan "
    "de servir.)"
)
MSG_SUSPENDIDA = (
    "X restringió o suspendió la cuenta secundaria. Entrá a x.com con esa cuenta para ver si te pide "
    "verificar algo. Si quedó suspendida, vas a necesitar otra cuenta secundaria y cookies nuevas."
)
MSG_BLOQUEO = (
    "X bloqueó los pedidos que salen del servidor (suele pasar con servidores en la nube). Probá "
    "agregando en Render la variable TWS_HTTP_BACKEND=curl (hace que el pedido se parezca más al de "
    "Chrome) y reiniciá. Si sigue bloqueado, la alternativa es correr el servidor en tu compu."
)
MSG_ACTUALIZAR = (
    "X cambió algo en su página y twscrape todavía no lo entiende. En Render tocá Manual Deploy → "
    "Clear build cache & deploy para instalar la última versión de twscrape. Si sigue igual, hay que "
    "esperar a que lo arreglen (github.com/vladkens/twscrape/issues)."
)
MSG_RED = "No se pudo conectar con X (problema de red). Probá de nuevo en un rato."

_PROBLEMAS = [
    (("Logged-out X web app", "Session expired or banned", "Could not authenticate",
      "Missing authentication cookies"), MSG_COOKIES),
    (("Ban detected", "Denied by access control"), MSG_SUSPENDIDA),
    (("Blocked by Cloudflare", "Blocked by HTML"), MSG_BLOQUEO),
    (("Signing indices", "Update required", "vladkens/twscrape/issues", "API unknown error"), MSG_ACTUALIZAR),
    (("cooling account",), MSG_RED),
]


def diagnosticar(desde: float) -> str | None:
    """Mira los avisos de twscrape posteriores a `desde` (time.monotonic) y explica el problema."""
    recientes = [m for t, m in _avisos if t >= desde]
    for claves, explicacion in _PROBLEMAS:
        if any(c in m for c in claves for m in recientes):
            return explicacion
    return None


async def _explicar_sin_cuenta(cola: str = COLA_BUSQUEDA) -> str:
    """Cuando twscrape no tiene la cuenta disponible: ¿sesión vencida, bloqueo o límite de X?"""
    api = await cliente_x()
    info = await api.pool.accounts_info()
    if not any(a.get("active") for a in info):
        errores = " ".join(str(a.get("error_msg")) for a in info)
        return MSG_SUSPENDIDA if ("(88)" in errores or "(326)" in errores) else MSG_COOKIES
    # La cuenta queda en pausa 15 minutos tanto por un problema como por el límite de búsquedas.
    if explicacion := diagnosticar(time.monotonic() - 20 * 60):
        return explicacion
    minutos = _minutos_hasta(await api.pool.next_available_at(cola))
    espera = f"en unos {minutos} minutos" if minutos else "en unos minutos"
    return (
        f"La cuenta llegó al límite de consultas que permite X. Se libera {espera}. "
        "Probá de nuevo después, o pedí menos posts o menos temas por vez."
    )


async def _juntar(generador: Any, limite: int, cola: str) -> list[Any]:
    """Recorre un generador de twscrape con manejo de errores, hasta `limite` posts."""
    posts: list[Any] = []
    inicio = time.monotonic()
    try:
        async with aclosing(generador) as gen:
            async for post in gen:
                posts.append(post)
                if len(posts) >= limite:
                    break
    except NoAccountError as e:
        raise ToolError(await _explicar_sin_cuenta(cola)) from e
    except ToolError:
        raise
    except Exception as e:  # cambios de X, red, etc.
        raise ToolError(
            f"Falló la consulta a X ({e.__class__.__name__}: {str(e)[:200]}). Si se repite, puede que X "
            "haya cambiado algo: en Render tocá Manual Deploy → Clear build cache & deploy para "
            "actualizar twscrape."
        ) from e
    if not posts and (explicacion := diagnosticar(inicio)):
        raise ToolError(explicacion)  # cero resultados por un problema, no porque no haya posts
    return posts


async def traer_posts(consulta_x: str, limite: int, orden: Orden = "recientes") -> list[Any]:
    """Corre una búsqueda en X y devuelve hasta `limite` posts (objetos Tweet de twscrape)."""
    api = await cliente_x()
    producto = "Top" if orden == "destacados" else "Latest"
    return await _juntar(api.search(consulta_x, limit=limite, kv={"product": producto}), limite, COLA_BUSQUEDA)


async def traer_post(id_post: int) -> Any:
    """Un post puntual por su número."""
    api = await cliente_x()
    inicio = time.monotonic()
    try:
        post = await api.tweet_details(id_post)
    except NoAccountError as e:
        raise ToolError(await _explicar_sin_cuenta(COLA_RESPUESTAS)) from e
    except Exception as e:
        raise ToolError(f"Falló la consulta a X ({e.__class__.__name__}: {str(e)[:200]}).") from e
    if post is None:
        raise ToolError(
            diagnosticar(inicio)
            or "No encontré ese post: puede estar borrado, ser de una cuenta privada o el enlace estar mal."
        )
    return post


async def traer_conversacion(id_post: int, limite: int) -> list[Any]:
    """Todos los posts de la conversación de un post: respuestas, respuestas a las respuestas y
    lo que el propio autor agregó."""
    api = await cliente_x()
    return await _juntar(api.tweet_thread(id_post, limit=limite), limite, COLA_RESPUESTAS)


async def traer_citas(id_post: int, limite: int) -> list[Any]:
    """Posts que citan a otro (lo comparten con un comentario propio). Primero prueba el operador
    quoted_tweet_id: (el de la pestaña "Citas" de X) y, si no trae nada, busca el enlace del post."""
    for consulta in (f"quoted_tweet_id:{id_post}", f"url:{id_post}"):
        citas = await traer_posts(consulta, limite, "destacados")
        citas = [c for c in citas if getattr(getattr(c, "quotedTweet", None), "id", None) == id_post]
        if citas:
            return citas
    return []


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _or_suelto(texto: str) -> bool:
    """True si hay un OR fuera de paréntesis y de comillas (ej. 'a OR b', pero no '(a OR b) c')."""
    nivel, entre_comillas = 0, False
    for i, ch in enumerate(texto):
        if ch == '"':
            entre_comillas = not entre_comillas
        elif not entre_comillas:
            if ch == "(":
                nivel += 1
            elif ch == ")":
                nivel -= 1
            elif nivel == 0 and texto.startswith(" OR ", i):
                return True
    return False


def armar_consulta(
    consulta: str,
    desde: datetime,
    hasta: datetime | None = None,
    idioma: str | None = None,
    sin_retuits: bool = True,
    incluir_respuestas: bool = True,
) -> str:
    """Arma la búsqueda para X con los operadores de fecha, idioma y filtros."""
    base = consulta.strip()
    if _or_suelto(base):
        base = f"({base})"  # que el OR no se mezcle con los filtros que agregamos
    partes = [base, f"since_time:{int(desde.timestamp())}"]
    if hasta:
        partes.append(f"until_time:{int(hasta.timestamp())}")
    if idioma:
        partes.append(f"lang:{idioma.strip().lower()}")
    if sin_retuits and "filter:retweets" not in consulta:
        partes.append("-filter:retweets")
    if not incluir_respuestas and "filter:replies" not in consulta:
        partes.append("-filter:replies")
    return " ".join(partes)


def fecha_utc(post: Any) -> datetime:
    d = post.date
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def interacciones(post: Any) -> int:
    return sum(getattr(post, c, 0) or 0 for c in ("likeCount", "retweetCount", "replyCount", "quoteCount"))


def seguidores(post: Any) -> int:
    return (getattr(post.user, "followersCount", 0) or 0) if post.user else 0


def post_a_dict(post: Any, largo_texto: int = 500) -> dict[str, Any]:
    u = post.user
    texto = re.sub(r"\s+", " ", post.rawContent or "").strip()
    return {
        "fecha": fecha_utc(post).astimezone(ZONA).strftime("%Y-%m-%d %H:%M"),
        "autor": f"@{u.username}" if u else None,
        "nombre": u.displayname if u else None,
        "seguidores": u.followersCount if u else None,
        "verificado": bool(u and (u.verified or getattr(u, "blue", False))),
        "texto": texto[:largo_texto] + ("…" if len(texto) > largo_texto else ""),
        "likes": post.likeCount,
        "retuits": post.retweetCount,
        "respuestas": post.replyCount,
        "citas": post.quoteCount,
        "vistas": post.viewCount,
        "interacciones": interacciones(post),
        "es_respuesta": bool(post.inReplyToTweetId),
        "url": post.url,
    }


def _texto_horas(horas: float) -> str:
    if horas < 1:
        return f"{round(horas * 60)} min"
    return f"{horas:g} h"


def _cant(n: int, llego_al_tope: bool) -> str:
    """'40+' si la búsqueda llegó al tope (puede haber más), si no el número."""
    return f"{n}+" if llego_al_tope else str(n)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _ritmo(posts: list[Any], fin: datetime, inicio: datetime, llego_al_tope: bool) -> float:
    """Posts por hora. Si la búsqueda llegó al tope, mide desde el post más viejo que se trajo
    (así se puede comparar el volumen aunque haya más posts de los que se bajaron)."""
    if not posts:
        return 0.0
    desde = min(fecha_utc(p) for p in posts) if llego_al_tope else inicio
    horas = max((fin - desde).total_seconds() / 3600, 1 / 60)
    return len(posts) / horas


async def _comparar_ventanas(consulta: str, horas: float, max_posts: int, idioma: str | None) -> dict[str, Any]:
    """Trae la ventana actual y la anterior de una búsqueda y calcula volumen y ritmo."""
    ahora = _ahora()
    ini_actual = ahora - timedelta(hours=horas)
    ini_anterior = ahora - timedelta(hours=2 * horas)
    crudos_act = await traer_posts(armar_consulta(consulta, ini_actual, None, idioma), max_posts)
    crudos_ant = await traer_posts(armar_consulta(consulta, ini_anterior, ini_actual, idioma), max_posts)
    actuales = [p for p in crudos_act if fecha_utc(p) >= ini_actual]
    anteriores = [p for p in crudos_ant if ini_anterior <= fecha_utc(p) < ini_actual]
    sat_act, sat_ant = len(crudos_act) >= max_posts, len(crudos_ant) >= max_posts
    return {
        "actuales": actuales,
        "n_act": len(actuales),
        "n_ant": len(anteriores),
        "sat_act": sat_act,
        "sat_ant": sat_ant,
        "ritmo_act": _ritmo(actuales, ahora, ini_actual, sat_act),
        "ritmo_ant": _ritmo(anteriores, ini_actual, ini_anterior, sat_ant),
    }


def _es_pico(v: dict[str, Any], factor: float) -> bool:
    return v["n_act"] >= MIN_POSTS_PICO and v["ritmo_act"] >= factor * v["ritmo_ant"]


def _resumen_volumen(v: dict[str, Any]) -> dict[str, Any]:
    return {
        "posts_ventana_actual": _cant(v["n_act"], v["sat_act"]),
        "posts_ventana_anterior": _cant(v["n_ant"], v["sat_ant"]),
        "posts_por_hora_actual": round(v["ritmo_act"], 1),
        "posts_por_hora_anterior": round(v["ritmo_ant"], 1),
        "variacion_porcentual": (
            round((v["ritmo_act"] - v["ritmo_ant"]) / v["ritmo_ant"] * 100) if v["ritmo_ant"] else None
        ),
    }


def _texto_pico(v: dict[str, Any], h: str) -> str:
    return (
        f"{_cant(v['n_act'], v['sat_act'])} posts en las últimas {h} "
        f"({v['ritmo_act']:.1f}/hora) contra {_cant(v['n_ant'], v['sat_ant'])} en las {h} anteriores "
        f"({v['ritmo_ant']:.1f}/hora)."
    )


def _relevantes(posts: list[Any], marcados: list[Any], cuantos: int = 8) -> list[dict[str, Any]]:
    ids = {id(p) for p in marcados}
    orden = sorted(posts, key=lambda p: (id(p) in ids, interacciones(p)), reverse=True)
    return [post_a_dict(p, largo_texto=280) for p in orden[:cuantos]]


def _conversacion(post: Any) -> int:
    return (post.replyCount or 0) + (post.quoteCount or 0)


def _es_de(post: Any, usuario: str) -> bool:
    return bool(post.user) and post.user.username.lower() == usuario.lower()


def _breve(post: Any, largo: int = 280, extra: tuple[str, ...] = ()) -> dict[str, Any]:
    d = post_a_dict(post, largo)
    return {k: d[k] for k in ("fecha", "autor", "seguidores", "texto", "likes", "respuestas", *extra, "url")}


def _veces(n: int) -> str:
    return "1 vez" if n == 1 else f"{n} veces"


async def analizar_conversacion(
    post: Any, respuestas_max: int = 15, citas_max: int = 10, largo: int = 280
) -> dict[str, Any]:
    """Lee las respuestas y las citas de un post y estima si se armó un debate.

    nivel_debate suma puntos por: mucha conversación (50+ respuestas y citas: 1 punto; 200+: 2),
    más respuestas y citas que likes (2 puntos; la mitad o más: 1), 3 o más respuestas que abrieron
    su propia discusión (1) y 20 o más citas (1). Alto: 3 puntos o más; medio: 1 o 2; bajo: 0.
    """
    autor = post.user.username if post.user else ""
    hilo = await traer_conversacion(post.id, max(40, respuestas_max * 3)) if post.replyCount else []
    citas = await traer_citas(post.id, citas_max * 2) if post.quoteCount else []

    ajenas = [t for t in hilo if t.id != post.id and not _es_de(t, autor)]
    directas = [t for t in ajenas if t.inReplyToTweetId == post.id]
    anidadas = [t for t in ajenas if t.inReplyToTweetId != post.id]
    del_autor = [
        t for t in hilo
        if t.id != post.id and _es_de(t, autor)
        and getattr(t.inReplyToUser, "username", autor).lower() != autor.lower()
    ]
    discusiones = sorted((t for t in directas if (t.replyCount or 0) >= 3), key=lambda t: t.replyCount, reverse=True)
    grandes = sorted({f"@{t.user.username}" for t in directas + citas if seguidores(t) >= CUENTA_GRANDE})

    conv = _conversacion(post)
    relacion = conv / max(post.likeCount or 0, 1)
    puntos = 2 if conv >= 200 else 1 if conv >= 50 else 0
    senales = [f"{post.replyCount or 0} respuestas y {post.quoteCount or 0} citas, contra {post.likeCount or 0} likes."]
    if conv >= 20 and relacion >= 1:
        puntos += 2
        senales.append("Más gente responde o cita que la que da like: señal típica de polémica.")
    elif conv >= 20 and relacion >= 0.5:
        puntos += 1
        senales.append("Muchas respuestas y citas en relación a los likes.")
    if discusiones:
        puntos += 1 if len(discusiones) >= 3 else 0
        senales.append(
            f"{len(discusiones)} respuesta(s) abrieron su propia discusión entre usuarios (3 o más respuestas cada una)."
        )
    if (post.quoteCount or 0) >= 20:
        puntos += 1
        senales.append(f"Lo citaron {post.quoteCount} veces: la discusión salió del hilo.")
    if grandes:
        senales.append(f"Respondieron o citaron cuentas grandes: {', '.join(grandes[:5])}{'…' if len(grandes) > 5 else ''}.")
    if del_autor:
        senales.append(f"@{autor} les contestó a otros usuarios {_veces(len(del_autor))}.")
    nivel = "alto" if puntos >= 3 else "medio" if puntos >= 1 else "bajo"

    return {
        "post": _breve(post, 500, ("citas", "retuits", "vistas")),
        "nivel_debate": nivel,
        "senales": senales,
        "respuestas_destacadas": [
            _breve(t, largo) for t in sorted(directas, key=lambda t: t.likeCount or 0, reverse=True)[:respuestas_max]
        ],
        "discusiones": [
            {**_breve(t, largo), "contrarespuestas": [_breve(x, largo) for x in anidadas if x.inReplyToTweetId == t.id][:3]}
            for t in discusiones[:5]
        ],
        "citas_destacadas": [_breve(t, largo) for t in sorted(citas, key=interacciones, reverse=True)[:citas_max]],
        "el_autor_responde": [_breve(t, largo) for t in del_autor[:5]],
        "leido": (
            f"Se leyeron {len(directas) + len(anidadas)} de {post.replyCount or 0} respuestas y "
            f"{len(citas)} de {post.quoteCount or 0} citas (X muestra primero las más relevantes)."
        ),
    }


async def _posts_de_cuenta(cuenta: Cuenta, horas: float, limite: int = 40) -> list[Any]:
    """Posts propios de la cuenta en las últimas horas (sin retuits ni respuestas)."""
    desde = _ahora() - timedelta(hours=horas)
    consulta = armar_consulta(f"from:{cuenta.usuario}", desde, None, None, True, incluir_respuestas=False)
    return [p for p in await traer_posts(consulta, limite) if fecha_utc(p) >= desde]


# ---------------------------------------------------------------------------
# Herramientas (tools) que ve Claude
# ---------------------------------------------------------------------------
# El docstring de cada función es la descripción que lee Claude para decidir
# cuándo usarla; los tipos de los parámetros se convierten en el esquema JSON.


@mcp.tool()
async def buscar_en_x(
    consulta: str,
    horas: float = 24,
    max_posts: int = 40,
    orden: Orden = "recientes",
    idioma: str | None = None,
    incluir_respuestas: bool = True,
) -> dict[str, Any]:
    """Busca posts de X (Twitter) publicados en las últimas horas sobre un tema, para ver qué se
    está diciendo. Devuelve cada post con autor, texto, interacciones y enlace, más un resumen.

    Args:
        consulta: el nombre de un tema configurado (ej. "Despidos en el Estado", "Retiro voluntario":
            usa su búsqueda armada) o palabras a buscar. Acepta los operadores de búsqueda de X:
            "frase exacta", inflación OR precios, from:usuario (posts de una cuenta), @usuario
            (menciones), #hashtag, -palabra (excluir), min_faves:100 (con al menos 100 likes).
        horas: cuánto hacia atrás buscar (por defecto 24, máximo 168 = 7 días).
        max_posts: cuántos posts traer como máximo (por defecto 40, tope 200). Cuantos más, más se
            usa la cuenta de X y antes llega al límite de consultas.
        orden: "recientes" (los últimos) o "destacados" (los que X considera más relevantes).
        idioma: código de idioma opcional, ej. "es" o "en".
        incluir_respuestas: False para dejar afuera las respuestas a otros posts.
    """
    if len(consulta.strip()) < 2:
        raise ToolError("Indicá qué buscar (al menos 2 letras).")
    if not 0.25 <= horas <= 168:
        raise ToolError("`horas` tiene que estar entre 0.25 (15 minutos) y 168 (7 días).")
    max_posts = max(1, min(max_posts, MAX_POSTS))

    tema = resolver_tema(consulta)
    hasta = _ahora()
    desde = hasta - timedelta(hours=horas)
    consulta_x = armar_consulta(tema.consulta, desde, None, idioma, True, incluir_respuestas)
    crudos = await traer_posts(consulta_x, max_posts, orden)
    posts = [p for p in crudos if fecha_utc(p) >= desde]
    if orden == "recientes":
        posts.sort(key=fecha_utc, reverse=True)

    autores = Counter(f"@{p.user.username}" for p in posts if p.user)
    hashtags = Counter(f"#{h}" for p in posts for h in (getattr(p, "hashtags", None) or []))
    virales = sorted(posts, key=interacciones, reverse=True)[:5]
    tope = len(crudos) >= max_posts
    return {
        "consulta": consulta,
        "tema_configurado": tema.nombre if tema.consulta != consulta else None,
        "consulta_enviada_a_x": consulta_x,
        "desde": desde.astimezone(ZONA).strftime("%Y-%m-%d %H:%M"),
        "hasta": hasta.astimezone(ZONA).strftime("%Y-%m-%d %H:%M"),
        "total": len(posts),
        "tope_alcanzado": tope,
        "resumen": {
            "interacciones_totales": sum(interacciones(p) for p in posts),
            "autores_distintos": len(autores),
            "cuentas_mas_activas": [{"cuenta": c, "posts": n} for c, n in autores.most_common(5)],
            "hashtags_frecuentes": [{"hashtag": h, "veces": n} for h, n in hashtags.most_common(5)],
            "mas_interaccion": [p.url for p in virales],
        },
        "posts": [post_a_dict(p) for p in posts],
        "nota": (
            f"Se trajeron {max_posts} posts y hay más. Para ver más, subí max_posts o acortá `horas`."
            if tope
            else None if posts else "Sin resultados. Probá con otras palabras, sin comillas o con más horas."
        ),
    }


@mcp.tool()
async def reacciones_a_posts(
    cuenta: str | None = None,
    url_post: str | None = None,
    horas: float = 24,
    max_posts: int = 3,
    respuestas_por_post: int = 15,
    citas_por_post: int = 10,
    incluir_menciones: bool = True,
) -> dict[str, Any]:
    """Qué dice la gente sobre los posts de una cuenta de X (o sobre un post puntual): lee las
    respuestas, las citas (quienes comparten el post con su propia opinión) y las discusiones que se
    arman entre usuarios dentro de las respuestas, y estima si el post desató un debate
    (nivel_debate alto, medio o bajo, con las señales que lo explican). También trae lo que otros
    publicaron sobre esa persona en el período. Sirve para resumir argumentos, críticas y tono.

    Args:
        cuenta: usuario de X ("@fedesturze") o nombre de una cuenta seguida ("Sturzenegger",
            "Fariña"). Analiza los posts del período con más respuestas y citas.
        url_post: en vez de `cuenta`, el enlace de un post puntual (https://x.com/usuario/status/123...).
        horas: período a mirar cuando se usa `cuenta` (por defecto 24, entre 1 y 168).
        max_posts: de cuántos posts leer la conversación (por defecto 3, máximo 5).
        respuestas_por_post: cuántas respuestas mostrar por post (por defecto 15, máximo 30).
        citas_por_post: cuántas citas mostrar por post (por defecto 10, máximo 20).
        incluir_menciones: si es true, suma lo que otros publicaron sobre esa persona en el período.
    """
    if not cuenta and not url_post:
        raise ToolError("Indicá `cuenta` (ej. @fedesturze) o `url_post` (el enlace de un post).")
    if not 1 <= horas <= 168:
        raise ToolError("`horas` tiene que estar entre 1 y 168 (7 días).")
    max_posts = max(1, min(max_posts, 5))
    respuestas_por_post = max(1, min(respuestas_por_post, 30))
    citas_por_post = max(1, min(citas_por_post, 20))

    if url_post:
        m = re.search(r"/status(?:es)?/(\d+)", url_post) or re.fullmatch(r"\s*(\d{5,25})\s*", url_post)
        if not m:
            raise ToolError("No reconozco el enlace. Tiene que ser como https://x.com/usuario/status/123456789.")
        post = await traer_post(int(m[1]))
        c = resolver_cuenta(post.user.username) if post.user else None
        en_periodo, analizar = None, [post]
        otros: list[Any] = []
    else:
        c = resolver_cuenta(cuenta or "")
        en_periodo = await _posts_de_cuenta(c, horas)
        con_charla = [p for p in sorted(en_periodo, key=_conversacion, reverse=True) if _conversacion(p)]
        analizar = con_charla[:max_posts]
        otros = [p for p in en_periodo if p not in analizar]

    conversaciones: list[dict[str, Any]] = []
    notas: list[str] = []
    for p in analizar:
        try:
            conversaciones.append(await analizar_conversacion(p, respuestas_por_post, citas_por_post))
        except ToolError as e:
            notas.append(f"No se pudo leer toda la conversación: {e}")
            break

    menciones = None
    if incluir_menciones and c and not url_post:
        ini = _ahora() - timedelta(hours=horas)
        ms = [p for p in await traer_posts(armar_consulta(c.consulta_menciones(), ini), 40) if fecha_utc(p) >= ini]
        menciones = {
            "busqueda": c.consulta_menciones(),
            "total": _cant(len(ms), len(ms) >= 40),
            "mas_interaccion": [_breve(p) for p in sorted(ms, key=interacciones, reverse=True)[:10]],
        }
    if en_periodo == []:
        notas.append(f"{c.arroba} no publicó posts propios en las últimas {_texto_horas(horas)}.")
    return {
        "cuenta": c.arroba if c else None,
        "nombre": c.nombre if c else None,
        "periodo": None if url_post else _texto_horas(horas),
        "posts_en_el_periodo": None if en_periodo is None else len(en_periodo),
        "conversaciones": conversaciones,
        "otros_posts": [_breve(p, 200, ("citas",)) for p in sorted(otros, key=fecha_utc, reverse=True)],
        "menciones": menciones,
        "nota": " ".join(notas) or None,
    }


async def _revisar_tema(
    tema: Tema, horas: float, min_interacciones: int, min_seguidores: int, factor_pico: float,
    max_posts: int, idioma: str | None,
) -> dict[str, Any]:
    v = await _comparar_ventanas(tema.consulta, horas, max_posts, idioma)
    actuales, h = v["actuales"], _texto_horas(horas)
    tipos: list[str] = []
    motivos: list[str] = []

    if _es_pico(v, factor_pico):
        tipos.append("pico")
        motivos.append("Pico: " + _texto_pico(v, h))
    virales = [p for p in actuales if interacciones(p) >= min_interacciones]
    if virales:
        tipos.append("viral")
        mayor = max(virales, key=interacciones)
        motivos.append(
            f"{len(virales)} post(s) con {min_interacciones} interacciones o más; el mayor tiene "
            f"{interacciones(mayor)} (@{mayor.user.username if mayor.user else '?'})."
        )
    grandes = [p for p in actuales if seguidores(p) >= min_seguidores]
    if grandes:
        tipos.append("cuenta_grande")
        cuentas = sorted({f"@{p.user.username}" for p in grandes})
        motivos.append(
            f"{len(grandes)} post(s) de cuentas con {min_seguidores} seguidores o más: "
            f"{', '.join(cuentas[:5])}{'…' if len(cuentas) > 5 else ''}."
        )
    if tema.avisar_siempre and actuales and not tipos:
        tipos.append("aparicion")
        motivos.append(f"Apareció en {len(actuales)} post(s) en las últimas {h}.")

    return {
        "tema": tema.nombre,
        "busqueda": tema.consulta,
        "alerta": bool(tipos),
        "tipos": tipos,
        "motivos": motivos,
        **_resumen_volumen(v),
        "posts_relevantes": _relevantes(actuales, virales + grandes),
    }


async def _revisar_cuenta(
    cuenta: Cuenta, horas: float, horas_debate: float, avisar_cada_post: bool, factor_pico: float,
    min_interacciones_menciones: int, max_posts: int, idioma: str | None,
) -> dict[str, Any]:
    recientes = await _posts_de_cuenta(cuenta, max(horas, horas_debate))
    ini = _ahora() - timedelta(hours=horas)
    nuevos = [p for p in recientes if fecha_utc(p) >= ini]
    candidatos = [p for p in sorted(recientes, key=_conversacion, reverse=True) if _conversacion(p) >= MIN_CONVERSACION]
    conversaciones: list[dict[str, Any]] = []
    nota = None
    for p in candidatos[:3]:
        try:
            conversaciones.append(await analizar_conversacion(p, respuestas_max=8, citas_max=5, largo=200))
        except ToolError as e:
            nota = f"No se pudo leer toda la conversación: {e}"
            break
    v = await _comparar_ventanas(cuenta.consulta_menciones(), horas, max_posts, idioma)
    h = _texto_horas(horas)
    tipos: list[str] = []
    motivos: list[str] = []

    if nuevos and avisar_cada_post:
        tipos.append("publico")
        motivos.append(f"{cuenta.arroba} publicó {len(nuevos)} post(s) en las últimas {h}.")
    debates = [c for c in conversaciones if c["nivel_debate"] == "alto"]
    if debates:
        tipos.append("debate")
        for c in debates:
            inicio = c["post"]["texto"][:80] + ("…" if len(c["post"]["texto"]) > 80 else "")
            motivos.append(f"Debate en su post del {c['post']['fecha']} («{inicio}»): " + " ".join(c["senales"]))
    if _es_pico(v, factor_pico):
        tipos.append("pico_menciones")
        motivos.append("Pico de menciones: " + _texto_pico(v, h))
    virales = [p for p in v["actuales"] if interacciones(p) >= min_interacciones_menciones]
    if virales:
        tipos.append("mencion_viral")
        mayor = max(virales, key=interacciones)
        motivos.append(
            f"{len(virales)} post(s) de otros sobre {cuenta.nombre} con {min_interacciones_menciones} "
            f"interacciones o más; el mayor tiene {interacciones(mayor)} "
            f"(@{mayor.user.username if mayor.user else '?'})."
        )

    return {
        "cuenta": cuenta.arroba,
        "nombre": cuenta.nombre,
        "alerta": bool(tipos),
        "tipos": tipos,
        "motivos": motivos,
        "posts_nuevos": [_breve(p, 280, ("citas",)) for p in sorted(nuevos, key=fecha_utc, reverse=True)],
        "conversaciones": conversaciones,
        "menciones": {**_resumen_volumen(v), "posts_relevantes": _relevantes(v["actuales"], virales)},
        "nota": nota,
    }


@mcp.tool()
async def revisar_alertas(
    incluir: Incluir = "todo",
    temas: list[str] | None = None,
    cuentas: list[str] | None = None,
    horas: float = 6,
    min_interacciones: int = 100,
    min_seguidores: int = 50000,
    factor_pico: float = 2.0,
    horas_debate: float = 24,
    min_interacciones_menciones: int = 300,
    avisar_cada_post: bool = True,
    max_posts_por_busqueda: int = 40,
    idioma: str | None = None,
) -> dict[str, Any]:
    """Revisa en X los temas y las cuentas configurados y levanta alertas. Compara las últimas
    `horas` con las `horas` anteriores.

    Para cada TEMA marca alerta si:
      - pico: el ritmo de posts por hora se multiplicó por `factor_pico` o más (y hay al menos 10);
      - viral: algún post superó `min_interacciones` (likes + retuits + respuestas + citas);
      - cuenta_grande: publicó alguien con `min_seguidores` o más;
      - aparicion: el tema está marcado para avisar siempre que aparezca (ej. un nombre propio).
    Para cada CUENTA seguida marca alerta si:
      - publico: publicó posts nuevos (se puede apagar con avisar_cada_post=False);
      - debate: alguno de sus posts de las últimas `horas_debate` desató un debate (más respuestas y
        citas que likes, discusiones entre usuarios, muchas citas). Incluye las respuestas, las
        discusiones y las citas para resumir qué dice la gente;
      - pico_menciones / mencion_viral: subieron mucho las menciones de otros, o una superó
        `min_interacciones_menciones`.
    Devuelve los posts relevantes para resumir qué se está diciendo. Pensada para correr sola cada
    algunas horas (tarea programada) y avisar solo si hay_alertas es true.

    Args:
        incluir: "todo", "temas" o "cuentas" (para partir la revisión en dos si tarda mucho).
        temas: nombres de temas configurados o búsquedas propias. Si se omite, todos los configurados.
        cuentas: usuarios de X o nombres configurados. Si se omite, todas las configuradas.
        horas: tamaño de cada ventana (por defecto 6; entre 1 y 72).
        min_interacciones: umbral de post viral en los temas (por defecto 100).
        min_seguidores: umbral de cuenta grande en los temas (por defecto 50000).
        factor_pico: cuántas veces tiene que crecer el ritmo de posts (por defecto 2).
        horas_debate: de qué período mirar los posts de las cuentas para detectar debates (por
            defecto 24: un debate puede armarse horas después del post).
        min_interacciones_menciones: umbral de mención viral para las cuentas (por defecto 300).
        avisar_cada_post: si es true, cada post nuevo de una cuenta seguida es una alerta.
        max_posts_por_busqueda: tope de posts por ventana (por defecto 40, máximo 100).
        idioma: código de idioma opcional para los temas, ej. "es".
    """
    if not 1 <= horas <= 72:
        raise ToolError("`horas` tiene que estar entre 1 y 72.")
    if factor_pico <= 1:
        raise ToolError("`factor_pico` tiene que ser mayor que 1.")
    if not 1 <= horas_debate <= 72:
        raise ToolError("`horas_debate` tiene que estar entre 1 y 72.")
    lista_temas = ([resolver_tema(t) for t in temas if t.strip()] if temas else temas_configurados()) \
        if incluir in ("todo", "temas") else []
    lista_cuentas = ([resolver_cuenta(c) for c in cuentas if c.strip()] if cuentas else cuentas_configuradas()) \
        if incluir in ("todo", "cuentas") else []
    if not lista_temas and not lista_cuentas:
        raise ToolError("No hay temas ni cuentas para revisar. Revisá las variables TEMAS y CUENTAS.")
    if len(lista_temas) + len(lista_cuentas) > MAX_ITEMS_ALERTA:
        raise ToolError(
            f"Máximo {MAX_ITEMS_ALERTA} temas y cuentas por revisión, para no agotar la cuenta de X. "
            "Usá incluir='temas' e incluir='cuentas' por separado."
        )
    max_posts = max(5, min(max_posts_por_busqueda, MAX_POSTS_ALERTA))

    res_cuentas: list[dict[str, Any]] = []
    res_temas: list[dict[str, Any]] = []
    sin_revisar: list[str] = []
    problema: str | None = None
    # De a uno: la cuenta de X no puede hacer dos búsquedas a la vez. Primero las cuentas.
    trabajos = [("cuenta", c) for c in lista_cuentas] + [("tema", t) for t in lista_temas]
    for tipo, item in trabajos:
        etiqueta = item.arroba if tipo == "cuenta" else item.nombre
        if problema:
            sin_revisar.append(etiqueta)
            continue
        try:
            if tipo == "cuenta":
                res_cuentas.append(await _revisar_cuenta(
                    item, horas, horas_debate, avisar_cada_post, factor_pico,
                    min_interacciones_menciones, max_posts, idioma))
            else:
                res_temas.append(await _revisar_tema(
                    item, horas, min_interacciones, min_seguidores, factor_pico, max_posts, idioma))
        except ToolError as e:
            problema = str(e)
            sin_revisar.append(etiqueta)

    if not res_cuentas and not res_temas and problema:
        raise ToolError(problema)

    con_alerta = [(r.get("cuenta") or r["tema"], r["tipos"]) for r in res_cuentas + res_temas if r["alerta"]]
    revisados = len(res_cuentas) + len(res_temas)
    if con_alerta:
        detalle = "; ".join(f"{nombre} ({', '.join(tipos)})" for nombre, tipos in con_alerta)
        resumen = f"{len(con_alerta)} de {revisados} con alertas: {detalle}."
    else:
        resumen = f"Sin alertas en {revisados} temas y cuentas."
    return {
        "revisado": _ahora().astimezone(ZONA).strftime("%Y-%m-%d %H:%M"),
        "ventana": _texto_horas(horas),
        "hay_alertas": bool(con_alerta),
        "resumen": resumen,
        "cuentas": res_cuentas,
        "temas": res_temas,
        "sin_revisar": sin_revisar or None,
        "nota": f"No se pudo revisar todo: {problema}" if problema else None,
        "criterios": {
            "factor_pico": factor_pico,
            "min_posts_pico": MIN_POSTS_PICO,
            "min_interacciones": min_interacciones,
            "min_seguidores": min_seguidores,
            "horas_debate": horas_debate,
            "min_interacciones_menciones": min_interacciones_menciones,
            "max_posts_por_busqueda": max_posts,
        },
    }


@mcp.tool()
async def estado_del_scraper() -> dict[str, Any]:
    """Dice si la cuenta de X que usa el servidor está activa, si está en pausa por el límite de
    consultas de X, y qué temas y cuentas tiene configurados. Usala cuando una búsqueda falla."""
    api = await cliente_x()
    info = await api.pool.accounts_info()
    minutos = _minutos_hasta(await api.pool.next_available_at(COLA_BUSQUEDA))
    cuentas = [
        {
            "cuenta": a.get("username"),
            "activa": a.get("active"),
            "pedidos_hechos": a.get("total_req"),
            "ultimo_uso": a["last_used"].astimezone(ZONA).strftime("%Y-%m-%d %H:%M") if a.get("last_used") else None,
            "error": a.get("error_msg") if a.get("error_msg") not in (None, "None") else None,
        }
        for a in info
    ]
    activa = any(c["activa"] for c in cuentas)
    if not activa:
        diagnostico = await _explicar_sin_cuenta()
    elif minutos:
        problema = diagnosticar(time.monotonic() - 20 * 60)
        diagnostico = f"En pausa por unos {minutos} minutos. " + (
            problema or "Llegó al límite de consultas de X; se libera sola."
        )
    else:
        diagnostico = "Activa. Si nunca buscó todavía, probá con buscar_en_x para confirmar que X la acepta."
        if any(c["pedidos_hechos"] for c in cuentas):
            diagnostico = "Activa y lista para buscar."
    return {
        "diagnostico": diagnostico,
        "cuentas_de_scraping": cuentas,
        "ultimos_avisos_de_x": [m[:200] for _, m in list(_avisos)[-5:]] or None,
        "temas_configurados": [
            {"tema": t.nombre, "busqueda": t.consulta, "avisar_siempre": t.avisar_siempre}
            for t in temas_configurados()
        ],
        "cuentas_seguidas": [
            {"cuenta": c.arroba, "nombre": c.nombre, "busqueda_menciones": c.consulta_menciones()}
            for c in cuentas_configuradas()
        ],
    }


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------


def app_http() -> Any:
    """App HTTP con el MCP en /<CLAVE_ACCESO>/mcp. Sin la clave, nadie más puede usar tu cuenta."""
    clave = os.environ.get("CLAVE_ACCESO", "").strip()
    if len(clave) < 16 or not re.fullmatch(r"[A-Za-z0-9_-]+", clave):
        sys.exit(
            "Falta CLAVE_ACCESO o es muy corta: tiene que tener al menos 16 caracteres "
            "(solo letras, números, guion y guion bajo)."
        )
    return mcp.streamable_http_app(streamable_http_path=f"/{clave}/mcp", stateless_http=True, host="0.0.0.0")


def main() -> None:
    if "--http" in sys.argv:
        import uvicorn

        # access_log=False: así la clave de la URL no queda escrita en los logs.
        uvicorn.run(
            app_http(),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8000")),
            access_log=False,
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
