"""Tests del módulo de cifrado de secretos."""
from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

from crumbpos.core.security import cifrado
from crumbpos.core.security.cifrado import (
    SecretoCifradoError,
    cifrar,
    descifrar,
    es_cifrado,
    rotar_clave,
)


@pytest.fixture(autouse=True)
def _master_key_test(monkeypatch):
    """Master key dedicada para tests. Reset del cache antes de cada test."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CRUMBPOS_MASTER_KEY", key)
    monkeypatch.setenv("CRUMBPOS_ENV", "test")
    cifrado._reset_cache()
    yield key
    cifrado._reset_cache()


class TestCifradoBasico:
    def test_cifra_y_descifra_string(self):
        original = "MiPasswordSuperSecreta123!"
        cifrado_val = cifrar(original)
        assert cifrado_val != original
        assert cifrado_val.startswith("enc:v1:")
        assert descifrar(cifrado_val) == original

    def test_cifrar_none_devuelve_none(self):
        assert cifrar(None) is None
        assert descifrar(None) is None

    def test_es_cifrado_detecta_prefijo(self):
        assert es_cifrado("enc:v1:abc")
        assert not es_cifrado("plaintext")
        assert not es_cifrado(None)
        assert not es_cifrado("")

    def test_cifrar_es_idempotente(self):
        """Cifrar dos veces el mismo valor no genera doble-cifrado."""
        original = "secreto"
        primero = cifrar(original)
        segundo = cifrar(primero)  # ya cifrado
        assert primero == segundo

    def test_descifrar_plaintext_legacy_devuelve_tal_cual(self):
        """Valores sin prefijo (legacy pre-migración) se devuelven sin error."""
        legacy = "plaintext-sin-cifrar"
        assert descifrar(legacy) == legacy


class TestSeguridad:
    def test_dos_cifrados_del_mismo_texto_difieren(self):
        """Fernet incluye nonce aleatorio: cifrados distintos del mismo texto."""
        a = cifrar("hola")
        b = cifrar("hola")
        assert a != b
        # pero ambos descifran al mismo plaintext
        assert descifrar(a) == descifrar(b) == "hola"

    def test_master_key_distinta_no_descifra(self, monkeypatch):
        """Cifrar con una clave y descifrar con otra → SecretoCifradoError."""
        token = cifrar("dato-protegido")
        # Cambiar la master key
        nueva_key = Fernet.generate_key().decode()
        monkeypatch.setenv("CRUMBPOS_MASTER_KEY", nueva_key)
        cifrado._reset_cache()
        with pytest.raises(SecretoCifradoError, match="Token cifrado corrupto"):
            descifrar(token)

    def test_token_corrupto_lanza_error(self):
        with pytest.raises(SecretoCifradoError):
            descifrar("enc:v1:basura-no-es-token")

    def test_falta_master_key_en_produccion_falla(self, monkeypatch):
        monkeypatch.delenv("CRUMBPOS_MASTER_KEY", raising=False)
        monkeypatch.setenv("CRUMBPOS_ENV", "production")
        cifrado._reset_cache()
        with pytest.raises(SecretoCifradoError, match="CRUMBPOS_MASTER_KEY es obligatoria"):
            cifrar("dato")

    def test_master_key_invalida_falla(self, monkeypatch):
        monkeypatch.setenv("CRUMBPOS_MASTER_KEY", "no-es-fernet-key-valida")
        cifrado._reset_cache()
        with pytest.raises(SecretoCifradoError, match="Fernet key válida"):
            cifrar("dato")


class TestRotacionClave:
    def test_rotar_clave_con_clave_anterior(self, monkeypatch):
        """Migrar valores cifrados de master key vieja a nueva."""
        # Cifrar con clave A
        clave_a = os.environ["CRUMBPOS_MASTER_KEY"]
        token_a = cifrar("secreto-historico")

        # Cambiar a clave B
        clave_b = Fernet.generate_key().decode()
        monkeypatch.setenv("CRUMBPOS_MASTER_KEY", clave_b)
        cifrado._reset_cache()

        # Rotar token_a a clave_b
        token_b = rotar_clave(token_a, clave_a)
        assert token_b != token_a
        assert token_b.startswith("enc:v1:")
        # Y se descifra correctamente con clave_b
        assert descifrar(token_b) == "secreto-historico"

    def test_rotar_clave_sobre_plaintext_legacy(self, monkeypatch):
        """Rotar sobre un plaintext legacy: lo cifra con la clave actual."""
        clave_a = os.environ["CRUMBPOS_MASTER_KEY"]
        legacy = "valor-plaintext"
        rotado = rotar_clave(legacy, clave_a)
        assert rotado.startswith("enc:v1:")
        assert descifrar(rotado) == legacy

    def test_rotar_clave_anterior_incorrecta_falla(self, monkeypatch):
        token = cifrar("secreto")
        clave_falsa = Fernet.generate_key().decode()
        with pytest.raises(SecretoCifradoError, match="master_key_anterior"):
            rotar_clave(token, clave_falsa)


class TestModoDesarrollo:
    """En dev/test sin master key, se usa clave derivada (con WARNING)."""

    def test_dev_sin_master_key_usa_derivada(self, monkeypatch, caplog):
        monkeypatch.delenv("CRUMBPOS_MASTER_KEY", raising=False)
        monkeypatch.setenv("CRUMBPOS_ENV", "development")
        cifrado._reset_cache()
        with caplog.at_level("WARNING"):
            token = cifrar("dato-dev")
        assert any("CRUMBPOS_MASTER_KEY no definida" in r.message for r in caplog.records)
        assert descifrar(token) == "dato-dev"

    def test_dev_clave_derivada_es_estable(self, monkeypatch):
        """En dev, el cifrado/descifrado funciona estable entre tests."""
        monkeypatch.delenv("CRUMBPOS_MASTER_KEY", raising=False)
        monkeypatch.setenv("CRUMBPOS_ENV", "development")
        cifrado._reset_cache()
        a = cifrar("x")
        cifrado._reset_cache()
        assert descifrar(a) == "x"
