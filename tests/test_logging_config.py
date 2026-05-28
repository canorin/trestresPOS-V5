"""Tests para configurar_logging().

Verifica que el sistema de logging quede correctamente configurado:
- Dos handlers activos (consola + archivo).
- El archivo de log se crea en el directorio indicado.
- La función es idempotente (sin handlers duplicados al llamarla dos veces).
- Las librerías externas ruidosas quedan en WARNING.
"""
import logging
import pytest
from crumbpos.core.logging_config import configurar_logging, _LIBRERIAS_SILENCIAR


class TestConfigurarLogging:

    def test_crea_dos_handlers(self, tmp_path):
        """Después de configurar, el root logger tiene exactamente 2 handlers."""
        configurar_logging(nivel="INFO", log_dir=tmp_path)
        root = logging.getLogger()
        assert len(root.handlers) == 2

    def test_crea_archivo_log(self, tmp_path):
        """El archivo crumbpos.log se crea en el directorio indicado."""
        configurar_logging(nivel="INFO", log_dir=tmp_path)
        log_file = tmp_path / "crumbpos.log"
        assert log_file.exists(), f"Archivo de log no creado en {log_file}"

    def test_idempotente_no_duplica_handlers(self, tmp_path):
        """Llamar dos veces no acumula handlers."""
        configurar_logging(nivel="INFO", log_dir=tmp_path)
        configurar_logging(nivel="INFO", log_dir=tmp_path)
        root = logging.getLogger()
        assert len(root.handlers) == 2

    def test_nivel_debug(self, tmp_path):
        """nivel='DEBUG' configura el root logger en DEBUG."""
        configurar_logging(nivel="DEBUG", log_dir=tmp_path)
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_nivel_info(self, tmp_path):
        """nivel='INFO' configura el root logger en INFO."""
        configurar_logging(nivel="INFO", log_dir=tmp_path)
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_crea_directorio_si_no_existe(self, tmp_path):
        """Crea el directorio de logs aunque no exista."""
        nuevo_dir = tmp_path / "sub" / "logs"
        assert not nuevo_dir.exists()
        configurar_logging(nivel="INFO", log_dir=nuevo_dir)
        assert nuevo_dir.exists()

    def test_librerias_externas_en_warning(self, tmp_path):
        """Las librerías ruidosas quedan en WARNING tras configurar."""
        configurar_logging(nivel="DEBUG", log_dir=tmp_path)
        for nombre in _LIBRERIAS_SILENCIAR:
            nivel = logging.getLogger(nombre).level
            assert nivel == logging.WARNING, (
                f"Librería {nombre!r} quedó en nivel {nivel}, esperado WARNING"
            )

    def test_escribe_en_archivo(self, tmp_path):
        """Un log.info() después de configurar se escribe en el archivo."""
        configurar_logging(nivel="INFO", log_dir=tmp_path)
        test_logger = logging.getLogger("crumbpos.test_escritura")
        test_logger.info("Mensaje de prueba de escritura en archivo")
        log_file = tmp_path / "crumbpos.log"
        contenido = log_file.read_text(encoding="utf-8")
        assert "Mensaje de prueba de escritura en archivo" in contenido
