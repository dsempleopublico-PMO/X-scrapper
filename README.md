# MCP de alertas de X (Twitter)

Servidor MCP que conecta Claude con X para **monitorear temas y cuentas, y levantar alertas**, sin pagar nada: ni API de X, ni servicios de scraping. Con esto le podés preguntar cosas como:

- "¿Qué se está diciendo en X sobre despidos en el Estado en las últimas 24 horas?"
- "¿Qué le está respondiendo la gente a Sturzenegger en sus posts de hoy? ¿Se armó debate?"
- "¿Qué dicen las respuestas y las citas de este post? https://x.com/MaxiFarina/status/..."
- "Revisá las alertas y decime si pasó algo."

## Qué monitorea

**Temas** (cada uno con una búsqueda armada; se cambian en `server.py` → `TEMAS_POR_DEFECTO`, o con la variable `TEMAS`):

| Tema | Qué busca en X |
|---|---|
| Empleo público | "empleo público", "empleados públicos" (con y sin tilde) |
| Despidos en el Estado | despidos / despedidos / cesantías **y** estatales / "en el Estado" / "sector público" / "administración pública" |
| Conflictos gremiales | "conflicto gremial", paro, "medida de fuerza", "plan de lucha" **y** estatales / sector público / ATE / UPCN |
| Retiro voluntario | "retiro voluntario", "retiros voluntarios" |
| ATE | ATE **y** estatales / paro / gremio / sindicato / trabajadores / despidos / Estado / movilización (ATE solo trae mucho ruido: "ate" también es un verbo) |
| UPCN | UPCN |
| Rosana Reggi | "Rosana Reggi" — **avisa cada vez que aparece**, aunque sea un solo post |

**Cuentas** (se cambian en `CUENTAS_POR_DEFECTO`, o con la variable `CUENTAS`):

| Cuenta | Qué mira |
|---|---|
| Federico Sturzenegger (@fedesturze) | Qué le responde la gente a sus posts, quién los cita y con qué comentario, si se arman discusiones entre usuarios, y qué publican otros sobre él ("Sturzenegger", "Sturze" o @fedesturze). |
| Maximiliano Fariña (@MaxiFarina) | Lo mismo; menciones como "Maxi Fariña", "Maximiliano Fariña" o @MaxiFarina. |

## Cómo funciona

X no deja buscar sin iniciar sesión, así que el servidor usa **[twscrape](https://github.com/vladkens/twscrape)** (open source y gratis), que le habla a X igual que la página web, usando las **cookies de una cuenta de X**.

> ⚠️ **Usá una cuenta secundaria, nunca tu cuenta principal.** El uso automatizado va contra las reglas de X: si lo detecta, la cuenta que uses puede quedar restringida o suspendida. El servidor busca despacio y con topes para que eso sea poco probable, pero el riesgo existe.

Tiene 4 herramientas:

| Herramienta | Qué hace |
|---|---|
| `buscar_en_x` | Trae los posts de las últimas horas de un tema configurado ("despidos en el Estado") o de cualquier búsqueda, con autor, texto, likes, retuits, vistas y enlace, más un resumen. Acepta operadores de X: `"frase exacta"`, `OR`, `from:usuario`, `@usuario`, `#hashtag`, `-palabra`, `lang:es`, `min_faves:100`. |
| `reacciones_a_posts` | **Qué dice la gente sobre los posts de una cuenta** (o sobre un post puntual, pasándole el enlace): las respuestas más votadas, las **citas** (quienes comparten el post con su propia opinión), las **discusiones entre usuarios** dentro de las respuestas (con el ida y vuelta), si el autor contesta, y un **nivel de debate** (alto, medio o bajo) con las señales que lo explican. También trae lo que otros publicaron sobre esa persona. |
| `revisar_alertas` | Revisa todos los temas y cuentas y dice si hay alertas (ver abajo). |
| `estado_del_scraper` | Dice si la cuenta de X está activa, si está en pausa por el límite de X o si hay que renovar las cookies, y qué temas y cuentas tiene configurados. |

### Cómo decide una alerta

Compara las últimas `horas` (6 por defecto) con las 6 horas anteriores.

**Temas:**

| Alerta | Cuándo salta | Por defecto |
|---|---|---|
| `pico` | El ritmo de posts por hora se multiplicó por `factor_pico` (y hay al menos 10 posts). | ×2 |
| `viral` | Un post superó `min_interacciones` (likes + retuits + respuestas + citas). | 100 |
| `cuenta_grande` | Publicó alguien con `min_seguidores` o más. | 50.000 |
| `aparicion` | El tema está marcado para avisar siempre (Rosana Reggi) y apareció al menos un post. | — |

**Cuentas seguidas:**

| Alerta | Cuándo salta | Por defecto |
|---|---|---|
| `publico` | La cuenta publicó posts nuevos (se apaga con `avisar_cada_post=false`). | sí |
| `debate` | Uno de sus posts de las últimas `horas_debate` desató un debate (ver abajo). Trae las respuestas, discusiones y citas para resumir qué se dice. | 24 h |
| `pico_menciones` | Las menciones de otros crecieron `factor_pico` veces: la discusión salió de sus posts. | ×2 |
| `mencion_viral` | Un post de otro sobre esa persona superó `min_interacciones_menciones`. | 300 |

**Cómo se estima el debate en un post.** Suma puntos por: mucha conversación (50 o más respuestas y citas: 1 punto; 200 o más: 2), **más respuestas y citas que likes** (2 puntos; la mitad o más: 1; es la señal típica de polémica), 3 o más respuestas que abrieron su propia discusión entre usuarios (1) y 20 o más citas (1). Alto: 3 puntos o más; medio: 1 o 2; bajo: 0. Junto con el nivel vienen las señales en palabras ("lo citaron 40 veces", "respondieron cuentas grandes: …", "el autor les contestó a otros usuarios 3 veces"), y el contenido para que Claude resuma argumentos, críticas y tono.

Todo se puede cambiar al pedirlo ("revisá las alertas con ventana de 12 horas y avisame solo si algo pasa de 500 interacciones"). X muestra primero las respuestas más relevantes, así que en posts con miles de respuestas se lee una muestra (el resultado dice cuántas se leyeron).

## Archivos

```
x-alertas-mcp/
├── server.py            ← el servidor MCP (todo el código, comentado; temas y cuentas arriba de todo)
├── tests/test_server.py ← pruebas sin internet (simulan a X)
├── requirements.txt     ← dependencias
├── pyproject.toml       ← lo mismo, para uv
└── Dockerfile           ← para publicarlo en Render
```

---

## Paso 1 — Cuenta secundaria de X y sus cookies

1. Creá una cuenta nueva en X con otro mail. Conviene usarla normal un par de días (seguir algunas cuentas, poner foto): las cuentas recién creadas que solo buscan son más sospechosas.
2. En Chrome abrí una **ventana de incógnito** (`Ctrl+Shift+N`) y entrá a x.com con esa cuenta.
3. Apretá `F12` (o clic derecho → **Inspeccionar**) → pestaña **Application** / **Aplicación** → a la izquierda **Cookies** → `https://x.com`.
4. Buscá las filas **`auth_token`** y **`ct0`** y copiá sus valores (columna *Value*).
5. Armá una línea así (sin comillas):
   ```
   auth_token=VALOR_DE_AUTH_TOKEN; ct0=VALOR_DE_CT0
   ```
6. Cerrá la ventana de incógnito **sin tocar "Cerrar sesión"** (si cerrás sesión, las cookies dejan de servir).

Las cookies son como la contraseña de esa cuenta: no las compartas ni las subas a GitHub.

## Paso 2 — Subir el código a GitHub

1. En GitHub: **New repository** → nombre `x-alertas-mcp` → **Private** → *Create repository*.
2. **uploading an existing file** → arrastrá todos los archivos de esta carpeta (incluida la carpeta `tests`) → *Commit changes*.

## Paso 3 — Publicarlo en Render (gratis)

1. En [render.com](https://render.com): **New + → Web Service** → elegí el repo `x-alertas-mcp`. (Si no aparece, en *Configure GitHub* dale acceso a ese repo.)
2. **Language:** Docker · **Instance Type:** Free.
3. En **Environment Variables** cargá:

   | Variable | Valor | |
   |---|---|---|
   | `X_COOKIES` | la línea del Paso 1 | obligatoria |
   | `CLAVE_ACCESO` | una clave larga al azar (mín. 16 letras/números, sin espacios) | obligatoria |
   | `X_USUARIO` | el @ de la cuenta secundaria | opcional |

   Los temas y cuentas ya vienen cargados en el código. Solo si los querés cambiar sin tocar `server.py`, agregá `TEMAS` y/o `CUENTAS` (ver más abajo).

4. **Deploy Web Service**. Cuando termina, arriba aparece la URL, ej. `https://x-alertas-mcp.onrender.com`. Si la abrís en el navegador tiene que decir `ok`.

## Paso 4 — Conectarlo a Claude

En claude.ai: **Configuración → Conectores → Agregar conector personalizado**, y pegá:

```
https://x-alertas-mcp.onrender.com/TU_CLAVE_ACCESO/mcp
```

La clave en la URL es lo que impide que otra persona use tu servidor (y tu cuenta de X). No compartas esa URL.

## Paso 5 — Probar

1. "Usá estado_del_scraper." → tiene que decir que la cuenta está activa y listar los temas y cuentas.
2. "¿Qué se está diciendo en X sobre retiro voluntario en las últimas 24 horas?" → si devuelve posts, anda todo.
3. "¿Qué le responde la gente a Sturzenegger en sus posts de hoy?"
4. "Revisá las alertas."

## Paso 6 — Alertas automáticas

Las alertas se revisan solas con una **tarea programada de Claude**, por ejemplo cada 6 horas. Una revisión completa hace cerca de 50 consultas a X (unas 10 por cuenta, porque lee respuestas y citas, y hasta 4 por tema), así que conviene partirla en dos llamadas. Texto de ejemplo para la tarea:

> Usá el conector de alertas de X. Primero corré `revisar_alertas` con `incluir="cuentas"` y después con `incluir="temas"`, las dos con ventana de 6 horas. Si en ninguna `hay_alertas` es true, terminá sin avisar. Si hay alertas, armá un resumen corto. Para Sturzenegger y Fariña: qué publicaron y, sobre todo, qué le dice la gente a cada post (argumentos a favor y en contra, críticas, tono, si hay discusión entre usuarios o cuentas grandes metidas, y si la discusión se extendió más allá del post), con 2 o 3 respuestas o citas de ejemplo y sus enlaces. Para cada tema con alerta: qué pasó (pico, viral, cuenta grande, aparición) y qué se está diciendo, con los 3 posts más relevantes y sus enlaces.

## Cambiar temas y cuentas sin tocar el código

En Render → Environment podés agregar estas variables. **Reemplazan** la lista entera del código. Cada entrada va separada por punto y coma, y las partes de cada entrada por `|`:

```
TEMAS   = Empleo público | "empleo público" OR "empleados públicos" ; Retiro voluntario | "retiro voluntario" ; Mi nombre | "Nombre Apellido" | siempre
CUENTAS = fedesturze | Federico Sturzenegger | Sturzenegger OR Sturze ; MaxiFarina | Maximiliano Fariña | "Maxi Fariña"
```

- Tema: `Nombre | búsqueda en X | siempre` (el `siempre` es opcional: avisa cada vez que aparece).
- Cuenta: `usuario | Nombre | otras formas de mencionarla` (las dos últimas partes son opcionales).

Después de cambiarlas, Render reinicia solo.

---

## Límites y cuidados

- **Límite de X:** X limita cuántas consultas puede hacer cada cuenta en cada período de 15 minutos. Si se pasa, el servidor avisa cuánto falta para que se libere y devuelve lo que alcanzó a revisar. Por eso la revisión va de a un tema por vez y conviene partirla en cuentas y temas.
- **Primera consulta lenta:** en el plan gratis de Render el servidor se duerme a los 15 minutos sin uso y tarda cerca de un minuto en despertar. Si la primera consulta falla por tiempo, repetila.
- **Las cookies duran meses**, salvo que cierres sesión en esa cuenta o X la invalide. Cuando venzan, el servidor lo dice: repetí el Paso 1 y actualizá `X_COOKIES` en Render.
- **Se puede romper cuando X cambia su página.** twscrape se actualiza seguido: en Render tocá **Manual Deploy → Clear build cache & deploy** para instalar la última versión.
- **Plan Free de Claude:** permite un solo conector personalizado. Si ya tenés otro (por ejemplo el del Boletín Oficial), vas a tener que elegir.

## Problemas frecuentes

| El servidor dice | Qué hacer |
|---|---|
| "X no reconoce la sesión de la cuenta secundaria" | Las cookies vencieron o se copiaron mal. Repetí el Paso 1, actualizá `X_COOKIES` y reiniciá (Manual Deploy → Deploy latest commit). |
| "X restringió o suspendió la cuenta secundaria" | Entrá a x.com con esa cuenta y fijate si pide verificar algo. Si está suspendida, hace falta otra cuenta. |
| "X bloqueó los pedidos que salen del servidor" | Agregá en Render la variable `TWS_HTTP_BACKEND` = `curl` y reiniciá. Si sigue, X está bloqueando a Render: la alternativa es correr el servidor en tu compu. |
| "X cambió algo en su página" | Manual Deploy → **Clear build cache & deploy**. Si sigue, esperar el arreglo en [twscrape](https://github.com/vladkens/twscrape/issues). |
| "La cuenta llegó al límite de consultas" | Esperar los minutos que indica. Revisar cuentas y temas por separado, o pedir menos posts. |
| "no parece un usuario de X" | La variable `CUENTAS` está mal escrita: el primer dato de cada cuenta es el usuario de X, sin espacios. |
| El conector no conecta | Revisá que la URL termine en `/TU_CLAVE_ACCESO/mcp` y que la clave sea idéntica a la de Render. |

## Pruebas (opcional, en tu compu)

```bash
uv run --with pytest pytest     # pruebas sin internet
```

## Uso responsable

Solo lee contenido público, como lo vería cualquier persona con cuenta en X. Usalo para monitorear temas y la comunicación pública de funcionarios, no para juntar datos personales de gente particular ni para descargar volúmenes enormes. Lo que dice un post no es un hecho comprobado.
