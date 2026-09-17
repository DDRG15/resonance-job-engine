"""
endpoint_registry.py — qué boards se leen sin navegador, y cómo.

Fase 1 del . Este archivo se commitea; `board_strategy.json` no.

Esa separación es el punto: el JSON local guarda estado de runtime —salud, último
probe, qué vio el último intento— y se pierde al clonar. La capacidad de un board
—«remoteok expone una API pública en /api y estos son sus campos»— es conocimiento
del proyecto, no estado de una máquina. Si viviera solo en el JSON, un clon limpio
volvería a abrir Chromium para leer un endpoint que ya sabemos que existe.

Cada entrada lleva la fecha y la medición con la que se verificó. Un endpoint sin
medición es una suposición, y las suposiciones sobre boards se pudren: cuatro de los
siete boards retirados el 2026-07-11 lo fueron por diagnósticos que envejecieron.

Para agregar un board: correr `python tools/probe_endpoints.py --solo <board>`, pegar
la medición en el comentario, y mapear los campos. Nada más.
"""

from __future__ import annotations

# Medido con tools/probe_endpoints.py el 2026-09-09 y re-verificado contra
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
    # Retirado el 2026-07-11 con la nota «HTTP 403 anti-bot, reactivar con camoufox».
    # El RSS sirve los mismos avisos sin anti-bot: camoufox era más caro y más frágil
    # que un GET. El feed no tiene campo de empresa —la mete en el título, medido
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
}


def politica_de(domain: str) -> dict | None:
    """La politica declarada de un board, o None si no hay una escrita."""
    return POLITICAS.get(domain)


def para(domain: str) -> dict | None:
    """La definición de endpoint de un board, o None si no tiene."""
    return ENDPOINTS.get(domain)


def dominios() -> list[str]:
    return sorted(ENDPOINTS)
