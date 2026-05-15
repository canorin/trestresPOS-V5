"""Cifrado simétrico de secretos sensibles en BD.

Expone una API mínima para cifrar/descifrar campos sensibles
(cert_password, cert_data, caf_xml_raw) con Fernet + master key.

Uso:
    from crumbpos.core.security import cifrar, descifrar

    # Al persistir
    empresa.cert_password = cifrar(password_plana)

    # Al leer
    password_plana = descifrar(empresa.cert_password)
"""
from crumbpos.core.security.cifrado import (
    SecretoCifradoError,
    cifrar,
    descifrar,
    es_cifrado,
    rotar_clave,
)

__all__ = [
    "SecretoCifradoError",
    "cifrar",
    "descifrar",
    "es_cifrado",
    "rotar_clave",
]
