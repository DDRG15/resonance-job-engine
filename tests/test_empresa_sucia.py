"""
tests/test_empresa_sucia.py — el campo `company` llega sucio desde algunas fuentes.

Casos reales, con estructuras sintéticas:
  - título y empresa en el mismo nodo (python.org): «NEW <título>\\n<empresa>»;
  - un eslogan o una descripción en lugar del nombre (jobspresso);
  - vacía o con el marcador «(empresa no listada)».

La tarjeta de python.org y las de Jobspresso también se prueban con HTML capturado.

Contrato: lo rescatable se rescata (`normalizar_empresa`), lo que sigue dudoso NO se
descarta —el aviso queda en el registry— y se anota en la pila de revisión con su motivo.
Un nombre ya limpio, aunque largo, vuelve idéntico.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

import selectors_registry as sr
from database import GemaDatabase
from empresa import (
    MOTIVO_ESLOGAN, MOTIVO_VACIA, PLACEHOLDER_EMPRESA, TEXTO_MOTIVO,
    motivo_empresa_dudosa, normalizar_empresa,
)
from exportar_excel import _MOTIVOS_PILA
from models import JobResult
from scraper import GemaScraper, derive_company_from_url

TITULO = "Senior Backend Engineer (Data Platform)"

NOMBRES_LEGITIMOS = [
    "Patrick J. McGovern Foundation",
    "Department of Customer Services, Open Data and Small and Family Business",
    "ForFutures Financial, Planning, a financial advisory practice of Ameriprise Financial Services LLC",
    "Caribbean Bottling Company (Bahamas) Ltd.",
    "International Society of Antimicrobial Chemotherapy",
    "Dobinsons Spring & Suspension - Middle East & Africa",
    "Blanco Silva Consultoría Informática - BSCI -",
    "National Legal Aid & Defender Association",
    "Remote",
    "Acme Remote Systems",
    "OnePointFive",
]

ESLOGANES = [
    "Software for researchers and students. It's like Gmail for your papers.",
    "Automation that moves you forward. Zapier connects all the apps you use for work.",
    "Our mission is to make mental healthcare work for everyone.",
    "Award-winning open source, private cloud Slack-alternative.",
    "Change is easier with the right tools - navigate your engineering teams through it with Acme",
    "Digitizing the world of physical operations to increase the efficiency and safety of operations",
]


# ---------------------------------------------------------------------------
# normalizar_empresa
# ---------------------------------------------------------------------------

class TestNormalizar:
    def test_titulo_y_empresa_en_lineas_con_etiqueta_new(self):
        assert normalizar_empresa(f"NEW {TITULO}\nIllumen Group Inc.", TITULO) == "Illumen Group Inc."

    def test_titulo_y_empresa_en_lineas_sin_etiqueta(self):
        assert normalizar_empresa(f"{TITULO}\nMyDataValue", TITULO) == "MyDataValue"

    def test_titulo_con_otro_caso_y_espacios(self):
        assert normalizar_empresa(f"  {TITULO.upper()}  \n  Kappa   Works ", TITULO) == "Kappa Works"

    def test_titulo_pegado_en_la_misma_linea(self):
        assert normalizar_empresa(f"{TITULO} - Acme Labs", TITULO) == "Acme Labs"
        assert normalizar_empresa(f"Acme Labs | {TITULO}", TITULO) == "Acme Labs"

    def test_rescata_la_empresa_en_una_linea_mixta_con_etiquetas(self):
        assert normalizar_empresa(f"{TITULO} - Acme Labs\nFeatured", TITULO) == "Acme Labs"
        assert normalizar_empresa(f"NEW {TITULO}", TITULO) == ""

    def test_no_borra_el_titulo_cuando_es_parte_del_nombre(self):
        nombre = "The Backend Engineer Company"
        assert normalizar_empresa(f"{nombre}\nRemote", "Backend Engineer") == nombre

    @pytest.mark.parametrize("valor", ['=HYPERLINK("http://evil","x")', '+cmd', '-1+1', '@SUM(A1)'])
    def test_no_oculta_texto_adversarial_igual_al_titulo(self, valor):
        assert normalizar_empresa(valor, valor) == valor

    def test_si_el_campo_es_solo_el_titulo_no_hay_empresa(self):
        assert normalizar_empresa(TITULO, TITULO) == ""
        assert normalizar_empresa(f"NEW {TITULO}\nNEW", TITULO) == ""

    def test_ruido_de_scraping_inequivoco(self):
        assert normalizar_empresa("Acme Labs · Remote") == "Acme Labs"
        assert normalizar_empresa("Acme Labs (12 jobs)") == "Acme Labs"
        assert normalizar_empresa("Acme Labs - 5 open positions") == "Acme Labs"

    def test_etiqueta_en_linea_propia_se_descarta(self):
        assert normalizar_empresa("Acme Labs\nRemote") == "Acme Labs"

    @pytest.mark.parametrize("nombre", NOMBRES_LEGITIMOS)
    def test_nombre_limpio_vuelve_identico(self, nombre):
        assert normalizar_empresa(nombre, TITULO) == nombre

    def test_un_titulo_corto_no_se_busca_dentro_del_nombre(self):
        # «Dev» es una palabra del nombre, no un título pegado.
        assert normalizar_empresa("Dev Society Group", "Dev") == "Dev Society Group"

    def test_idempotente(self):
        una = normalizar_empresa(f"NEW {TITULO}\nIllumen Group Inc.", TITULO)
        assert normalizar_empresa(una, TITULO) == una

    def test_none_y_vacio(self):
        assert normalizar_empresa(None) == ""
        assert normalizar_empresa("   ") == ""


# ---------------------------------------------------------------------------
# motivo_empresa_dudosa
# ---------------------------------------------------------------------------

class TestDudosa:
    @pytest.mark.parametrize("nombre", NOMBRES_LEGITIMOS)
    def test_nombres_legitimos_no_son_dudosos(self, nombre):
        assert motivo_empresa_dudosa(nombre) is None

    @pytest.mark.parametrize("eslogan", ESLOGANES)
    def test_eslogan(self, eslogan):
        assert motivo_empresa_dudosa(eslogan) == MOTIVO_ESLOGAN

    @pytest.mark.parametrize("vacia", ["", "   ", None, PLACEHOLDER_EMPRESA, "(Empresa no listada)"])
    def test_vacia_o_marcador(self, vacia):
        assert motivo_empresa_dudosa(vacia) == MOTIVO_VACIA

    def test_todo_motivo_tiene_texto_claro_en_el_excel(self):
        for codigo, texto in TEXTO_MOTIVO.items():
            assert _MOTIVOS_PILA[codigo] == texto and texto.strip()


# ---------------------------------------------------------------------------
# JobResult: el modelo aplica la normalización a toda fuente
# ---------------------------------------------------------------------------

def _job(company, title=TITULO, url="https://example.com/j/1", dominio="example.com"):
    return JobResult(title=title, company=company, url=url, source_domain=dominio)


class TestJobResult:
    def test_titulo_pegado_se_separa(self):
        assert _job(f"NEW {TITULO}\nIllumen Group Inc.").company == "Illumen Group Inc."

    def test_campo_igual_al_titulo_queda_como_marcador(self):
        assert _job(TITULO).company == PLACEHOLDER_EMPRESA

    @pytest.mark.parametrize("nombre", NOMBRES_LEGITIMOS)
    def test_empresa_limpia_no_cambia(self, nombre):
        assert _job(nombre).company == nombre

    def test_eslogan_se_conserva_para_revisar(self):
        # No se inventa ni se borra: el valor queda y se marca más abajo.
        assert _job(ESLOGANES[0]).company == ESLOGANES[0]

    def test_empresa_vacia_sigue_aceptada(self):
        assert _job("").company == ""
        assert _job(None).company == ""
        assert _job(None).empresa_no_confirmada is True

    def test_calidad_serializada_con_el_mismo_criterio_de_revision(self):
        assert _job(NOMBRES_LEGITIMOS[1]).model_dump()["empresa_no_confirmada"] is False
        assert _job(ESLOGANES[0]).model_dump()["empresa_no_confirmada"] is True


# ---------------------------------------------------------------------------
# Extracción de la tarjeta (camino de los boards por navegador)
# ---------------------------------------------------------------------------

class _FakeEl:
    def __init__(self, text):
        self._text = text

    async def inner_text(self):
        return self._text

    async def get_attribute(self, _attr):
        return None


class _FakeCard:
    def __init__(self, campos):
        self._campos = campos

    async def query_selector(self, selector):
        return _FakeEl(self._campos[selector]) if selector in self._campos else None


def _extraer(sel, campos, url, dominio):
    s = GemaScraper.__new__(GemaScraper)
    return asyncio.run(s._extract_card_metadata(_FakeCard(campos), url, dominio, sel))


class TestTarjeta:
    @pytest.mark.parametrize("title,company", [
        ("Artie (YC S23) Is Hiring Enterprise AES and Senior Engineers", "Artie"),
        ("Flexport Is Hiring a Biz Ops Leader in Office of CEO", "Flexport"),
        ("Piramidal (YC W24) Is Hiring", "Piramidal"),
    ])
    def test_hn_empresa_en_titulo_no_el_host_del_ats(self, title, company):
        sel = sr.SELECTORS["news.ycombinator.com"]
        result = _extraer(sel, {sel.title[0]: title, sel.company[0]: "greenhouse.io"},
                          "https://job-boards.greenhouse.io/piramidalinc/jobs/4443102009",
                          "news.ycombinator.com")
        assert result.company == company

    def test_arc_exclusive_es_una_etiqueta_del_board_y_conserva_el_aviso(self):
        sel = sr.SELECTORS["arc.dev"]
        result = _extraer(sel, {sel.title[0]: "AI Integration Engineer",
                               sel.company[0]: "Arc Exclusive"},
                          "https://arc.dev/remote-jobs/details/ai-integration-engineer-id", "arc.dev")
        assert result is not None
        assert result.company == PLACEHOLDER_EMPRESA
        assert result.empresa_no_confirmada

    def test_tarjeta_python_org_html_real(self, tmp_path):
        from test_location_jobspresso_arc import _extraer as extraer_html
        # Tarjeta pública capturada de https://www.python.org/jobs/ el 2026-10-17.
        html = Path(__file__).with_name("fixtures").joinpath("company/python_org.html").read_text(encoding="utf-8")
        jobs = asyncio.run(extraer_html(f'<ol class="list-recent-jobs">{html}</ol>', "python.org", tmp_path))
        assert len(jobs) == 1
        assert jobs[0].title == "Software Engineer - Full Stack"
        assert jobs[0].company == "3Gimbals"
        assert jobs[0].empresa_no_confirmada is False

    def test_python_org_titulo_y_empresa_en_el_mismo_nodo(self):
        sel = sr.SELECTORS["python.org"]
        job = _extraer(sel, {
            sel.title[0]: TITULO,
            sel.company[0]: f"NEW {TITULO}\nIllumen Group Inc.",
        }, "https://www.python.org/jobs/1", "python.org")
        assert job.company == "Illumen Group Inc."
        assert "\n" not in job.company

    def test_nodo_solo_con_titulo_cae_a_la_url_del_ats(self):
        sel = sr.SELECTORS["posthog.com"]
        job = _extraer(sel, {sel.title[0]: "Backend Engineer"},
                       "https://jobs.gem.com/acme-labs/abc123?utm_source=x", "posthog.com")
        assert job.company == "Acme Labs"

    def test_gem_en_la_derivacion_de_url(self):
        assert derive_company_from_url("https://jobs.gem.com/acme/id1", "posthog.com") == "Acme"

    def test_selector_de_jobspresso_apunta_al_nombre_y_no_al_eslogan(self):
        sel = sr.SELECTORS["jobspresso.co"]
        assert sel.company[0] == ".job_listing-company strong"


# ---------------------------------------------------------------------------
# Registry y pila de revisión
# ---------------------------------------------------------------------------

def _db(tmp_path):
    return GemaDatabase(db_path=str(tmp_path / "e.db"))


class TestRegistroYRevision:
    def test_eslogan_se_conserva_y_va_a_revision(self, tmp_path):
        db = _db(tmp_path)
        j = _job(ESLOGANES[0], url="https://example.com/j/eslogan")
        db.mark_seen_batch([j])
        assert db.is_seen(j.url)[0] is True            # el aviso NO se descartó
        pila = db.get_manual_review()
        assert [r["reason"] for r in pila] == [MOTIVO_ESLOGAN]
        assert pila[0]["original_url"] == j.url and pila[0]["company"] == ESLOGANES[0]

    def test_vacia_se_conserva_y_va_a_revision(self, tmp_path):
        db = _db(tmp_path)
        j = _job(PLACEHOLDER_EMPRESA, url="https://example.com/j/vacia")
        db.mark_seen(j)
        assert db.is_seen(j.url)[0] is True
        assert [r["reason"] for r in db.get_manual_review()] == [MOTIVO_VACIA]

    def test_empresa_limpia_no_entra_a_la_pila(self, tmp_path):
        db = _db(tmp_path)
        db.mark_seen_batch([_job(n, url=f"https://example.com/j/{i}")
                            for i, n in enumerate(NOMBRES_LEGITIMOS)])
        assert db.get_manual_review() == []

    def test_marcar_dos_veces_no_duplica(self, tmp_path):
        db = _db(tmp_path)
        j = _job(ESLOGANES[1], url="https://example.com/j/dup")
        db.mark_seen_batch([j])
        db.mark_seen_batch([j])
        assert len(db.get_manual_review()) == 1

    def test_empresas_desconocidas_con_urls_distintas_no_se_agrupan(self, tmp_path):
        db = _db(tmp_path)
        jobs = [_job(PLACEHOLDER_EMPRESA, url=f"https://example.com/jobs/{i}") for i in (1, 2)]
        db.mark_seen_batch(jobs)
        assert {r["original_url"] for r in db.get_manual_review()} == {j.url for j in jobs}
        first = db.get_manual_review()[0]
        db.resolve_manual_review(first["review_hash"], None)
        db.mark_seen_batch(jobs)
        assert len(db.get_manual_review()) == 1
        assert len(db.get_manual_review(include_resolved=True)) == 2

    def test_la_revision_de_empresa_no_oculta_la_revision_de_url(self, tmp_path):
        db = _db(tmp_path)
        job = _job(PLACEHOLDER_EMPRESA)
        db.add_to_manual_review(job, "https://example.com/jobs", "url_no_verificable_titulo_real")
        db.mark_seen(job)
        assert {r["reason"] for r in db.get_manual_review()} == {MOTIVO_VACIA, "url_no_verificable_titulo_real"}


class TestRepararRegistro:
    def _sembrar_sucio(self, db, filas):
        """Filas como las dejó el scraper viejo: sin pasar por el validador."""
        con = sqlite3.connect(db.db_path)
        for i, (titulo, empresa, url, dominio) in enumerate(filas):
            con.execute(
                "INSERT INTO seen_jobs_registry (job_hash, url, title, company, source_domain, scraped_at)"
                " VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
                (db.compute_hash(url), url, titulo, empresa, dominio),
            )
        con.commit()
        con.close()

    def _filas(self):
        return [
            (TITULO, f"NEW {TITULO}\nIllumen Group Inc.", "https://py.example/1", "py.example"),
            (TITULO, ESLOGANES[0], "https://js.example/2", "js.example"),
            ("Backend Engineer", PLACEHOLDER_EMPRESA, "https://jobs.gem.com/acme/3", "posthog.com"),
            ("Backend Engineer", PLACEHOLDER_EMPRESA, "https://arc.example/4", "arc.example"),
            (TITULO, "Patrick J. McGovern Foundation", "https://ok.example/5", "ok.example"),
        ]

    def test_dry_run_no_escribe(self, tmp_path):
        db = _db(tmp_path)
        self._sembrar_sucio(db, self._filas())
        r = db.reparar_empresas(aplicar=False)
        assert r == {"filas": 5, "corregidas": 2, "dudosas": 2}
        assert db.get_manual_review() == []
        con = sqlite3.connect(db.db_path)
        assert con.execute("SELECT company FROM seen_jobs_registry WHERE url='https://py.example/1'"
                           ).fetchone()[0].startswith("NEW ")
        con.close()

    def test_aplicar_corrige_marca_y_no_borra(self, tmp_path):
        db = _db(tmp_path)
        self._sembrar_sucio(db, self._filas())
        db.reparar_empresas(aplicar=True)
        con = sqlite3.connect(db.db_path)
        empresas = dict(con.execute("SELECT url, company FROM seen_jobs_registry").fetchall())
        con.close()
        assert len(empresas) == 5
        assert empresas["https://py.example/1"] == "Illumen Group Inc."
        assert empresas["https://jobs.gem.com/acme/3"] == "Acme"
        assert empresas["https://ok.example/5"] == "Patrick J. McGovern Foundation"
        motivos = sorted(r["reason"] for r in db.get_manual_review())
        assert motivos == sorted([MOTIVO_ESLOGAN, MOTIVO_VACIA])
        # segunda pasada: nada que corregir, la pila no crece
        assert db.reparar_empresas(aplicar=True)["corregidas"] == 0
        assert len(db.get_manual_review()) == 2

    def test_solo_las_empresas_corregidas_quedan_pendientes_de_repuntuar(self, tmp_path):
        db = _db(tmp_path)
        self._sembrar_sucio(db, self._filas())
        with sqlite3.connect(db.db_path) as conn:
            conn.execute("UPDATE seen_jobs_registry SET score_version='v7', match_score=90, tier='Tier 1'")
        db.reparar_empresas(aplicar=True)
        with sqlite3.connect(db.db_path) as conn:
            rows = {row[0]: row[1:] for row in conn.execute(
                "SELECT url, score_version, match_score, tier FROM seen_jobs_registry")}
        assert rows["https://py.example/1"] == (None, 90, "Tier 1")
        assert rows["https://ok.example/5"] == ("v7", 90, "Tier 1")


class TestHerramienta:
    def test_preview_no_construye_la_base_ni_aplica_migraciones(self, tmp_path, monkeypatch, capsys):
        import config
        from tools import limpiar_empresas_registry as tool
        path = tmp_path / "legacy.db"
        db = GemaDatabase(str(path))
        TestRepararRegistro()._sembrar_sucio(db, TestRepararRegistro()._filas())
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE seen_jobs_registry SET tier='Tier 4', match_score=-1")
        def constructor(*args, **kwargs):
            pytest.fail("preview no debe construir GemaDatabase")
        monkeypatch.setattr(config, "GEMA_DB_PATH", str(path))
        monkeypatch.setattr(GemaDatabase, "__init__", constructor)
        assert tool.main([]) == 0
        assert "filas: 5" in capsys.readouterr().out
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM seen_jobs_registry WHERE tier='Tier 4'").fetchone()[0] == 5
            assert conn.execute("SELECT company FROM seen_jobs_registry WHERE url='https://py.example/1'").fetchone()[0].startswith("NEW ")

    def test_preview_no_crea_una_base_ausente(self, tmp_path, monkeypatch):
        import config
        from tools import limpiar_empresas_registry as tool
        path = tmp_path / "missing.db"
        monkeypatch.setattr(config, "GEMA_DB_PATH", str(path))
        assert tool.main([]) == 0
        assert not path.exists()

    def test_si_el_snapshot_falla_no_abre_la_base(self, monkeypatch):
        import database
        from tools import limpiar_empresas_registry as tool
        def fallo(*args):
            raise tool.SnapshotError("sin espacio")
        def abrir(*args, **kwargs):
            pytest.fail("la base no debe abrirse sin snapshot")
        monkeypatch.setattr(tool, "tomar_snapshot", fallo)
        monkeypatch.setattr(database, "GemaDatabase", abrir)
        assert tool.main(["--aplicar"]) == 2

    def test_aplicar_saca_snapshot_de_la_empresa_original(self, tmp_path, monkeypatch):
        import config
        from tools import limpiar_empresas_registry as tool
        path = tmp_path / "original.db"
        db = GemaDatabase(str(path))
        TestRepararRegistro()._sembrar_sucio(db, TestRepararRegistro()._filas())
        backups = tmp_path / "backups"
        monkeypatch.setattr(config, "GEMA_DB_PATH", str(path))
        monkeypatch.setattr(tool, "directorio_de_backups", lambda value: backups)
        assert tool.main(["--aplicar"]) == 0
        snapshots = list(backups.glob("*.db"))
        assert len(snapshots) == 1
        with sqlite3.connect(snapshots[0]) as conn:
            value = conn.execute("SELECT company FROM seen_jobs_registry WHERE url='https://py.example/1'").fetchone()[0]
        assert value.startswith("NEW ")
