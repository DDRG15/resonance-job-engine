"""
tools/autorizar_gmail.py — autoriza a GEMA a LEER las alertas de Gmail (Fase 11).

Dos usos:

    python tools/autorizar_gmail.py            abre el navegador, el usuario acepta el permiso
                                                `gmail.readonly` y el token queda en
                                                credentials/gmail_token.json
    python tools/autorizar_gmail.py --probar   lee las alertas reales de los ultimos dias
                                                y muestra lo que el parser saca de cada una.
                                                NO escribe nada: ni registry ni traza.

El `--probar` es el paso obligatorio antes de prender `GEMA_GMAIL_ALERTAS=1`: el parser se
escribio contra fixtures sinteticas, y esta es la primera vez que ve un correo real. Si una
alerta sale con 0 avisos o con empresa vacia, el formato real no es el que se supuso, y se
arregla con esa alerta como fixture nueva antes de prender nada.

Requisito: un cliente OAuth «Desktop app» creado en Google Cloud Console, con la Gmail API
habilitada, descargado a credentials/gmail_client_secret.json. credentials/*.json esta
excluido por .gitignore y .dockerignore (test_gmail_alertas_credenciales.py lo verifica).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import gmail_alertas  # noqa: E402


#: Google vence el refresh_token a los 7 dias si la app OAuth esta en modo «Testing». Con el
#: cron de martes/viernes/sabado, la corrida del dia 8 da `invalid_grant` y la fuente queda
#: en error hasta que alguien re-autorice.
AVISO_TESTING = (
    "OJO: si la app OAuth quedo en modo «Testing» en Google Cloud Console, el token vence "
    "a los 7 dias (invalid_grant). Pasarla a «In production» (tipo Desktop, un solo "
    "usuario: no pide verificacion) o re-autorizar cada semana.")


def autorizar() -> int:
    if not os.path.isfile(config.GMAIL_CLIENT_SECRET_PATH):
        print(f"Falta {config.GMAIL_CLIENT_SECRET_PATH}: crear un cliente OAuth «Desktop app» "
              f"en Google Cloud Console (Gmail API habilitada) y descargarlo ahi.")
        return 2
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(
        config.GMAIL_CLIENT_SECRET_PATH, gmail_alertas.SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    os.makedirs(os.path.dirname(os.path.abspath(config.GMAIL_TOKEN_PATH)), exist_ok=True)
    gmail_alertas._guardar_token(config.GMAIL_TOKEN_PATH, creds.to_json())
    print(f"Token guardado en {config.GMAIL_TOKEN_PATH} (permiso: gmail.readonly).")
    print("Siguiente paso: python tools/autorizar_gmail.py --probar")
    print(AVISO_TESTING)
    return 0


def probar(dias: int) -> int:
    try:
        lector = gmail_alertas.LectorGmail()
    except gmail_alertas.GmailNoConfigurado as exc:
        print(f"No configurado: {exc}")
        return 2
    consulta = gmail_alertas.consulta_gmail(config.GMAIL_ALERTAS_REMITENTES, dias)
    print(f"Consulta: {consulta}")
    mensajes = lector.mensajes(consulta, config.GMAIL_ALERTAS_MAX_MENSAJES)
    print(f"{len(mensajes)} alerta(s).\n")
    sospechosas = 0
    for m in mensajes:
        proveedor = gmail_alertas.proveedor_de_remitente(m.remitente)
        avisos = gmail_alertas.parsear_alerta(proveedor, m.html) if proveedor else []
        sin_empresa = sum(1 for a in avisos if not a.empresa)
        marca = "  <-- REVISAR" if not avisos or sin_empresa else ""
        sospechosas += bool(marca)
        print(f"[{m.id}] {m.remitente} -> {proveedor or 'remitente no reconocido'}: "
              f"{len(avisos)} aviso(s){marca}")
        for a in avisos:
            print(f"    {a.titulo} | {a.empresa or '(sin empresa)'} | "
                  f"{a.ubicacion or '(sin ubicacion)'} | {a.salario or '-'} | {a.url}")
    if sospechosas:
        print(f"\n{sospechosas} alerta(s) a revisar: guardar su HTML como fixture en "
              f"tests/fixtures/gmail/ y ajustar el parser ANTES de prender GEMA_GMAIL_ALERTAS.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--probar", action="store_true",
                    help="leer las alertas reales y mostrar lo que se parsea, sin escribir")
    ap.add_argument("--dias", type=int, default=config.GMAIL_ALERTAS_DIAS)
    args = ap.parse_args(argv)
    return probar(args.dias) if args.probar else autorizar()


if __name__ == "__main__":
    sys.exit(main())
