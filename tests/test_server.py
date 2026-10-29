"""Pruebas sin internet: reemplazan twscrape por una API falsa.

    uv run --with pytest pytest
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient
from twscrape import NoAccountError

import server

AHORA = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# API falsa
# ---------------------------------------------------------------------------


def post(n: int, minutos_atras: float, likes=0, rts=0, seguidores=100, usuario=None, texto=None,
         respuestas=1, citas=0, responde_a=None, responde_a_usuario=None, cita_a=None):
    u = SimpleNamespace(
        username=usuario or f"user{n % 7}",
        displayname=f"Usuario {n}",
        followersCount=seguidores,
        verified=False,
        blue=seguidores > 10000,
    )
    return SimpleNamespace(
        id=n,
        url=f"https://x.com/{u.username}/status/{n}",
        date=AHORA - timedelta(minutes=minutos_atras),
        user=u,
        rawContent=texto or f"post número {n} sobre   el tema\ncon salto",
        likeCount=likes,
        retweetCount=rts,
        replyCount=respuestas,
        quoteCount=citas,
        viewCount=1000,
        hashtags=["inflacion"] if n % 2 else [],
        inReplyToTweetId=responde_a,
        inReplyToUser=SimpleNamespace(username=responde_a_usuario) if responde_a_usuario else None,
        quotedTweet=SimpleNamespace(id=cita_a) if cita_a else None,
    )


class PoolFalso:
    def __init__(self):
        self.activa = True
        self.liberacion = None
        self.agregada = None
        self.colas: list[str] = []

    async def add_account_cookies(self, usuario, cookies):
        if "auth_token" not in cookies or "ct0" not in cookies:
            raise ValueError("Cookies must include auth_token and ct0")
        self.agregada = (usuario, cookies)

    async def reset_locks(self):
        self.reseteada = True

    async def accounts_info(self):
        return [{"username": "secundaria", "active": self.activa, "total_req": 7,
                 "last_used": AHORA, "error_msg": None if self.activa else "403 - Forbidden"}]

    async def next_available_at(self, queue):
        self.colas.append(queue)
        return self.liberacion


class APIFalsa:
    """`ventanas(q, desde, hasta)` decide qué posts devuelve cada búsqueda."""

    def __init__(self):
        self.pool = PoolFalso()
        self.consultas: list[tuple[str, int, dict]] = []
        self.ventanas = lambda q, desde, hasta: []
        self.hilos: dict[int, list] = {}
        self.pedidos_hilos: list[int] = []
        self.posts: dict[int, Any] = {}
        self.error_hilos = None
        self.error = None
        self.aviso = None  # simula un aviso que twscrape escribe en el log

    async def search(self, q, limit=-1, kv=None):
        self.consultas.append((q, limit, kv))
        if self.aviso:
            server.log_twscrape.warning(self.aviso)
        if self.error:
            raise self.error
        m = re.search(r"since_time:(\d+)", q)
        desde = datetime.fromtimestamp(int(m[1]), timezone.utc) if m else None
        m = re.search(r"until_time:(\d+)", q)
        hasta = datetime.fromtimestamp(int(m[1]), timezone.utc) if m else None
        for p in self.ventanas(q, desde, hasta)[: limit if limit > 0 else None]:
            yield p

    async def tweet_thread(self, twid, limit=-1, kv=None):
        self.pedidos_hilos.append(twid)
        if self.error_hilos:
            raise self.error_hilos
        for r in self.hilos.get(twid, [])[: limit if limit > 0 else None]:
            yield r

    async def tweet_details(self, twid, kv=None):
        return self.posts.get(twid)


@pytest.fixture
def api(monkeypatch):
    falsa = APIFalsa()
    monkeypatch.setattr(server, "_api", falsa)
    monkeypatch.setattr(server, "_preparada", True)
    monkeypatch.setattr(server, "_ahora", lambda: AHORA)
    monkeypatch.delenv("TEMAS", raising=False)
    monkeypatch.delenv("CUENTAS", raising=False)
    server._avisos.clear()
    return falsa


# ---------------------------------------------------------------------------
# Consultas y configuración
# ---------------------------------------------------------------------------


def test_armar_consulta():
    desde = AHORA - timedelta(hours=6)
    q = server.armar_consulta('"dólar blue"', desde, AHORA, idioma="ES", incluir_respuestas=False)
    assert q.startswith('"dólar blue" since_time:')
    assert f"since_time:{int(desde.timestamp())}" in q
    assert f"until_time:{int(AHORA.timestamp())}" in q
    assert "lang:es" in q and "-filter:retweets" in q and "-filter:replies" in q
    assert "-filter:retweets" not in server.armar_consulta("ANMAT filter:retweets", desde)


@pytest.mark.parametrize("consulta, envuelve", [
    ('"empleo público" OR "empleo publico"', True),
    ("inflación OR precios", True),
    ("(a) OR b", True),
    ("ATE (paro OR gremio)", False),
    ("(despidos OR cesantías) (estatales OR Estado)", False),
    ('"esto OR aquello"', False),
    ("UPCN", False),
])
def test_or_suelto_se_envuelve(consulta, envuelve):
    q = server.armar_consulta(consulta, AHORA)
    assert q.startswith(f"({consulta}) ") is envuelve


def test_temas_y_cuentas_por_defecto(monkeypatch):
    monkeypatch.delenv("TEMAS", raising=False)
    monkeypatch.delenv("CUENTAS", raising=False)
    temas = server.temas_configurados()
    assert [t.nombre for t in temas] == [
        "Empleo público", "Despidos en el Estado", "Conflictos gremiales", "Retiro voluntario",
        "ATE", "UPCN", "Rosana Reggi"]
    assert [t.nombre for t in temas if t.avisar_siempre] == ["Rosana Reggi"]
    cuentas = server.cuentas_configuradas()
    assert [c.arroba for c in cuentas] == ["@fedesturze", "@MaxiFarina"]
    assert cuentas[0].consulta_menciones() == "(@fedesturze OR Sturzenegger OR Sturze) -from:fedesturze"
    assert "Rosana Reggi" in server._instrucciones() and "@MaxiFarina" in server._instrucciones()


def test_temas_y_cuentas_desde_variables(monkeypatch):
    monkeypatch.setenv("TEMAS", 'Paritarias | paritaria (estatales OR UPCN) ; Mi nombre | "Ana Pérez" | siempre\nANMAT')
    monkeypatch.setenv("CUENTAS", "@JMilei | Javier Milei | Milei; otra_cuenta")
    temas = server.temas_configurados()
    assert [(t.nombre, t.consulta, t.avisar_siempre) for t in temas] == [
        ("Paritarias", "paritaria (estatales OR UPCN)", False),
        ("Mi nombre", '"Ana Pérez"', True),
        ("ANMAT", "ANMAT", False)]
    cuentas = server.cuentas_configuradas()
    assert [(c.usuario, c.nombre, c.menciones) for c in cuentas] == [
        ("JMilei", "Javier Milei", "Milei"), ("otra_cuenta", "@otra_cuenta", "")]
    monkeypatch.setenv("CUENTAS", "no es un usuario!")
    with pytest.raises(ToolError, match="no parece un usuario"):
        server.cuentas_configuradas()


def test_resolver_temas_y_cuentas(monkeypatch):
    monkeypatch.delenv("TEMAS", raising=False)
    monkeypatch.delenv("CUENTAS", raising=False)
    assert server.resolver_tema("despidos en el estado").nombre == "Despidos en el Estado"
    assert server.resolver_tema("EMPLEO PUBLICO").consulta.startswith('"empleo público" OR')
    assert server.resolver_tema("algo libre").consulta == "algo libre"
    assert server.resolver_cuenta("Sturzenegger").usuario == "fedesturze"
    assert server.resolver_cuenta("fariña").usuario == "MaxiFarina"
    assert server.resolver_cuenta("@FEDESTURZE").nombre == "Federico Sturzenegger"
    otra = server.resolver_cuenta("@otra_cuenta")
    assert (otra.usuario, otra.consulta_menciones()) == ("otra_cuenta", "(@otra_cuenta) -from:otra_cuenta")


# ---------------------------------------------------------------------------
# buscar_en_x
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_buscar_en_x(api):
    api.ventanas = lambda q, d, h: [post(1, 300, likes=5), post(2, 10, likes=500, rts=40), post(3, 60 * 30)]
    r = await server.buscar_en_x("inflación", horas=24, max_posts=10, idioma="es")
    q, limite, kv = api.consultas[0]
    assert "lang:es" in q and "-filter:retweets" in q and kv == {"product": "Latest"} and limite == 10
    assert r["total"] == 2  # el post de hace 30 h queda afuera
    assert r["tema_configurado"] is None
    assert [p["url"] for p in r["posts"]] == ["https://x.com/user2/status/2", "https://x.com/user1/status/1"]
    primero = r["posts"][0]
    assert primero["fecha"] == "2026-10-29 14:50"  # hora de Buenos Aires
    assert primero["texto"] == "post número 2 sobre el tema con salto"
    assert primero["interacciones"] == 541
    assert r["tope_alcanzado"] is False and r["nota"] is None


@pytest.mark.anyio
async def test_buscar_un_tema_configurado_usa_su_busqueda(api):
    r = await server.buscar_en_x("retiro voluntario")
    assert r["tema_configurado"] == "Retiro voluntario"
    assert api.consultas[0][0].startswith('("retiro voluntario" OR "retiros voluntarios") since_time:')


@pytest.mark.anyio
async def test_buscar_destacados_y_tope(api):
    api.ventanas = lambda q, d, h: [post(i, i) for i in range(50)]
    r = await server.buscar_en_x("ANMAT", max_posts=5, orden="destacados")
    assert api.consultas[0][2] == {"product": "Top"}
    assert r["total"] == 5 and r["tope_alcanzado"] is True and "hay más" in r["nota"]


@pytest.mark.anyio
async def test_buscar_valida_parametros(api):
    with pytest.raises(ToolError):
        await server.buscar_en_x("x")
    with pytest.raises(ToolError):
        await server.buscar_en_x("inflación", horas=500)


# ---------------------------------------------------------------------------
# revisar_alertas: temas
# ---------------------------------------------------------------------------


def ventanas_temas(q, desde, hasta):
    actual = hasta is None
    if "cesantías" in q:  # solo la búsqueda de "Despidos en el Estado": pico + viral + cuenta grande
        if actual:
            return ([post(i, 5 + i) for i in range(28)]
                    + [post(100, 20, likes=900, rts=50), post(101, 40, seguidores=2_000_000, usuario="diario")])
        return [post(200 + i, 400 + i) for i in range(5)]
    if "Rosana Reggi" in q:  # aparece una vez: alerta por "avisar siempre"
        return [post(300, 30, usuario="colega")] if actual else []
    # el resto: volumen parejo y nada llamativo
    return [post(400 + i, (10 if actual else 400) + i * 20) for i in range(6)]


@pytest.mark.anyio
async def test_alertas_de_temas(api):
    api.ventanas = ventanas_temas
    r = await server.revisar_alertas(incluir="temas", horas=6, max_posts_por_busqueda=50)
    assert r["cuentas"] == [] and len(r["temas"]) == 7
    por_tema = {t["tema"]: t for t in r["temas"]}
    desp = por_tema["Despidos en el Estado"]
    assert desp["tipos"] == ["pico", "viral", "cuenta_grande"]
    assert desp["posts_ventana_actual"] == "30" and desp["posts_ventana_anterior"] == "5"
    assert desp["posts_por_hora_actual"] == 5.0 and desp["variacion_porcentual"] == 500
    assert "@diario" in desp["motivos"][2]
    assert {p["autor"] for p in desp["posts_relevantes"][:2]} == {"@user2", "@diario"}
    assert por_tema["Rosana Reggi"]["tipos"] == ["aparicion"]
    assert por_tema["UPCN"]["alerta"] is False
    assert r["hay_alertas"] is True
    assert r["resumen"] == "2 de 7 con alertas: Despidos en el Estado (pico, viral, cuenta_grande); Rosana Reggi (aparicion)."
    assert len(api.consultas) == 14  # 2 ventanas por tema, de a una


@pytest.mark.anyio
async def test_pico_aunque_las_dos_ventanas_lleguen_al_tope(api):
    # 30 posts en la última hora contra 30 en 7 horas: se compara el ritmo, no la cantidad
    api.ventanas = lambda q, d, h: (
        [post(i, 2 * i) for i in range(30)] if h is None else [post(100 + i, 361 + 15 * i) for i in range(30)]
    )
    r = await server.revisar_alertas(incluir="temas", temas=["dólar"], max_posts_por_busqueda=20)
    t = r["temas"][0]
    assert t["posts_ventana_actual"] == "20+" and t["posts_ventana_anterior"] == "20+"
    assert t["posts_por_hora_actual"] > 4 * t["posts_por_hora_anterior"]
    assert t["tipos"] == ["pico"]


@pytest.mark.anyio
async def test_sin_pico_si_el_ritmo_es_parejo(api):
    api.ventanas = lambda q, d, h: [post(i, (0 if h is None else 360) + 15 * i) for i in range(30)]
    r = await server.revisar_alertas(incluir="temas", temas=["dólar"], max_posts_por_busqueda=20)
    assert r["temas"][0]["tipos"] == [] and r["hay_alertas"] is False


# ---------------------------------------------------------------------------
# Cuentas: qué responde la gente y si se arma un debate
# ---------------------------------------------------------------------------

POLEMICO = dict(likes=150, respuestas=220, citas=40)  # más respuestas y citas que likes


def posts_sturze():
    return [
        post(1, 30, usuario="fedesturze", texto="Cerramos 50 organismos más del Estado.", **POLEMICO),
        post(2, 120, usuario="fedesturze", likes=300, respuestas=12, citas=1),   # tranquilo
        post(3, 60 * 20, usuario="fedesturze", likes=50, respuestas=5),        # viejo y con poca charla
    ]


def hilo_post_1():
    return [
        post(1, 30, usuario="fedesturze", **POLEMICO),  # el propio post también viene en el hilo
        post(1001, 25, usuario="critico1", likes=80, respuestas=5, responde_a=1, texto="Eso son despidos"),
        post(2001, 20, usuario="defensor", likes=9, respuestas=0, responde_a=1001, texto="Era un curro"),
        post(2002, 19, usuario="critico1", likes=7, respuestas=0, responde_a=1001, texto="Eran trabajadores"),
        post(1002, 24, usuario="periodista", seguidores=200_000, likes=60, respuestas=4, responde_a=1),
        post(1003, 23, usuario="usuario3", likes=10, respuestas=3, responde_a=1),
        post(1004, 22, usuario="usuario4", likes=5, respuestas=0, responde_a=1),
        post(3001, 18, usuario="fedesturze", likes=40, respuestas=2, responde_a=1001,
             responde_a_usuario="critico1", texto="No, eran áreas duplicadas"),
        post(3002, 29, usuario="fedesturze", likes=30, respuestas=0, responde_a=1,
             responde_a_usuario="fedesturze", texto="(sigue el hilo)"),
    ]


def ventanas_cuentas(q, desde, hasta):
    actual = hasta is None
    if q.startswith("from:fedesturze"):
        return [p for p in posts_sturze() if p.date >= desde]
    if q.startswith("quoted_tweet_id:1"):
        return [post(4001, 15, usuario="diputado", seguidores=80_000, likes=500, cita_a=1, texto="Vergonzoso"),
                post(4002, 10, usuario="otro", likes=20, cita_a=1),
                post(4003, 10, usuario="ruido", likes=999, cita_a=999)]  # cita a otro post: se descarta
    if q.startswith("url:2"):  # para el post 2 el operador de citas no anda: se encuentra por el enlace
        return [post(4100, 60, usuario="fan", likes=3, cita_a=2)]
    if "@fedesturze" in q:  # menciones: pico y una viral
        if actual:
            return [post(500 + i, 5 + 10 * i) for i in range(24)] + [post(600, 15, likes=400, usuario="critico")]
        return [post(700 + i, 400 + 30 * i) for i in range(5)]
    return []  # Fariña: no publicó y nadie lo menciona


@pytest.fixture
def api_cuentas(api):
    api.ventanas = ventanas_cuentas
    api.hilos = {1: hilo_post_1(), 2: [post(1100, 100, likes=2, responde_a=2), post(1101, 90, likes=1, responde_a=2)]}
    api.posts = {1: posts_sturze()[0]}  # para buscar un post por su enlace
    return api


@pytest.mark.anyio
async def test_alertas_de_cuentas_detectan_debate(api_cuentas):
    r = await server.revisar_alertas(incluir="cuentas")
    assert r["temas"] == []
    sturze, farina = r["cuentas"]
    assert sturze["tipos"] == ["publico", "debate", "pico_menciones", "mencion_viral"]
    assert len(sturze["posts_nuevos"]) == 2  # el de hace 20 h no es nuevo
    # se leen las conversaciones de los posts con 10+ respuestas y citas (el 3 tiene 5: no)
    assert api_cuentas.pedidos_hilos == [1, 2]
    alto, bajo = sturze["conversaciones"]
    assert (alto["nivel_debate"], bajo["nivel_debate"]) == ("alto", "bajo")
    assert any("Más gente responde o cita que la que da like" in s for s in alto["senales"])
    assert any("3 respuesta(s) abrieron su propia discusión" in s for s in alto["senales"])
    assert any("@diputado, @periodista" in s for s in alto["senales"])
    assert any("@fedesturze les contestó a otros usuarios 1 vez" in s for s in alto["senales"])
    assert [c["autor"] for c in bajo["citas_destacadas"]] == ["@fan"]  # encontrada con el plan B
    debate = next(m for m in sturze["motivos"] if m.startswith("Debate"))
    assert "«Cerramos 50 organismos más del Estado.»" in debate
    assert farina["alerta"] is False and farina["conversaciones"] == []


@pytest.mark.anyio
async def test_conversacion_separa_respuestas_discusiones_y_citas(api_cuentas):
    r = await server.reacciones_a_posts(url_post="https://x.com/fedesturze/status/1?s=20")
    assert r["cuenta"] == "@fedesturze" and r["nombre"] == "Federico Sturzenegger"
    assert r["periodo"] is None and r["menciones"] is None
    (c,) = r["conversaciones"]
    assert c["post"]["texto"] == "Cerramos 50 organismos más del Estado."
    # respuestas directas de otros, por likes (sin las del autor ni las respuestas a respuestas)
    assert [x["autor"] for x in c["respuestas_destacadas"]] == ["@critico1", "@periodista", "@usuario3", "@usuario4"]
    # discusiones: respuestas con 3+ respuestas, con el ida y vuelta entre usuarios
    assert [d["autor"] for d in c["discusiones"]] == ["@critico1", "@periodista", "@usuario3"]
    assert [x["texto"] for x in c["discusiones"][0]["contrarespuestas"]] == ["Era un curro", "Eran trabajadores"]
    # citas al post, ordenadas por interacción; la que cita a otro post se descarta
    assert [x["autor"] for x in c["citas_destacadas"]] == ["@diputado", "@otro"]
    # el autor contestando a otros (no su propio hilo)
    assert [x["texto"] for x in c["el_autor_responde"]] == ["No, eran áreas duplicadas"]
    assert c["leido"].startswith("Se leyeron 6 de 220 respuestas y 2 de 40 citas")
    assert api_cuentas.consultas[0][0] == "quoted_tweet_id:1" and api_cuentas.consultas[0][2] == {"product": "Top"}


@pytest.mark.anyio
async def test_reacciones_por_cuenta(api_cuentas):
    r = await server.reacciones_a_posts(cuenta="Sturzenegger", horas=24, max_posts=2)
    assert r["cuenta"] == "@fedesturze" and r["posts_en_el_periodo"] == 3
    assert [c["post"]["url"].rsplit("/", 1)[1] for c in r["conversaciones"]] == ["1", "2"]
    assert [p["url"].rsplit("/", 1)[1] for p in r["otros_posts"]] == ["3"]
    assert r["menciones"]["mas_interaccion"][0]["autor"] == "@critico"
    assert r["nota"] is None


@pytest.mark.anyio
async def test_reacciones_cuenta_sin_posts(api_cuentas):
    r = await server.reacciones_a_posts(cuenta="@MaxiFarina")
    assert r["posts_en_el_periodo"] == 0 and r["conversaciones"] == [] and "no publicó" in r["nota"]


@pytest.mark.anyio
async def test_reacciones_valida_y_post_inexistente(api_cuentas):
    with pytest.raises(ToolError, match="Indicá `cuenta`"):
        await server.reacciones_a_posts()
    with pytest.raises(ToolError, match="No reconozco el enlace"):
        await server.reacciones_a_posts(url_post="https://x.com/fedesturze")
    with pytest.raises(ToolError, match="No encontré ese post"):
        await server.reacciones_a_posts(url_post="https://x.com/alguien/status/123456")


@pytest.mark.anyio
async def test_si_x_corta_las_conversaciones_devuelve_lo_que_tiene(api_cuentas):
    api_cuentas.error_hilos = NoAccountError("no account")
    r = await server.reacciones_a_posts(cuenta="Sturzenegger")
    assert r["conversaciones"] == [] and "No se pudo leer toda la conversación" in r["nota"]
    assert r["menciones"] is not None


@pytest.mark.anyio
async def test_avisar_cada_post_se_puede_apagar(api):
    api.ventanas = lambda q, d, h: (
        [post(1, 30, likes=99, usuario="fedesturze", respuestas=0)] if q.startswith("from:") else [])
    r = await server.revisar_alertas(incluir="cuentas", cuentas=["Sturzenegger"], avisar_cada_post=False)
    assert r["cuentas"][0]["alerta"] is False and len(r["cuentas"][0]["posts_nuevos"]) == 1
    assert r["cuentas"][0]["conversaciones"] == []


# ---------------------------------------------------------------------------
# Revisión completa, cortes y errores
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_revision_completa_va_de_a_una_y_cuentas_primero(api):
    api.ventanas = lambda q, d, h: []
    r = await server.revisar_alertas()
    assert len(r["cuentas"]) == 2 and len(r["temas"]) == 7
    assert api.consultas[0][0].startswith("from:fedesturze")
    assert r["hay_alertas"] is False and r["resumen"] == "Sin alertas en 9 temas y cuentas."


@pytest.mark.anyio
async def test_revisar_alertas_resultado_parcial_si_se_corta(api):
    llamadas = {"n": 0}

    def ventanas(q, d, h):
        llamadas["n"] += 1
        if llamadas["n"] > 2:
            raise NoAccountError("sin cuenta")
        return []

    api.ventanas = ventanas
    r = await server.revisar_alertas(incluir="temas", temas=["uno", "dos", "tres"])
    assert [t["tema"] for t in r["temas"]] == ["uno"]
    assert r["sin_revisar"] == ["dos", "tres"] and "límite de consultas" in r["nota"]


@pytest.mark.anyio
async def test_revisar_alertas_valida(api):
    with pytest.raises(ToolError, match="Máximo"):
        await server.revisar_alertas(incluir="temas", temas=[f"t{i}" for i in range(13)])
    with pytest.raises(ToolError, match="entre 1 y 72"):
        await server.revisar_alertas(horas=100)


@pytest.mark.anyio
async def test_cookies_vencidas(api):
    api.error = NoAccountError("no account")
    api.pool.activa = False
    with pytest.raises(ToolError, match="cookies vencieron"):
        await server.buscar_en_x("inflación")


@pytest.mark.anyio
async def test_sesion_cerrada_aunque_la_cuenta_figure_activa(api):
    api.aviso = "XClIdAccountError: Logged-out X web app; username=prueba; queue=SearchTimeline"
    api.error = NoAccountError("no account")
    api.pool.liberacion = "23:59:59"
    with pytest.raises(ToolError, match="no reconoce la sesión"):
        await server.buscar_en_x("inflación")
    r = await server.estado_del_scraper()
    assert "no reconoce la sesión" in r["diagnostico"] and "Logged-out" in r["ultimos_avisos_de_x"][0]


@pytest.mark.anyio
async def test_bloqueo_no_se_disfraza_de_cero_resultados(api):
    api.aviso = "Blocked by Cloudflare: 403 - abc"
    with pytest.raises(ToolError, match="TWS_HTTP_BACKEND=curl"):
        await server.buscar_en_x("inflación")


@pytest.mark.anyio
async def test_cero_resultados_sin_problema_es_valido(api):
    r = await server.buscar_en_x("algo que nadie dice")
    assert r["total"] == 0 and "Sin resultados" in r["nota"]


@pytest.mark.anyio
async def test_limite_de_x(api):
    api.error = NoAccountError("no account")
    api.pool.liberacion = (datetime.now() + timedelta(minutes=12, seconds=30)).strftime("%H:%M:%S")
    with pytest.raises(ToolError, match=r"límite de consultas.*unos 1[23] minutos"):
        await server.buscar_en_x("inflación")
    assert api.pool.colas == ["SearchTimeline"]


@pytest.mark.anyio
async def test_error_de_x_sugiere_actualizar(api):
    api.error = KeyError("data")
    with pytest.raises(ToolError, match="Clear build cache"):
        await server.buscar_en_x("inflación")


@pytest.mark.anyio
async def test_estado_del_scraper(api):
    r = await server.estado_del_scraper()
    assert r["diagnostico"] == "Activa y lista para buscar." and r["ultimos_avisos_de_x"] is None
    assert len(r["temas_configurados"]) == 7 and r["cuentas_seguidas"][1]["cuenta"] == "@MaxiFarina"


@pytest.mark.anyio
async def test_faltan_o_estan_mal_las_cookies(monkeypatch):
    falsa = APIFalsa()
    monkeypatch.setattr(server, "_api", falsa)
    monkeypatch.setattr(server, "_preparada", False)
    monkeypatch.delenv("X_COOKIES", raising=False)
    with pytest.raises(ToolError, match="Falta la variable X_COOKIES"):
        await server.cliente_x()
    monkeypatch.setenv("X_COOKIES", "cualquier=cosa")
    with pytest.raises(ToolError, match="auth_token y ct0"):
        await server.cliente_x()
    monkeypatch.setenv("X_COOKIES", "auth_token=abc; ct0=def")
    monkeypatch.setenv("X_USUARIO", "@mi_secundaria")
    assert await server.cliente_x() is falsa
    assert falsa.pool.agregada == ("mi_secundaria", "auth_token=abc; ct0=def")
    assert falsa.pool.reseteada is True


# ---------------------------------------------------------------------------
# Protocolo MCP y HTTP
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_por_protocolo_mcp(api):
    api.ventanas = lambda q, d, h: [post(1, 30, likes=3)]
    async with Client(server.mcp) as c:
        nombres = {t.name for t in (await c.list_tools()).tools}
        assert nombres == {"buscar_en_x", "reacciones_a_posts", "revisar_alertas", "estado_del_scraper"}
        res = await c.call_tool("buscar_en_x", {"consulta": "inflación", "horas": 2})
        assert not res.is_error and res.structured_content["total"] == 1
        res = await c.call_tool("revisar_alertas", {"incluir": "temas", "temas": ["UPCN"]})
        assert not res.is_error and res.structured_content["temas"][0]["tema"] == "UPCN"


CLAVE = "clave-de-prueba-1234567890"


def test_http_con_clave(monkeypatch):
    monkeypatch.setenv("CLAVE_ACCESO", CLAVE)
    app = server.app_http()
    inicio = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "prueba", "version": "1"}},
    }
    headers = {"Accept": "application/json, text/event-stream", "Host": "x-alertas.onrender.com"}
    with TestClient(app) as cliente:
        assert cliente.get("/").text == "ok"
        assert cliente.post(f"/{CLAVE}/mcp", json=inicio, headers=headers).status_code == 200
        assert cliente.post("/mcp", json=inicio, headers=headers).status_code == 404
        assert cliente.post("/otra-clave-cualquiera-123/mcp", json=inicio, headers=headers).status_code == 404


def test_http_exige_clave(monkeypatch):
    monkeypatch.setenv("CLAVE_ACCESO", "corta")
    with pytest.raises(SystemExit):
        server.app_http()
    monkeypatch.delenv("CLAVE_ACCESO")
    with pytest.raises(SystemExit):
        server.app_http()
