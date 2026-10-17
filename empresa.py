"""
empresa.py — limpieza y control de calidad del campo `company`, en un solo lugar.

Por qué existe. El campo llega sucio desde el scraping de varias fuentes, y la
suciedad entraba al registry, a Notion y al Excel como si fuera un nombre:

  - título y empresa en el MISMO nodo: la tarjeta de python.org trae
    «NEW <título>\\n<empresa>» (o «<título>\\n<empresa>») y el selector devuelve
    todo el texto;
  - un eslogan o una descripción en lugar del nombre (jobspresso: el selector
    `span[class*='company']` matchea la línea de presentación de la empresa);
  - vacía o con el marcador «(empresa no listada)» cuando la fuente no la publica.

Dos funciones, ambas puras y deterministas:

  `normalizar_empresa(company, title)` rescata el nombre cuando el dato está y solo
  viene mezclado. Sobre un nombre limpio devuelve exactamente el mismo texto: ni el
  puntaje ni la deduplicación de un aviso ya limpio pueden cambiar.

  `motivo_empresa_dudosa(company)` dice POR QUÉ un valor no sirve como nombre, o None.
  Lo dudoso no se descarta: el aviso se conserva y se marca para revisión manual.
"""

from __future__ import annotations

import re
from typing import Optional

PLACEHOLDER_EMPRESA = "(empresa no listada)"

#: Largo desde el cual un nombre con forma de frase deja de ser creíble. Es el mismo
#: umbral que usa el front (`companyIsDirty`); el largo solo NO decide (hay nombres
#: legítimos de 70 caracteres), se combina con la forma del texto.
LARGO_SOSPECHOSO = 60

#: Más allá de este largo no hay nombre de empresa plausible, tenga la forma que tenga.
LARGO_MAXIMO = 120

# Códigos de motivo (van a la columna `reason` de la pila de revisión).
MOTIVO_VACIA = "empresa_dudosa_sin_empresa"
MOTIVO_ESLOGAN = "empresa_dudosa_eslogan"

#: Texto claro de cada código, para el panel y el Excel.
TEXTO_MOTIVO = {
    MOTIVO_VACIA: "La fuente no publica la empresa; el aviso se conserva sin ese dato",
    MOTIVO_ESLOGAN: "El campo empresa parece un eslogan o una descripción, no un nombre",
}

# Separadores que pegan título y empresa en una sola línea.
_SEPARADORES = r"[\s\-–—|·•:,/]+"

# Líneas que son solo una etiqueta de la tarjeta, no parte del nombre.
_LINEA_RUIDO = re.compile(
    r"^(?:new|nuevo|featured|destacado|hiring|urgent|remote|sponsored|"
    r"\d+\s+(?:open\s+)?(?:jobs?|roles?|positions?|openings?|vacantes?))$",
    re.IGNORECASE,
)

# Sufijos de scraping que NO son parte del nombre: «Acme · Remote», «Acme (5 jobs)».
_SUFIJO_RUIDO = re.compile(
    r"(?:\s*[·•|–—\-]\s*(?:remote|hiring|featured)"
    r"|\s*[·•|–—\-]?\s*\d+\s+(?:open\s+)?(?:jobs?|roles?|positions?|openings?))\s*$",
    re.IGNORECASE,
)
_SUFIJO_PARENTESIS = re.compile(
    r"\s*\(\s*(?:remote|\d+\s+(?:open\s+)?(?:jobs?|roles?|positions?|openings?))\s*\)\s*$",
    re.IGNORECASE,
)

# Palabras que en un nombre propio van en minúscula; no cuentan para la forma.
_NEXO = frozenset(
    "a an and at by de del di do du el en for in la las le les los of on or the to "
    "van von with y e & da das dos".split()
)

# Abreviaturas legales que terminan en punto sin cerrar una oración.
_ABREVIATURA = frozenset(
    "inc ltd llc llp lp co corp corps plc gmbh sa srl bv ag jr sr dr mr mrs ms st mt "
    "no vs etc intl dept univ assn assoc bros cia ltda sas spa".split()
)


def _clave(texto: str) -> str:
    return " ".join((texto or "").lower().split())


def _colapsar(texto: str) -> str:
    return " ".join((texto or "").replace(" ", " ").split())


def _quitar_ruido(nombre: str) -> str:
    anterior = None
    while anterior != nombre:
        anterior = nombre
        nombre = _SUFIJO_PARENTESIS.sub("", nombre)
        nombre = _SUFIJO_RUIDO.sub("", nombre)
    return nombre.strip(" \t-–—|·•:,/")


def normalizar_empresa(company: Optional[str], title: str = "") -> str:
    """Devuelve el nombre de empresa limpio, o "" si el texto no traía ninguno.

    Pasos, en orden, y cada uno es identidad sobre un nombre ya limpio:
      1. Parte por saltos de línea y descarta las líneas que son el título (con o sin
         una etiqueta «NEW» delante) o una etiqueta de la tarjeta.
      2. En cada línea con el título pegado al principio o al final
         («<título> - <empresa>»), lo quita. Si la línea ES el título, no hay empresa.
      3. Quita sufijos de scraping inequívocos («· Remote», «(12 jobs)»).
      4. Colapsa espacios.
    """
    if company is None:
        return ""
    # Mantener el texto adversarial intacto para auditarlo. La neutralización de
    # fórmulas pertenece al exportador, no a la normalización del dato de origen.
    if str(company).lstrip()[:1] in ("=", "+", "-", "@"):
        return str(company)
    bruto = str(company).replace(" ", " ")
    titulo_clave = _clave(title) if len(_clave(title)) >= 4 else ""

    lineas = [_colapsar(l) for l in re.split(r"[\r\n]+", bruto)]
    varias_lineas = len([l for l in lineas if l]) > 1
    conservadas = []
    for linea in lineas:
        if not linea or (varias_lineas and _LINEA_RUIDO.fullmatch(linea)):
            continue
        if not titulo_clave:
            conservadas.append(linea)
            continue
        if re.fullmatch(r"(?:new\s+)?" + re.escape(titulo_clave), _clave(linea)):
            continue
        patron_inicio = re.compile(
            r"^(?:new\s+)?" + re.escape(titulo_clave) + r"(?!\w)" + _SEPARADORES,
            re.IGNORECASE,
        )
        patron_fin = re.compile(
            _SEPARADORES + r"(?<!\w)" + re.escape(titulo_clave) + r"$", re.IGNORECASE
        )
        for patron in (patron_inicio, patron_fin):
            m = patron.search(linea)
            if m and len(linea) > m.end() - m.start():
                linea = _colapsar(patron.sub("", linea, count=1))
                break
        conservadas.append(linea)

    texto = _colapsar(" ".join(conservadas))

    sin_ruido = _quitar_ruido(texto)
    # Los separadores sobrantes (« - BSCI -») solo se recortan cuando hubo ruido real
    # que quitar: un nombre limpio vuelve idéntico.
    return sin_ruido if sin_ruido != texto.strip(" 	-–—|·•:,/") else texto


def _palabras_de_nombre(texto: str) -> list[str]:
    return [p for p in re.split(r"\s+", texto) if p and p.lower() not in _NEXO]


def _termina_en_oracion(texto: str) -> bool:
    """Termina en . ! ? y la última palabra no es una abreviatura legal."""
    if not re.search(r"[.!?]$", texto):
        return False
    ultima = texto.split()[-1].rstrip(".!?").lower()
    return ultima not in _ABREVIATURA and len(ultima) > 1


def _corte_de_oracion(texto: str) -> bool:
    """Un punto seguido de mayúscula, tras una palabra que no es abreviatura ni inicial."""
    for m in re.finditer(r"(\w+)[.!?]\s+[A-Z¿¡]", texto):
        previa = m.group(1)
        if len(previa) >= 3 and previa.lower() not in _ABREVIATURA:
            return True
    return False


def _parece_eslogan(texto: str) -> bool:
    palabras = texto.split()
    if len(palabras) >= 4 and (_termina_en_oracion(texto) or _corte_de_oracion(texto)):
        return True
    if len(texto) > LARGO_MAXIMO:
        return True
    if len(texto) > LARGO_SOSPECHOSO:
        nucleo = _palabras_de_nombre(texto)
        if nucleo:
            con_forma = sum(1 for p in nucleo if p[0].isupper() or p[0].isdigit() or not p[0].isalpha())
            if con_forma / len(nucleo) < 0.6:
                return True
    return False


def motivo_empresa_dudosa(company: Optional[str]) -> Optional[str]:
    """Código de motivo (`MOTIVO_*`) si `company` no sirve como nombre, o None.

    Se evalúa sobre el valor YA normalizado. Un nombre legítimo y largo
    («Patrick J. McGovern Foundation», «Department of Customer Services, Open Data and
    Small and Family Business») da None: el largo solo no condena, la forma sí.
    """
    c = _colapsar(company or "")
    if not c or c.lower() == PLACEHOLDER_EMPRESA:
        return MOTIVO_VACIA
    if _parece_eslogan(c):
        return MOTIVO_ESLOGAN
    return None
