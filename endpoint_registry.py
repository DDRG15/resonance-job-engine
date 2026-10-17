"""
endpoint_registry.py — qué boards se leen sin navegador, y cómo.

Este archivo se commitea; `board_strategy.json` no.

Esa separación es el punto: el JSON local guarda estado de runtime —salud, último
probe, qué vio el último intento— y se pierde al clonar. La capacidad de un board
—«remoteok expone una API pública en /api y estos son sus campos»— es conocimiento
del proyecto, no estado de una máquina. Si viviera solo en el JSON, un clon limpio
volvería a abrir Chromium para leer un endpoint que ya sabemos que existe.

Cada entrada lleva la fecha y la medición con la que se verificó. Un endpoint sin
medición es una suposición, y las suposiciones sobre boards se pudren: cuatro de los
siete boards retirados el 2026-07-11 lo fueron por diagnósticos que envejecieron.

Para agregar un board: pedir el endpoint con un GET a mano (curl o requests), revisar
su robots.txt, pegar la medición en el comentario, y mapear los campos. Nada más.
"""

from __future__ import annotations

# Medido con GET directo el 2026-09-09 y re-verificado contra
# endpoint_scraper el 2026-09-14. `items` es la primera página, no el catálogo.
ENDPOINTS: dict[str, dict] = {

    # 99 items · salario entero en 12% · ubicación en 70% · empresa en 100%.
    # Por CSS este mismo board daba salario en 1,2% y ubicación en 0% sobre 518
    # filas acumuladas — no tiene selector de ubicación. El techo no era del board.
    # /api no acepta búsqueda: devuelve los últimos avisos y GEMA filtra por título.
    # robots.txt: `User-agent: * / Allow: /`.
    "remoteok.com": {
        "endpoint_template": "https://remoteok.com/api",
        "response_format": "json",
        "field_map": {
            "title": "position",
            "company": "company",
            "url": "url",
            "location": "location",
            "salary_min": "salary_min",
            "salary_max": "salary_max",
        },
    },

    # 87-90 items en ~484 ms · empresa en 100% · región en 100%.
    # Retirado el 2026-07-11 con la nota «HTTP 403 anti-bot».
    # El RSS sirve los mismos avisos sin anti-bot: un navegador con evasión era más caro
    # y más frágil que un GET. El feed no tiene campo de empresa —la mete en el título, medido
    # 87 de 87— de ahí `titulo_trae_empresa`.
    # Trae además `expires_at` en el 100% de los items: es la fuente del `deadline`
    # de la Fase 6, que hoy no existe en ninguna parte del código.
    # robots.txt: `Allow: /`, prohíbe solo /admin/, /account/ y /job-seekers/account/.
    "weworkremotely.com": {
        "endpoint_template": "https://weworkremotely.com/remote-jobs.rss",
        "response_format": "rss",
        "titulo_trae_empresa": True,
        "field_map": {
            "title": "title",
            "url": "link",
            "location": "region",
            # 90 de 90 items lo traen. Es una de las dos fuentes de deadline que
            # existen hoy, y llego gratis con un board que estaba dado por perdido.
            "deadline": "expires_at",
        },
    },

    # Medido el 2026-09-30:
    # `country=<pais>` sin `q` = 2.902 avisos; con `q=python developer` = 171. Traer el
    # feed entero son 145 paginas de 20, asi que va una consulta por titulo, pagina 1.
    # Por CSS este board chocaba contra un 403 en cada rama: 6 filas en el registry
    # desde mayo. La API responde 200 sin key.
    # - `guid` es la pagina del aviso EN Himalayas: identidad estable para el hash y el
    #   enlace de vuelta que piden sus terminos ("link back... and mention Himalayas").
    # - `expiryDate` es epoch en segundos, 20 de 20 avisos; `_a_fecha` lo lee desde
    #   esta fase. Antes devolvia None y el deadline se perdia en silencio.
    # - salario: `minSalary` en 6 de 20, periodo annual 17 / hourly 2 / monthly 1,
    #   moneda USD 8 / EUR 2. Solo se compone el anual y con su codigo (ver `_salario`).
    # - descripcion: mediana 3.585 caracteres limpios. Entra con la Fase 7.
    # crawl_delay_s = 1: los terminos no dan cifra, solo "429 al pasarse"; un segundo
    # entre consultas es 51 s por corrida con la busqueda del CLI.
    # El OpenAPI oficial documenta `guid`
    # como identificador y `applicationLink` como URL, aunque la respuesta real trae una
    # URL en los dos. Se usa `guid` si es una URL de verdad y si no `applicationLink`;
    # un ID opaco nunca se convierte en enlace. `totalCount` marca la cobertura parcial
    # (20 de 171) y `ttl_consulta_s` = 20 h no repite en el dia una consulta que la API
    # solo actualiza cada 24 h.
    "himalayas.app": {
        "endpoint_template": "https://himalayas.app/jobs/api/search?q={title}&country={pais}",
        "response_format": "json",
        "crawl_delay_s": 1.0,
        "ubicacion_es_lista": True,
        "total_path": "totalCount",
        "ttl_consulta_s": 20 * 3600,
        "field_map": {
            "title": "title",
            "company": "companyName",
            "url": "guid",
            "url_alternativa": "applicationLink",
            "location": "locationRestrictions",
            "deadline": "expiryDate",
            "salary_min": "minSalary",
            "salary_max": "maxSalary",
            "salary_currency": "currency",
            "salary_period": "salaryPeriod",
            # Fase 7: HTML, mediana 3.585 caracteres limpios (anexo de la Fase 0).
            "description": "description",
        },
    },

    # La API publica trae el catalogo publico entero: 16 avisos,
    # con o sin `category` (medido 2026-09-30), con 24 h de retraso respecto de la web.
    # Terminos: "more than 2x per minute will be blocked", y como mucho 4 veces por dia:
    # feed fijo, un pedido, y `intervalo_minimo_s` de 6 h entre corridas.
    # `candidate_required_location` es texto libre ("Worldwide", "USA, Canada,
    # Argentina, Mexico, Chile"); `salary` tambien, en 10 de 16. Sin fecha de cierre.
    "remotive.com": {
        "endpoint_template": "https://remotive.com/api/remote-jobs",
        "response_format": "json",
        "intervalo_minimo_s": 6 * 3600,
        "field_map": {
            "title": "title",
            "company": "company_name",
            "url": "url",
            "location": "candidate_required_location",
            "salary_raw": "salary",
            "description": "description",     # Fase 7: HTML, mediana 3.611
        },
    },

    # Terminos: "must not exceed once per hour" y conservar la
    # URL canonica de Jobicy. Un pedido por corrida, y el filtro por titulo del lado de
    # GEMA. Las claves de salario aparecen solo cuando hay salario y vienen con moneda y
    # periodo: `BRL 249000-409000 yearly`.
    # `geo=latam`, medido contra el feed general con el filtro de titulo y el perfil
    # real: general (2026-09-30) 100 items -> 32 relevantes, ubicacion PASS 8 (el 43%
    # del feed es "USA"); latam (2026-10-01) 73 items -> 27 relevantes, PASS 27. Trae el
    # catalogo LATAM entero (73 < count) e incluye los "Anywhere" (8 de 73).
    "jobicy.com": {
        "endpoint_template": "https://jobicy.com/api/v2/remote-jobs?count=100&geo=latam",
        "response_format": "json",
        "intervalo_minimo_s": 3600,
        "field_map": {
            "title": "jobTitle",
            "company": "companyName",
            "url": "url",
            "location": "jobGeo",
            "salary_min": "salaryMin",
            "salary_max": "salaryMax",
            "salary_currency": "salaryCurrency",
            "salary_period": "salaryPeriod",
            "description": "jobDescription",  # Fase 7: HTML
        },
    },

    # 326 avisos en la pagina 1, 19 con `remote=true`. Es un
    # board europeo (.com, .co.uk, .fr, .ch) y casi todo es presencial: entra solo lo
    # remoto, y con la region del board, porque "Remote" a secas (10 de 19) es remoto
    # dentro de Europa. `created_at` es epoch; no hay fecha de cierre.
    "arbeitnow.com": {
        "endpoint_template": "https://www.arbeitnow.com/api/job-board-api",
        "response_format": "json",
        "solo_si_campo": "remote",
        "region_del_board": "Europe",
        "field_map": {
            "title": "title",
            "company": "company_name",
            "url": "url",
            "location": "location",
            "description": "description",     # Fase 7: HTML
        },
    },
}


# ── Fase 9 (2026-09-17): politica de acceso por board ────────────────────────
#
# Los cuatro primeros salieron del inventario del 2026-09-09 y no son opiniones:
# son lo que el sitio devolvio o lo que declara su robots.txt.
#
# Un board `declined` deja de reaparecer como un fallo tecnico recurrente. remote.co
# y welcometothejungle.com se "diagnosticaron" dos veces entre julio y septiembre
# porque la razon vivia en un comentario de Python y no en la estrategia que el
# router lee.
POLITICAS: dict[str, dict] = {
    # ReadTimeout de 21 s en dos endpoints distintos. No es anti-bot ni template
    # viejo: el host no responde.
    "remote.co": {"access_policy": "declined", "policy_reason":
                  "host muerto — ReadTimeout 21s en dos endpoints (2026-09-09)"},

    # Responde 200 con cero items, consistente con su muro de login. Un board con
    # auth-wall no se scrapea.
    "welcometothejungle.com": {"access_policy": "auth_required", "policy_reason":
                               "muro de login — 200 con cero items (2026-09-09)"},

    # HTTP 429 al PRIMER pedido, sin haber hecho nada antes. Era el unico board
    # activo produciendo cero y sin diagnostico: no eran selectores stale.
    "remotivated.com": {"access_policy": "rate_limited", "policy_reason":
                        "HTTP 429 al primer pedido (2026-09-09) — bajar frecuencia"},

    # robots.txt: `User-agent: * / Allow: /`, bloquea a Amazonbot y Applebot-Extended
    # por nombre, no a genericos.
    "remoteok.com": {"access_policy": "open", "policy_reason":
                     "robots.txt: Allow: / (2026-09-09)"},

    # `Allow: /`, prohibe solo /admin/, /account/ y /job-seekers/account/. El RSS no
    # esta en la lista.
    "weworkremotely.com": {"access_policy": "open", "policy_reason":
                           "robots.txt permite el RSS (2026-09-09)"},

    # robots.txt responde 404 (sin reglas). La API publica de busqueda no pide key y
    # sus terminos permiten el uso con atribucion: enlazar al aviso en Himalayas, que es
    # exactamente la URL que GEMA guarda.
    "himalayas.app": {"access_policy": "open", "policy_reason":
                      "API publica sin key, atribucion por enlace (2026-09-30)"},

    # robots.txt sin reglas. Terminos: enlazar y nombrar a Remotive, 4 pedidos por dia.
    "remotive.com": {"access_policy": "open", "policy_reason":
                     "API publica sin key, max 4/dia, atribucion por enlace (2026-09-30)"},

    # robots.txt solo prohibe `/*api_access_key=`. Terminos: 1 pedido por hora.
    "jobicy.com": {"access_policy": "open", "policy_reason":
                   "API publica sin key, max 1/hora, URL canonica (2026-09-30)"},

    # robots.txt de 3 lineas, nada sobre la API. Sin limite declarado.
    "arbeitnow.com": {"access_policy": "open", "policy_reason":
                      "API publica sin key, sin limite declarado (2026-09-30)"},
}


def politica_de(domain: str) -> dict | None:
    """La politica declarada de un board, o None si no hay una escrita."""
    return POLITICAS.get(domain)


def para(domain: str) -> dict | None:
    """La definición de endpoint de un board, o None si no tiene."""
    return ENDPOINTS.get(domain)


def dominios() -> list[str]:
    return sorted(ENDPOINTS)
