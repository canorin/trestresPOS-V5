"""Compatibilidad con ``facturacion_electronica.firma.Firma``.

Reemplaza el método ``verificar_firma`` del tercero por una
implementación basada en ``cryptography``. Es código permanente, no
un parche temporal: vivirá mientras la versión de
``facturacion_electronica`` instalada siga usando la API removida.

## Bug que arregla

``facturacion_electronica.firma.Firma.verificar_firma`` (versión 0.20.4 al
menos) llama a ``OpenSSL.crypto.verify(key, sig, data, algo)`` para validar
una firma RSA. Esa función fue **removida** de ``pyOpenSSL`` en versiones
recientes — el módulo ``OpenSSL.crypto`` ya no expone ``verify`` como
atributo, y al usarse lanza ``AttributeError``.

El ``except:`` desnudo del original silencia la excepción y retorna
``False`` para TODA firma, sin importar si es válida o no. Eso se
manifiesta en producción como:

  "Firma DTE inválida (pre-verificación): Rechazado - Error en Firma"

bloqueando la emisión completa de DTEs aunque la firma esté impecable.

## Qué hace este parche

Reemplaza ``Firma.verificar_firma`` por una implementación que usa
``cryptography`` (ya es dependencia transitiva, y es lo que el resto
del core usa para firmar/cifrar). Comportamiento idéntico desde fuera:

- Retorna ``True`` si la firma RSA es válida bajo la clave pública del
  certificado embebido en ``self.cert``.
- Retorna ``False`` si la firma no valida (``InvalidSignature``) o si
  cualquier otra cosa explota (cert mal formado, base64 corrupto…).

## Política

- Idempotente: si el parche ya está aplicado, no se re-aplica.
- Aplicado al importarse este módulo. ``crumbpos.api.app`` lo importa
  como side-effect al arrancar, por eso queda activo para toda la API.
- Vivirá hasta que ``facturacion_electronica`` libere una versión que
  no use ``OpenSSL.crypto.verify``. Removerlo en ese momento.
"""
from __future__ import annotations

import base64
import logging

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.hashes import SHA1, SHA256

import facturacion_electronica.firma as _fe_firma

logger = logging.getLogger(__name__)

_MARKER = "_crumbpos_verificar_firma_v1"


def _verificar_firma_cryptography(self, firma, texto, algo: str = "sha1") -> bool:
    """Verifica firma RSA usando ``cryptography``.

    Reemplazo drop-in de ``Firma.verificar_firma``: misma signature,
    mismo semántica de retorno (True/False).
    """
    try:
        sig_bytes = base64.b64decode(firma)
        data = texto if isinstance(texto, bytes) else texto.encode()
        pem = (
            "-----BEGIN CERTIFICATE-----\n"
            + self.cert
            + "\n-----END CERTIFICATE-----\n"
        ).encode("ascii")
        cert = x509.load_pem_x509_certificate(pem)
        pub_key = cert.public_key()
        hash_algo = SHA256() if algo == "sha256" else SHA1()
        pub_key.verify(sig_bytes, data, PKCS1v15(), hash_algo)
        return True
    except InvalidSignature:
        logger.warning(
            "Firma RSA inválida — la clave pública del certificado "
            "no coincide con la firma."
        )
        return False
    except Exception:
        logger.warning("Error verificando firma", exc_info=True)
        return False


# Marcamos la función para detección de idempotencia.
_verificar_firma_cryptography._crumbpos_patch = _MARKER  # type: ignore[attr-defined]


def aplicar_parche() -> None:
    """Aplica el parche si no está aplicado todavía. Idempotente."""
    actual = getattr(_fe_firma.Firma.verificar_firma, "_crumbpos_patch", None)
    if actual == _MARKER:
        return  # ya aplicado
    _fe_firma.Firma.verificar_firma = _verificar_firma_cryptography
    logger.info(
        "Parche aplicado: facturacion_electronica.firma.Firma."
        "verificar_firma → cryptography (compat pyOpenSSL >= 24)."
    )


# Aplicar al importarse el módulo.
aplicar_parche()
