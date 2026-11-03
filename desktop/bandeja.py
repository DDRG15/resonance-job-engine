"""
desktop/bandeja.py — ícono de la bandeja del sistema (pystray).

Es lo único que le dice al usuario que Resonance sigue corriendo cuando cerró la ventana.
Sin él, cerrar la ventana dejaría un proceso invisible que el usuario solo puede matar
desde el administrador de tareas.

Tres entradas, y las tres hacen algo:
- Abrir: la ventana de la UI.
- Correr búsqueda ahora: POST /api/scrape. Si todavía no hay una búsqueda confirmada, el
  backend respondería 400; en ese caso abre la UI, que es donde se arma.
- Salir: para uvicorn y borra runtime.json (lo hace `app.py` vía `al_salir`).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from desktop import runtime

log = logging.getLogger("resonance.bandeja")


def correr_busqueda(url: str) -> tuple[str, str]:
    """
    Dispara una corrida si hay configuración. Devuelve (resultado, mensaje) con resultado
    en {"iniciada", "en_curso", "sin_config", "error"}; no levanta.

    Se pregunta GET /api/config primero y no se confía en el 400 del POST: el 400 también
    sale por otros motivos y «sin config» merece abrir la UI, no un aviso de error.
    """
    try:
        st, cuerpo = runtime.http_json("GET", f"{url}/api/config", timeout=5.0)
        if st != 200 or not isinstance(cuerpo, dict) or not cuerpo.get("config"):
            return "sin_config", "Todavía no hay una búsqueda armada: abro el panel."
        st, _ = runtime.http_json("POST", f"{url}/api/scrape", {}, timeout=10.0)
        if st == 202:
            return "iniciada", "Búsqueda iniciada."
        if st == 409:
            return "en_curso", "Ya hay una búsqueda corriendo."
        return "error", f"El servidor respondió {st}."
    except Exception as exc:  # noqa: BLE001 — la bandeja nunca debe caerse por un clic
        return "error", f"No se pudo iniciar la búsqueda: {exc}"


def _imagen_icono():
    """Ícono generado: ondas concéntricas (resonancia). Evita empaquetar un .ico."""
    from PIL import Image, ImageDraw

    tam = 64
    img = Image.new("RGBA", (tam, tam), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, tam - 1, tam - 1), radius=14, fill=(24, 32, 56, 255))
    for r, color in ((24, (92, 124, 250, 255)), (15, (132, 156, 255, 255)),
                     (6, (255, 255, 255, 255))):
        d.ellipse((32 - r, 32 - r, 32 + r, 32 + r), outline=color, width=4)
    return img


def reanudar_corrida(url: str) -> tuple[str, str]:
    try:
        status, body = runtime.http_json("GET", f"{url}/api/results", timeout=5.0)
        last = body.get("last_run") if status == 200 and isinstance(body, dict) else None
        if not last or not last.get("resumable"):
            return "sin_avance", "No hay una corrida pendiente para reanudar."
        if last.get("resume_requires_cv"):
            return "requiere_cv", "Cargá el mismo CV en el panel para reanudar."
        from urllib.parse import quote
        run_id = quote(last["run_id"], safe="")
        status, _ = runtime.http_json("POST", f"{url}/api/scrape/{run_id}/resume", {}, timeout=30.0)
        return ("iniciada", "Corrida reanudada.") if status == 202 else ("error", "Abrí el panel para revisar la corrida.")
    except Exception as exc:
        return "error", f"No se pudo reanudar: {exc}"


class Bandeja:
    def __init__(self, url: str, abrir: Callable[[], None],
                 al_salir: Callable[[], None]) -> None:
        self._url = url
        self._abrir = abrir
        self._al_salir = al_salir
        self._icono = None

    def _on_abrir(self, icono=None, item=None) -> None:
        self._abrir()

    def _on_correr(self, icono=None, item=None) -> None:
        resultado, mensaje = correr_busqueda(self._url)
        if resultado == "sin_config":
            self._abrir()
            return
        try:
            if self._icono is not None:
                self._icono.notify(mensaje, "Resonance")
        except Exception:  # notify no existe en todos los backends de pystray
            log.info("bandeja: %s", mensaje)

    def _on_salir(self, icono=None, item=None) -> None:
        self._al_salir()
        self.detener()

    def _on_reanudar(self, icono=None, item=None) -> None:
        resultado, mensaje = reanudar_corrida(self._url)
        if resultado in ("requiere_cv", "error"):
            self._abrir()
        try:
            if self._icono is not None:
                self._icono.notify(mensaje, "Resonance")
        except Exception:
            log.info("bandeja: %s", mensaje)

    def ejecutar(self) -> None:
        """
        Bloquea hasta `detener()`. Es el `target` del hilo de la bandeja y NO levanta:
        sin pystray/Pillow, sin display o con un backend roto, loguea «sin bandeja» y
        vuelve. El try/except de `app.py` solo cubre crear el hilo; una excepción dentro
        del hilo nunca llegaría ahí y saldría por `threading.excepthook` sin el aviso.
        """
        try:
            self._correr_icono()
        except Exception as exc:  # noqa: BLE001 — sin bandeja la app sigue sirviendo
            log.warning("sin bandeja (%s): la app sigue corriendo", exc)

    def _correr_icono(self) -> None:
        import pystray

        if not getattr(pystray.Icon, "HAS_MENU", True):
            log.warning(
                "bandeja: el backend de pystray no muestra menú (HAS_MENU=False), así que "
                "«Correr búsqueda ahora», «Reanudar corrida» y «Salir» no son alcanzables "
                "desde el ícono. En Linux falta AppIndicator/GTK vía PyGObject. «Abrir» "
                "sigue como acción por defecto y la UI web tiene los controles.")

        menu = pystray.Menu(
            pystray.MenuItem("Abrir", self._on_abrir, default=True),
            pystray.MenuItem("Correr búsqueda ahora", self._on_correr),
            pystray.MenuItem("Reanudar corrida", self._on_reanudar),
            pystray.MenuItem("Salir", self._on_salir),
        )
        self._icono = pystray.Icon("resonance", _imagen_icono(), "Resonance", menu)
        self._icono.run()

    def detener(self) -> None:
        icono: Optional[object] = self._icono
        if icono is not None:
            try:
                icono.stop()
            except Exception:
                pass
