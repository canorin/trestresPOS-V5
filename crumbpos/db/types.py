"""Tipos SQLAlchemy custom: cifrado transparente en reposo.

Expone `EncryptedString` y `EncryptedText`: columnas que se cifran al
escribir y se descifran al leer, sin tocar el código de aplicación que
las usa.

Uso:
    cert_password: Mapped[str | None] = mapped_column(EncryptedString(200))

El SQLAlchemy ORM hace:
- `INSERT/UPDATE`: pasa el valor por `process_bind_param` → ciphertext.
- `SELECT`: pasa el valor por `process_result_value` → plaintext.

Compatibilidad legacy:
- Si la columna tiene valores plaintext pre-migración, `descifrar()` los
  devuelve tal cual (sin prefijo `enc:v1:`). La siguiente escritura los
  cifra. Esto permite migración rolling sin downtime.

Performance:
- Fernet con AES-128 cifra ~100MB/s. Para campos pequeños (passwords,
  hasta unos cientos de KB en CAF) el overhead es despreciable.
"""
from sqlalchemy.types import String, Text, TypeDecorator

from crumbpos.core.security import cifrar, descifrar


class EncryptedString(TypeDecorator):
    """String cifrado en reposo. Tamaño en disco ≈ 2× tamaño plaintext.

    El tamaño que se pasa al constructor es el del PLAINTEXT esperado;
    en disco se almacena un múltiplo (~1.4-2x) por el token Fernet +
    prefijo `enc:v1:`. Por defecto la columna usa `String(2000)` para
    dar espacio holgado.
    """

    impl = String
    cache_ok = True

    def __init__(self, length: int = 500, *args, **kwargs):
        # Aproximación: ciphertext mide ~1.4x el plaintext + 7 bytes de
        # prefijo + 100 bytes de overhead del token Fernet. Damos 2x.
        super().__init__(length * 2 + 200, *args, **kwargs)

    def process_bind_param(self, value, dialect):
        return cifrar(value)

    def process_result_value(self, value, dialect):
        return descifrar(value)


class EncryptedText(TypeDecorator):
    """Text cifrado en reposo, para payloads grandes (CAF XML, certificados PFX).

    A diferencia de `EncryptedString`, no impone tamaño máximo (`Text`
    es ilimitado en la mayoría de DB engines). Pensado para `cert_data`
    (.pfx base64, típicamente 5-20 KB) y `caf_xml_raw` (1-3 KB).
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return cifrar(value)

    def process_result_value(self, value, dialect):
        return descifrar(value)
