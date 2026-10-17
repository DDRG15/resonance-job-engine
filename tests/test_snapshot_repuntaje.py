"""
Snapshot automatico de tools/repuntuar_registry.py.

Re-puntuar pisa `tier`, `score` y `score_version` de miles de filas. El 29-09 se hicieron
tres backups a mano antes de correrla; el backup dependia de que el usuario se acordara, y una
re-puntuacion con una version del scoring mala no tiene "deshacer": la fila vieja se pisa.
Estos tests fijan lo que importa de la copia: que sea CONSISTENTE (incluye lo que vive en
el -wal), que sea ANTERIOR a cualquier escritura, que una falla del snapshot frene la
herramienta en vez de seguir sin red, y que el directorio no pueda terminar en git ni en
la imagen Docker.
"""

import importlib.util
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import config
from _dockerignore import entra_a_la_imagen
from models import JobResult

_RAIZ = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "repuntuar_registry_tool", _RAIZ / "tools" / "repuntuar_registry.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


def _job(url, title="Python Backend Developer"):
    return JobResult(title=title, company="Acme", url=url, source_domain="remoteok.com",
                     salary_raw="$120k", location_raw="Worldwide")


def _filas(db_path, sql="SELECT COUNT(*) FROM seen_jobs_registry"):
    c = sqlite3.connect(db_path)
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


@pytest.fixture
def base(tmp_path):
    """Una base real con el esquema de database.py y 3 avisos sin version de score."""
    from database import GemaDatabase
    ruta = tmp_path / "gema_registry.db"
    db = GemaDatabase(str(ruta))
    db.mark_seen_batch([_job("https://x.com/a"), _job("https://x.com/b"),
                        _job("https://x.com/c")])
    return ruta


class TestTomarSnapshot:
    def test_deja_un_archivo_nuevo_con_el_contenido_de_la_base(self, base, tmp_path):
        destino = tool.tomar_snapshot(str(base), tmp_path / "backups", "v5")
        assert destino is not None and destino.exists()
        assert destino.parent == tmp_path / "backups"
        assert _filas(destino) == 3 == _filas(base)

    def test_el_nombre_lleva_base_fecha_utc_y_version(self, base, tmp_path):
        ahora = datetime(2026, 9, 30, 14, 5, 9, tzinfo=timezone.utc)
        destino = tool.tomar_snapshot(str(base), tmp_path / "b", "v5", ahora=ahora)
        assert destino.name == "gema_registry_20260930T140509Z_pre-repuntaje-v5.db"

    def test_dos_snapshots_en_el_mismo_segundo_no_se_pisan(self, base, tmp_path):
        ahora = datetime(2026, 9, 30, 14, 5, 9, tzinfo=timezone.utc)
        a = tool.tomar_snapshot(str(base), tmp_path / "b", "v5", ahora=ahora)
        b = tool.tomar_snapshot(str(base), tmp_path / "b", "v5", ahora=ahora)
        assert a != b and a.exists() and b.exists()

    def test_la_copia_pasa_integrity_check_y_no_deja_restos(self, base, tmp_path):
        destino = tool.tomar_snapshot(str(base), tmp_path / "b", "v5")
        assert _filas(destino, "PRAGMA integrity_check") == "ok"
        assert sorted(p.name for p in (tmp_path / "b").iterdir()) == [destino.name], \
            "quedaron .parcial / -wal / -shm junto al snapshot"

    def test_la_copia_es_consistente_con_el_wal_sin_checkpoint(self, base, tmp_path):
        """
        La razon de usar Connection.backup y no shutil.copy: con otra conexion abierta,
        las filas recientes viven en el -wal y no en el .db. Copiar el .db entero sin el
        -wal da una base a la que le faltan esas transacciones —sin error, sin aviso—.
        """
        lector = sqlite3.connect(base)
        lector.execute("PRAGMA journal_mode=WAL")
        lector.execute("BEGIN")
        lector.execute("SELECT COUNT(*) FROM seen_jobs_registry").fetchone()  # snapshot del lector
        escritor = sqlite3.connect(base)
        escritor.execute("PRAGMA wal_autocheckpoint=0")
        escritor.execute(
            "INSERT INTO seen_jobs_registry "
            "(job_hash, url, title, company, source_domain, scraped_at) "
            "VALUES ('h-wal', 'https://x.com/wal', 'T', 'C', 'x.com', '2026-09-30T00:00:00Z')")
        escritor.commit()
        try:
            # Premisa: el .db suelto NO tiene la fila nueva (esta en el -wal).
            ingenua = tmp_path / "ingenua.db"
            shutil.copy(base, ingenua)
            assert _filas(ingenua) == 3, "premisa: la fila reciente vive solo en el -wal"

            destino = tool.tomar_snapshot(str(base), tmp_path / "b", "v5")
            assert _filas(destino) == 4, "el snapshot perdio una transaccion del -wal"
        finally:
            lector.close()
            escritor.close()

    def test_base_inexistente_devuelve_none_y_no_crea_nada(self, tmp_path):
        assert tool.tomar_snapshot(str(tmp_path / "no_existe.db"), tmp_path / "b", "v5") is None
        assert not (tmp_path / "b").exists()

    def test_una_base_ilegible_lanza_y_no_deja_archivos(self, tmp_path):
        rota = tmp_path / "rota.db"
        rota.write_bytes(b"esto no es sqlite" * 100)
        with pytest.raises(tool.SnapshotError):
            tool.tomar_snapshot(str(rota), tmp_path / "b", "v5")
        assert list((tmp_path / "b").glob("*")) == [], \
            "una copia fallida no puede quedar con nombre de snapshot ni como .parcial"

    def test_si_integrity_check_falla_se_borra_la_copia(self, base, tmp_path, monkeypatch):
        """El veredicto se lee de la copia, no de la conexion que la escribio."""
        real = sqlite3.connect

        class _Conn:
            def __init__(self, c):
                self._c = c

            def execute(self, sql, *a):
                if sql.startswith("PRAGMA integrity_check"):
                    class _R:
                        def fetchone(self_inner):
                            return ("*** in database main ***",)
                    return _R()
                return self._c.execute(sql, *a)

            def close(self):
                self._c.close()

        def _connect(*a, **k):
            c = real(*a, **k)
            return _Conn(c) if k.get("uri") else c

        monkeypatch.setattr(tool.sqlite3, "connect", _connect)
        with pytest.raises(tool.SnapshotError, match="integrity_check"):
            tool.tomar_snapshot(str(base), tmp_path / "b", "v5")
        assert list((tmp_path / "b").glob("*")) == []


class TestDirectorioDeBackups:
    def test_relativo_se_ancla_a_la_raiz_del_repo_no_al_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert tool.directorio_de_backups("backups") == _RAIZ / "backups"

    def test_absoluto_se_respeta(self, tmp_path):
        assert tool.directorio_de_backups(str(tmp_path / "x")) == tmp_path / "x"

    def test_el_default_de_config_es_backups(self):
        assert config.GEMA_BACKUP_DIR == "backups"


class TestMain:
    @pytest.fixture
    def entorno(self, base, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "GEMA_DB_PATH", str(base))
        monkeypatch.setattr(config, "GEMA_BACKUP_DIR", str(tmp_path / "backups"))
        return base, tmp_path / "backups"

    def test_correr_la_herramienta_deja_un_snapshot_nuevo(self, entorno, capsys):
        base, backups = entorno
        assert tool.main([]) == 0
        snaps = list(backups.glob("*_pre-repuntaje-*.db"))
        assert len(snaps) == 1
        assert f"snapshot: {snaps[0]}" in capsys.readouterr().out

    def test_el_snapshot_es_anterior_a_la_escritura(self, entorno):
        """El invariante entero: la copia tiene el estado VIEJO. Si se sacara despues de
        re-puntuar, seria una copia de lo nuevo y no serviria para volver atras."""
        base, backups = entorno
        sin_version = "SELECT COUNT(*) FROM seen_jobs_registry WHERE score_version IS NULL"
        assert _filas(base, sin_version) == 3

        assert tool.main([]) == 0

        snap = next(backups.glob("*.db"))
        assert _filas(snap, sin_version) == 3, "el snapshot ya traia las filas re-puntuadas"
        assert _filas(base, sin_version) == 0, "la herramienta no re-puntuo (premisa del test)"

    def test_dry_run_no_saca_snapshot(self, entorno, capsys):
        base, backups = entorno
        assert tool.main(["--dry-run"]) == 0
        assert not backups.exists() or list(backups.iterdir()) == []
        assert "--dry-run: no se escribio nada." in capsys.readouterr().out

    def test_si_el_snapshot_falla_no_se_escribe_nada(self, entorno, monkeypatch, capsys):
        base, backups = entorno

        def _falla(*a, **k):
            raise tool.SnapshotError("disco lleno")

        monkeypatch.setattr(tool, "tomar_snapshot", _falla)
        assert tool.main([]) == 2
        assert "No se escribio nada" in capsys.readouterr().err
        sin_version = "SELECT COUNT(*) FROM seen_jobs_registry WHERE score_version IS NULL"
        assert _filas(base, sin_version) == 3, "re-puntuo sin red de seguridad"


class TestElDirectorioNoPuedeTerminarEnGitNiEnLaImagen:
    """
    Un `.dockerignore` con comentarios al final de linea no excluye nada. Un snapshot es
    el registry completo: el mismo error aca filtraria todo el historial de avisos y
    postulaciones.
    """

    @staticmethod
    def _lineas(nombre):
        return (_RAIZ / nombre).read_text(encoding="utf-8").splitlines()

    def test_backups_esta_en_gitignore_como_linea_propia(self):
        assert "backups/" in [l.strip() for l in self._lineas(".gitignore")]

    @pytest.mark.parametrize("ruta", ["backups/gema_registry_x.db", "backups/otro.sqlite3"])
    def test_backups_no_entra_a_la_imagen(self, ruta):
        """Se evalua con la semantica de Docker (gana el ultimo patron que coincide)."""
        assert not entra_a_la_imagen(ruta)

    def test_dockerignore_no_tiene_comentarios_al_final_de_linea(self):
        """`#` solo es comentario al inicio de linea; despues de un patron forma parte de
        el. Es la regla del propio .dockerignore, convertida en test."""
        malas = [l for l in self._lineas(".dockerignore")
                 if l.strip() and not l.lstrip().startswith("#") and "#" in l]
        assert malas == []

    @pytest.mark.skipif(shutil.which("git") is None or not (_RAIZ / ".git").exists(),
                        reason="sin git o fuera del repo")
    def test_la_herramienta_no_esta_ignorada_por_git(self):
        """Un patron de ignore demasiado ancho la dejaria fuera del repo en silencio."""
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index",
                            "tools/repuntuar_registry.py"], cwd=_RAIZ, capture_output=True)
        assert r.returncode != 0
