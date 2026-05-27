"""Cifrado simétrico (Fernet) para secretos sensibles en BD.

## Arquitectura

- **Algoritmo**: Fernet (AES-128-CBC + HMAC-SHA256, RFC compatible).
- **Clave maestra**: 32 bytes random base64-url-safe, obtenida de
  la variable de entorno `CRUMBPOS_MASTER_KEY`.
- **Prefijo de marcador**: los valores cifrados se almacenan con prefijo
  `"enc:v1:"` para distinguirlos de valores legacy en plaintext y permitir
  migración idempotente. El número de versión habilita futuras rotaciones.

## Política operativa

1. **Master key en producción**: NUNCA hardcodear. Debe venir de un
   secret manager (AWS Secrets Manager, GCP Secret Manager, HashiCorp Vault)
   y rotarse anualmente.
2. **Backup de la clave**: si se pierde, todos los secretos cifrados
   quedan inaccesibles. Mantener copia en cold storage offline.
3. **Migración legacy**: valores existentes sin prefijo se descifran
   como plaintext (transición). El siguiente set los cifra.
4. **Rotación**: `rotar_clave()` permite recifrar masivamente con clave nueva.

## Qué se cifra

| Campo | Riesgo si se filtra |
|-------|---------------------|
| `Empresa.cert_password` | Firma de DTEs en nombre del contribuyente |
| `Empresa.cert_data` (.pfx) | Mismo riesgo + autenticación SOAP SII |
| `CafFolio.caf_xml_raw` | Clave privada del timbre — falsificación TED |

## Modo de desarrollo

Si `CRUMBPOS_MASTER_KEY` no está definida y `CRUMBPOS_ENV != "production"`,
se usa una clave derivada determinística (banner WARNING). Esto facilita
tests locales sin exigir setup. **NUNCA usar en producción.**
"""
import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Prefijo de marcador para valores cifrados. v1 = Fernet con master key
# desde env. Si en el futuro migramos a sobre con KMS, sería v2.
_PREFIX = "enc:v1:"

# Cache de la instancia Fernet — barata pero evita re-leer env en cada op
_fernet_cache: Fernet | None = None


class SecretoCifradoError(Exception):
    """Error específico del módulo de cifrado.

    Subclases: master key ausente/inválida, token corrupto, formato inválido.
    """


def _obtener_master_key() -> bytes:
    """Obtiene la master key desde el entorno.

    Returns:
        bytes: 32 bytes base64-url-safe listos para Fernet.

    Raises:
        SecretoCifradoError: si CRUMBPOS_ENV=production y la clave falta.
    """
    key_b64 = os.getenv("CRUMBPOS_MASTER_KEY")

    if key_b64:
        # Validar que sea una Fernet key válida (44 chars base64-url-safe)
        try:
            key_bytes = key_b64.encode("ascii")
            # Fernet acepta directamente la base64-url
            Fernet(key_bytes)  # valida formato
            return key_bytes
        except Exception as exc:
            raise SecretoCifradoError(
                f"CRUMBPOS_MASTER_KEY no es una Fernet key válida (44 bytes base64-url-safe). "
                f"Genera una nueva con: python -c \"from cryptography.fernet import Fernet; "
                f"print(Fernet.generate_key().decode())\". Error: {exc}"
            ) from exc

    # Sin clave en env: solo permitido en dev/test.
    entorno = os.getenv("CRUMBPOS_ENV", "development").lower()
    if entorno == "production":
        raise SecretoCifradoError(
            "CRUMBPOS_MASTER_KEY es obligatoria en producción. "
            "Configurar variable de entorno con una Fernet key de 32 bytes base64-url-safe."
        )

    # Modo desarrollo: usar clave persistida en data/.dev_master_key si existe,
    # o generarla y guardarla en ese archivo. Así la clave es única por máquina
    # y estable entre reinicios, sin hardcodear un seed predecible en el código.
    from pathlib import Path
    data_dir = Path(__file__).resolve().parent.parent.parent.parent / "data"
    dev_key_path = data_dir / ".dev_master_key"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        if dev_key_path.exists():
            stored = dev_key_path.read_text().strip().encode("ascii")
            Fernet(stored)  # validar que sigue siendo una key válida
            logger.warning(
                "⚠️  CRUMBPOS_MASTER_KEY no definida. Usando clave local de desarrollo "
                "(%s). NO USAR EN PRODUCCIÓN.", dev_key_path
            )
            return stored
        # Generar clave nueva única para esta instalación
        new_key = Fernet.generate_key()
        dev_key_path.write_text(new_key.decode("ascii"))
        dev_key_path.chmod(0o600)
        logger.warning(
            "⚠️  CRUMBPOS_MASTER_KEY no definida. Clave de desarrollo generada y "
            "guardada en %s. NO USAR EN PRODUCCIÓN. Para producción: "
            "export CRUMBPOS_MASTER_KEY=$(python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\")",
            dev_key_path,
        )
        return new_key
    except Exception as exc:
        # Fallback: clave derivada determinística (menos segura, solo si el FS no es escribible)
        logger.warning(
            "⚠️  No se pudo leer/crear %s (%s). Usando clave determinística "
            "de último recurso. NO USAR EN PRODUCCIÓN.", dev_key_path, exc
        )
        digest = hashlib.sha256(b"crumbpos-dev-key-NEVER-USE-IN-PRODUCTION").digest()
        return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    """Instancia Fernet cacheada (lazy init)."""
    global _fernet_cache
    if _fernet_cache is None:
        _fernet_cache = Fernet(_obtener_master_key())
    return _fernet_cache


def _reset_cache() -> None:
    """Resetea el cache de Fernet. Útil en tests para forzar re-lectura
    de la master key tras un `monkeypatch.setenv(...)`."""
    global _fernet_cache
    _fernet_cache = None


def es_cifrado(valor: str | None) -> bool:
    """Verifica si un valor está cifrado (tiene prefijo enc:v1:).

    Permite migración idempotente: valores legacy en plaintext no llevan
    el prefijo y `descifrar()` los devuelve tal cual, gatillando re-cifrado
    en la próxima escritura.
    """
    if not valor or not isinstance(valor, str):
        return False
    return valor.startswith(_PREFIX)


def cifrar(plaintext: str | None) -> str | None:
    """Cifra un valor con la master key. Devuelve None si entrada es None.

    Si el valor ya viene cifrado (prefijo presente), lo retorna tal cual
    para idempotencia. Esto permite llamar `cifrar()` en migraciones
    masivas sin re-cifrar lo ya cifrado.

    Args:
        plaintext: texto plano a cifrar (UTF-8) o None.

    Returns:
        String con prefijo `enc:v1:` seguido del token Fernet,
        o None si plaintext era None.

    Raises:
        SecretoCifradoError: si la master key no está disponible.
    """
    if plaintext is None:
        return None
    if not isinstance(plaintext, str):
        raise SecretoCifradoError(
            f"cifrar() espera str, recibió {type(plaintext).__name__}"
        )
    # Idempotencia: si ya está cifrado, no re-cifrar
    if es_cifrado(plaintext):
        return plaintext
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def descifrar(valor: str | None) -> str | None:
    """Descifra un valor con la master key.

    Si el valor NO tiene prefijo (legacy plaintext), se retorna tal cual.
    Esto permite leer datos pre-migración sin error.

    Args:
        valor: texto cifrado con prefijo, plaintext legacy, o None.

    Returns:
        Texto plano UTF-8 o None.

    Raises:
        SecretoCifradoError: si el token está corrupto o la master key
            no puede descifrarlo (clave incorrecta).
    """
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise SecretoCifradoError(
            f"descifrar() espera str o None, recibió {type(valor).__name__}"
        )
    if not es_cifrado(valor):
        # Legacy plaintext — retornar tal cual para compatibilidad migracional
        return valor
    token = valor[len(_PREFIX):].encode("ascii")
    try:
        return _fernet().decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise SecretoCifradoError(
            "Token cifrado corrupto o master key incorrecta. "
            "Verificar que CRUMBPOS_MASTER_KEY no haya cambiado desde el cifrado."
        ) from exc


def rotar_clave(valor: str | None, master_key_anterior: str) -> str | None:
    """Re-cifra un valor con la master key ACTUAL, descifrándolo con la anterior.

    Útil para rotación periódica de la master key sin downtime.

    Args:
        valor: texto cifrado con la master key anterior.
        master_key_anterior: la clave previa (base64-url-safe).

    Returns:
        Texto cifrado con la master key actual.
    """
    if valor is None or not es_cifrado(valor):
        # Plaintext o None: solo cifrar con la nueva
        return cifrar(valor)
    # Descifrar con la anterior
    fernet_anterior = Fernet(master_key_anterior.encode("ascii"))
    token = valor[len(_PREFIX):].encode("ascii")
    try:
        plaintext = fernet_anterior.decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise SecretoCifradoError(
            "No se pudo descifrar con master_key_anterior. ¿La clave correcta?"
        ) from exc
    return cifrar(plaintext)
