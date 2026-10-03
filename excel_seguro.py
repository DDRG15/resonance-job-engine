"""
excel_seguro.py — arma el XLSX de resultados sin que el texto de un tercero sea codigo.

El titulo, la empresa, el salario y la URL de cada fila los escribio un board. openpyxl
documenta que una cadena que empieza con `=` se escribe como FORMULA, y pandas.to_excel no
lo evita: un aviso titulado `=HYPERLINK("http://evil","x")` sale del export como una celda
con formula que Excel evalua al abrir el archivo, y `=cmd|' /C calc'!A0` es el vector
clasico de DDE. Nadie vio un aviso asi en el registry; el camino existe, y el que abre el
XLSX es el usuario, con su Excel, sobre un archivo que GEMA le dijo que era de resultados.

La defensa vive en el borde de exportacion, no en la DB ni en la UI: el texto guardado y el
que se ve en pantalla no cambian. Tres reglas, todas sobre las columnas que vienen de
fuera (Title, Company, Salary, Apply Link, Source):

  1. Toda celda queda como TEXTO (`data_type == "s"`), nunca como formula. Se fuerza
     despues de escribirla, en vez de anteponerle un apostrofo o un espacio: el contenido
     visible es EXACTAMENTE el original (`=HYPERLINK(...)` se ve como esa cadena), y
     `+cmd`, `-1+1` o `@SUM(A1)` tampoco cambian de aspecto.
  2. La URL es un enlace clicable solo si su esquema es http o https y trae host. Un
     `javascript:...`, `file://...` o `\\\\servidor\\share` queda como texto visible, sin
     hipervinculo: el enlace es lo unico del archivo que ejecuta algo al hacer clic.
  3. Se quitan los caracteres de control que XML 1.0 no admite (openpyxl lanza
     IllegalCharacterError con un `\\x07` en el titulo: un aviso adversarial tumbaba el
     export entero con un 500) y se recorta a 32.767 caracteres, el maximo de una celda;
     mas que eso y Excel ofrece "reparar" el archivo y descarta el contenido.
"""

import io
from urllib.parse import urlparse

import pandas as pd
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

# Columnas cuyo contenido escribio un tercero. Tier, Match Score y Date Fetched los
# calcula GEMA.
COLUMNAS_EXTERNAS = ("Title", "Company", "Salary", "Apply Link", "Source")

# Maximo de caracteres por celda en XLSX, y maximo de largo de una URL de hipervinculo en
# Excel (2079; mas largo y el enlace se descarta o el archivo se "repara").
_MAX_CELDA = 32767
_MAX_URL_ENLACE = 2000


def limpiar_celda(valor):
    """Texto apto para una celda: sin caracteres ilegales en XML y dentro del tope."""
    if valor is None:
        return ""
    texto = ILLEGAL_CHARACTERS_RE.sub("", str(valor))
    return texto[:_MAX_CELDA]


def es_url_enlazable(url: str) -> bool:
    """True solo para http/https con host y de largo que Excel acepta como enlace."""
    candidata = (url or "").strip()
    if not candidata or len(candidata) > _MAX_URL_ENLACE:
        return False
    try:
        partes = urlparse(candidata)
    except ValueError:
        return False
    return partes.scheme in ("http", "https") and bool(partes.netloc)


def construir_xlsx(filas: list[dict]) -> io.BytesIO:
    """
    XLSX en memoria a partir de `filas` (una por aviso, con las claves del export).

    Las columnas de COLUMNAS_EXTERNAS se limpian antes de escribir y se fuerzan a texto
    despues; "Apply Link" ademas se enlaza si la URL es http/https.
    """
    limpias = [
        {k: (limpiar_celda(v) if k in COLUMNAS_EXTERNAS else v) for k, v in fila.items()}
        for fila in filas
    ]
    df = pd.DataFrame(limpias)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as escritor:
        df.to_excel(escritor, index=False)
        hoja = escritor.sheets["Sheet1"]

        # Columna por nombre de encabezado, no por posicion: si el export cambia el
        # orden de las columnas, la defensa sigue apuntando a las correctas.
        indices = {
            celda.value: celda.column
            for celda in hoja[1]
            if celda.value in COLUMNAS_EXTERNAS
        }
        for fila in hoja.iter_rows(min_row=2):
            for celda in fila:
                if celda.column not in indices.values() or celda.value is None:
                    continue
                # openpyxl puso data_type "f" a toda cadena que empieza con "=". Con "s"
                # se escribe como cadena en linea: el texto es el mismo, deja de ser codigo.
                celda.data_type = "s"
                if celda.column == indices.get("Apply Link") and es_url_enlazable(celda.value):
                    celda.hyperlink = celda.value.strip()
                    celda.style = "Hyperlink"
    buf.seek(0)
    return buf
