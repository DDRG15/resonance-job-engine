"""
requisitos.py — que pide un aviso y cuanto de eso tiene el CV, con evidencia.

Por que existe: `matcher._score_skill_overlap` buscaba las
skills del perfil en titulo + empresa + salario. Un aviso cuya descripcion pide Python y
FastAPI no sumaba nada por eso si el titulo no los nombraba: el matcher leia la tapa del
libro. Ahora, cuando la fuente trae descripcion, se leen los requisitos.

Diseño, en tres reglas:

1. **Deterministico.** Vocabulario = skills del perfil + una lista curada de
   tecnologias. Para cada termino presente en la descripcion: `match` si tambien esta en
   el CV, `missing` si no, con la frase de la descripcion como evidencia. Mismo texto y
   mismo perfil, mismo resultado: `tools/repuntuar_registry.py` sigue siendo fiel.
2. **Requerido o deseable.** Un termino que el aviso marca como "nice to have",
   "bonus", "preferred", "deseable" —en su misma oracion o bajo un titulo de seccion
   asi— pesa la mitad: no saber algo deseable no es lo mismo que no saber algo
   requerido. La señal no cruza el fin de la oracion (ver `_peso`).
3. **El LLM no participa.** La capa LLM del match esta apagada y no tiene codigo en este
   modulo: si se prende, no puede cambiar lo que dijo el deterministico ni inventar
   evidencia (solo sumar requisitos implicitos, que no cuentan para la cobertura).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable, Optional

#: Tecnologias que un aviso de backend/datos puede pedir. Canonico -> alias (regex sobre
#: texto en minusculas). Solo terminos que identifican una tecnologia sin ambiguedad:
#: "go" queda afuera porque es un verbo en cada descripcion; "golang" no.
VOCABULARIO: dict[str, tuple[str, ...]] = {
    "Python": (r"python",),
    "FastAPI": (r"fastapi",),
    "Django": (r"django",),
    "Flask": (r"flask",),
    "Pydantic": (r"pydantic",),
    "SQLAlchemy": (r"sqlalchemy",),
    "Celery": (r"celery",),
    "asyncio": (r"asyncio", r"async\s*/\s*await"),
    "pytest": (r"pytest",),
    "PostgreSQL": (r"postgres(?:ql)?",),
    "MySQL": (r"mysql",),
    "SQLite": (r"sqlite",),
    "MongoDB": (r"mongo(?:db)?",),
    # "Oracle" a secas es tambien la empresa: se exige que hable de la base de datos.
    "Oracle DB": (r"oracle\s+(?:db|database|sql)", r"pl\s*/\s*sql"),
    "DynamoDB": (r"dynamo\s*db",),
    "Cassandra": (r"cassandra",),
    "Redis": (r"redis",),
    "Elasticsearch": (r"elastic\s?search", r"opensearch"),
    "Kafka": (r"kafka",),
    "RabbitMQ": (r"rabbitmq",),
    "SQL": (r"sql",),
    "Docker": (r"docker",),
    "Kubernetes": (r"kubernetes", r"k8s"),
    "Terraform": (r"terraform",),
    "Ansible": (r"ansible",),
    "Jenkins": (r"jenkins",),
    "Datadog": (r"datadog",),
    "AWS": (r"aws", r"amazon web services"),
    "GCP": (r"gcp", r"google cloud"),
    "Azure": (r"azure",),
    "Linux": (r"linux",),
    "Git": (r"git",),
    "CI/CD": (r"ci\s*/\s*cd", r"continuous integration"),
    "GitHub Actions": (r"github actions",),
    "JavaScript": (r"javascript",),
    "TypeScript": (r"typescript",),
    # "react" es tambien un verbo ("react to incidents", comun en avisos de SRE): se
    # exige la mayuscula —scoped, (?-i:)— y que no siga "to".
    "React": (r"react(?:\.js|js)", r"(?-i:React)(?!\s+to)"),
    "Node.js": (r"node\.?js",),
    "Golang": (r"golang",),
    "Java": (r"java(?!script)",),
    # "spring" a secas es una estacion ("Spring 2027 internship"): se exige el framework.
    "Spring": (r"spring\s*boot", r"spring\s+framework", r"spring\s+(?:mvc|cloud|data)"),
    "Kotlin": (r"kotlin",),
    "Scala": (r"scala",),
    ".NET": (r"\.net(?:\s*core)?", r"asp\.net"),
    # "swift" es tambien un adjetivo ("swift decisions"): mayuscula exigida, como React.
    "Swift": (r"swiftui", r"(?-i:Swift)(?!\s+(?:and|to)\b)"),
    "Angular": (r"angular(?:js)?",),
    "Vue": (r"vue(?:\.js|js)?",),
    "Next.js": (r"next\.?js",),
    "Rust": (r"rust",),
    "C++": (r"c\+\+",),
    "C#": (r"c#",),
    "Ruby": (r"ruby",),
    "PHP": (r"php",),
    "GraphQL": (r"graphql",),
    "REST": (r"rest(?:ful)?\s+apis?", r"restful"),
    "gRPC": (r"grpc",),
    "Microservicios": (r"micro-?services?",),
    "Airflow": (r"airflow",),
    "Spark": (r"spark",),
    "Pandas": (r"pandas",),
    "dbt": (r"dbt",),
    "Snowflake": (r"snowflake",),
    "BigQuery": (r"bigquery",),
    "Databricks": (r"databricks",),
    "Hadoop": (r"hadoop",),
    "PyTorch": (r"pytorch",),
    "TensorFlow": (r"tensorflow",),
    "LLM": (r"llms?", r"large language models?"),
    "LangChain": (r"langchain",),
    "Playwright": (r"playwright",),
    "Selenium": (r"selenium",),
    "Prometheus": (r"prometheus",),
    "Grafana": (r"grafana",),
    "OpenAPI": (r"openapi", r"swagger"),
}

#: Señales de "deseable" que bajan el peso de un termino. "not required" y "no
#: excluyente" son deseables: niegan la exigencia, no la afirman.
_DESEABLE = re.compile(
    r"nice[\s-]to[\s-]haves?|bonus|preferred|preferably|is a plus|are a plus|a plus\b|"
    r"pluses|desirable|not required|not a requirement|no excluyente|"
    r"deseables?|valorables?|se valorar[aá]|plus\b|ideally|would be great|extra credit",
    re.IGNORECASE,
)

#: Señales de "requerido" dentro de la misma oracion. Sirven para cortar el alcance de
#: una señal de deseable que viene DESPUES del termino: en "Python is required, Docker
#: is a plus", el "a plus" es de Docker, no de Python. Las negaciones ("not required",
#: "no excluyente") no cuentan: ya las toma `_DESEABLE`.
_REQUERIDO = re.compile(
    r"(?<!not )(?<!n't )(?<!no )"
    r"\b(?:required|requires?|must|mandatory|essential|requisitos?|excluyentes?|obligatori[oa]s?)\b",
    re.IGNORECASE,
)

#: Fin de oracion. El punto cuenta solo seguido de espacio o fin de texto, para no
#: partir "Node.js" ni "3.5 years"; y no despues de "e.g", "i.e" o "etc", que en un
#: aviso casi siempre preceden a la lista de tecnologias ("streaming (e.g. Kafka)").
_FIN_ORACION = re.compile(r"(?<!e\.g)(?<!i\.e)(?<!etc)[.!?](?=\s|$)|[;\n•]")

#: Encabezados de seccion. Un aviso con formato pone los deseables bajo su propio titulo
#: ("Nice to have", "Preferred qualifications") y cada termino de esa lista vive en su
#: propia linea, sin la señal al lado: la oracion sola no alcanza para saber su peso.
_ENCABEZADO = re.compile(
    r"(?:requirements?|required|must[\s-]haves?|(?:minimum|basic|required)\s+qualifications|"
    r"qualifications|skills|what\s+you(?:'ll|\s+will)?\s+(?:need|bring|have)|"
    r"what\s+we(?:'re|\s+are)?\s+looking\s+for|about\s+you|you\s+have|who\s+you\s+are|"
    r"responsibilities|what\s+you(?:'ll|\s+will)\s+do|about\s+the\s+(?:role|job|position)|"
    r"requisitos|requerimientos|responsabilidades|lo\s+que\s+buscamos)\b",
    re.IGNORECASE,
)
#: Secciones que describen a la empresa o lo que ofrece, no lo que pide. "About us: we
#: run on Kubernetes" no es un requisito del puesto; un termino que SOLO aparece en
#: secciones asi no entra a la lista.
_ENCABEZADO_CONTEXTO = re.compile(
    r"(?:benefits|perks|what\s+we\s+offer|compensation|about\s+(?:us|the\s+(?:team|company))|"
    r"who\s+we\s+are|our\s+(?:team|company|mission|stack)|beneficios|ofrecemos|"
    r"qui[eé]nes\s+somos|sobre\s+nosotros)\b",
    re.IGNORECASE,
)
#: Un encabezado sin dos puntos es una linea corta que EMPIEZA con una de las frases de
#: arriba. Mas palabras que esto ya es un item de la lista ("Bonus points if you know
#: Rust and have shipped a compiler"), no un titulo.
_ENCABEZADO_MAX_PALABRAS = 5
#: Con dos puntos al final la linea anuncia una lista aunque no use una frase conocida
#: ("It would be great if you also have:"), pero hasta este largo.
_ENCABEZADO_CON_DOS_PUNTOS_MAX_PALABRAS = 10

#: Menos de esto no es señal: un aviso que nombra dos tecnologias no dice cuanto
#: del puesto cubre el CV.
MIN_REQUISITOS = 3

PESO = {"requerido": 1.0, "deseable": 0.5}


@dataclass(frozen=True)
class Requisito:
    requisito: str
    estado: str          # "match" | "missing" | "implicito" (solo LLM, no cuenta)
    peso: str            # "requerido" | "deseable"
    evidencia: str       # la frase de la DESCRIPCION; nunca del CV

    def to_dict(self) -> dict:
        return asdict(self)


def _patron(alias: str) -> re.Pattern:
    # Bordes que funcionan con "c++", "c#", "node.js" y "ci/cd": no hay \b despues de
    # "+" o "#", asi que el borde es "no seguido de letra o digito".
    return re.compile(rf"(?<![a-z0-9]){alias}(?![a-z0-9])", re.IGNORECASE)


def _vocabulario(perfil: Optional[dict]) -> dict[str, tuple[re.Pattern, ...]]:
    """La lista curada mas las skills del perfil que no esten ya en ella."""
    voc = {k: tuple(_patron(a) for a in v) for k, v in VOCABULARIO.items()}
    for skill in (perfil or {}).get("core_skills", []) or []:
        s = str(skill).strip()
        if not s or _canonico_de(s, voc):
            continue
        voc[s] = (_patron(re.escape(s.lower())),)
    return voc


def _canonico_de(texto: str, voc: dict) -> Optional[str]:
    """El canonico que ya cubre una skill del perfil, o None. "Docker Compose" lo cubre
    Docker y "pytest-xdist" lo cubre pytest; "circuit breaker" no lo cubre nadie y entra
    al vocabulario como termino propio. El tope de largo evita que una frase larga del
    perfil que menciona una tecnologia de pasada se de por cubierta."""
    t = texto.lower()
    for canon, pats in voc.items():
        if len(t) <= len(canon) + 12 and any(p.search(t) for p in pats):
            return canon
    return None


def skills_del_cv(perfil: Optional[dict], voc: Optional[dict] = None) -> set[str]:
    """Los canonicos que el CV declara. Una skill del perfil cuenta como el canonico
    cuyo alias la nombra ("Docker Compose" -> Docker, "PostgreSQL" -> PostgreSQL)."""
    voc = voc or _vocabulario(perfil)
    salida: set[str] = set()
    for skill in (perfil or {}).get("core_skills", []) or []:
        s = str(skill).strip().lower()
        for canon, pats in voc.items():
            if any(p.search(s) for p in pats):
                salida.add(canon)
    return salida


def _limites_oracion(texto: str, inicio: int, fin: int) -> tuple[int, int]:
    """[a, b) de la oracion que contiene texto[inicio:fin], segun `_FIN_ORACION`."""
    a = 0
    for m in _FIN_ORACION.finditer(texto, 0, inicio):
        a = m.end()
    m = _FIN_ORACION.search(texto, fin)
    return a, (m.start() if m else len(texto))


def _frase(texto: str, inicio: int, fin: int, largo: int = 200) -> str:
    """La oracion que contiene el match, recortada alrededor de el."""
    a, b = _limites_oracion(texto, inicio, fin)
    frase = texto[a:b].strip()
    if len(frase) > largo:
        centro = inicio - a
        desde = max(0, centro - largo // 2)
        frase = ("…" if desde else "") + frase[desde:desde + largo].strip() + "…"
    return frase


def _encabezado(linea: str) -> Optional[str]:
    """
    "deseable", "requerido" o "contexto" si la linea es un titulo de seccion; None si es
    contenido.

    Titulo es una linea corta que empieza con una frase de seccion conocida, o una linea
    que termina en dos puntos. Un titulo cuyo texto es una señal de deseable abre una
    seccion deseable ("Nice to have", "Preferred qualifications"); uno de empresa o
    beneficios abre una de contexto; cualquier otro abre una requerida. Cada titulo
    cierra la seccion anterior: "Benefits" despues de "Nice to have" no hereda el peso.
    """
    nucleo = linea.strip().lstrip("-*•·–—>#").strip()
    if not nucleo:
        return None
    con_dos_puntos = nucleo.endswith(":")
    nucleo = nucleo.rstrip(":").strip()
    if ":" in nucleo:
        # "Nice to have: Kafka" es un rotulo en linea, no un titulo: lo resuelve la regla
        # de la oracion, y su peso no se extiende a las lineas que siguen.
        return None
    palabras = len(nucleo.split())
    if con_dos_puntos and palabras <= _ENCABEZADO_CON_DOS_PUNTOS_MAX_PALABRAS:
        if _DESEABLE.search(nucleo):
            return "deseable"
        return "contexto" if _ENCABEZADO_CONTEXTO.match(nucleo) else "requerido"
    if palabras <= _ENCABEZADO_MAX_PALABRAS:
        if _DESEABLE.match(nucleo):
            return "deseable"
        if _ENCABEZADO_CONTEXTO.match(nucleo):
            return "contexto"
        if _ENCABEZADO.match(nucleo):
            return "requerido"
    return None


def _secciones(texto: str) -> list[tuple[int, str]]:
    """(posicion, peso) de cada titulo de seccion, en orden."""
    salida: list[tuple[int, str]] = []
    pos = 0
    for linea in texto.split("\n"):
        peso = _encabezado(linea)
        if peso:
            salida.append((pos, peso))
        pos += len(linea) + 1
    return salida


def _peso(texto: str, inicio: int, fin: int, secciones: list[tuple[int, str]]) -> str:
    """
    "deseable" o "requerido" para el termino en texto[inicio:fin]. Tres reglas, en orden:

    1. Una señal de deseable ANTES del termino en la misma oracion lo alcanza, salvo que
       entre los dos haya una de requerido: "Nice to have: Kafka, Redis" alcanza a los
       dos; "Bonus: Rust. Python is required" no alcanza a Python, porque el punto corta.
    2. Una señal DESPUES del termino en la misma oracion lo alcanza solo si llega antes
       que una de requerido: "Kafka is a plus" si; en "Python is required, Docker is a
       plus" el "a plus" es de Docker.
    3. Una señal de requerido en la oracion gana sobre la seccion: "Python is required"
       es requerido aunque el aviso lo haya escrito debajo de "Nice to have".
    4. Sin señal en la oracion, manda la seccion: el titulo mas cercano por encima.
       Sin titulo, requerido. Bajo un titulo de empresa o beneficios, "contexto": no es
       un requisito (lo decide `extraer`).

    Lo que esto reemplaza: una ventana fija de 160 caracteres hacia atras y 40 hacia
    adelante. Cruzaba el punto, y "Kubernetes is required. Nice to have: ..." salia
    deseable.
    """
    a, b = _limites_oracion(texto, inicio, fin)
    antes = texto[a:inicio]
    ultima = None
    for ultima in _DESEABLE.finditer(antes):
        pass
    if ultima is not None and not _REQUERIDO.search(antes, ultima.end()):
        return "deseable"

    despues = texto[fin:b]
    des = _DESEABLE.search(despues)
    req = _REQUERIDO.search(despues)
    if des is not None and (req is None or des.start() < req.start()):
        return "deseable"
    if req is not None or _REQUERIDO.search(antes):
        return "requerido"

    peso = "requerido"
    for pos, p in secciones:
        if pos > inicio:
            break
        peso = p
    return peso


def extraer(descripcion: Optional[str], perfil: Optional[dict],
            empresa: Optional[str] = None) -> list[Requisito]:
    """
    Los requisitos que la descripcion nombra, con su estado contra el CV y su evidencia.

    Sin descripcion no hay requisitos: una lista vacia, no una lista de "missing". Sin
    perfil se extraen igual, todos `missing`: sin CV no hay contra que comparar, y la
    cobertura no se calcula (ver `cobertura`).

    `empresa`: un termino que es el nombre de la empresa no es un requisito. Un aviso de
    Grafana Labs o de MongoDB nombra su producto en cada parrafo, y eso no dice que el
    puesto lo pida: contarlo seria un `missing` seguro para casi cualquier CV.

    El peso de un termino que aparece varias veces es el de su aparicion mas exigente:
    si una vez figura como requerido, es requerido, aunque otra lo nombre como deseable.
    Un termino que solo aparece en secciones de contexto (empresa, beneficios) no entra.
    """
    texto = (descripcion or "").strip()
    if not texto:
        return []
    voc = _vocabulario(perfil)
    cv = skills_del_cv(perfil, voc)
    nombre_empresa = (empresa or "").strip().lower()
    secciones = _secciones(texto)
    salida: list[Requisito] = []
    posicion: dict[str, int] = {}
    for canon, pats in voc.items():
        if nombre_empresa and any(p.search(nombre_empresa) for p in pats):
            continue
        apariciones = sorted((m for p in pats for m in p.finditer(texto)),
                             key=lambda m: m.start())
        if not apariciones:
            continue
        pesos = [(m, _peso(texto, m.start(), m.end(), secciones)) for m in apariciones]
        elegido = (next((x for x in pesos if x[1] == "requerido"), None)
                   or next((x for x in pesos if x[1] == "deseable"), None))
        if elegido is None:
            continue    # solo en secciones de contexto
        primero, peso = elegido
        posicion[canon] = primero.start()
        salida.append(Requisito(
            requisito=canon,
            estado="match" if canon in cv else "missing",
            peso=peso,
            evidencia=_frase(texto, primero.start(), primero.end()),
        ))
    # Orden estable para la UI y para comparar corridas: requeridos primero, y dentro de
    # cada grupo, por donde aparecen en el texto.
    return sorted(salida, key=lambda r: (r.peso != "requerido", posicion[r.requisito]))


def cobertura(requisitos: Iterable[Requisito], perfil: Optional[dict]) -> Optional[float]:
    """
    Fraccion ponderada de los requisitos del aviso que el CV cubre, 0..1, o None si no
    hay señal suficiente: sin perfil, o menos de `MIN_REQUISITOS` requisitos. None quiere
    decir "el eje no participa", igual que el salario cuando no hay dato.
    """
    if not perfil or not (perfil.get("core_skills") or []):
        return None
    reqs = [r for r in requisitos if r.estado in ("match", "missing")]
    if len(reqs) < MIN_REQUISITOS:
        return None
    total = sum(PESO[r.peso] for r in reqs)
    cubierto = sum(PESO[r.peso] for r in reqs if r.estado == "match")
    return cubierto / total if total else None
