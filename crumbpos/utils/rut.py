"""Utilidades para manejo de RUT chileno."""
import re

# Regex estricta para RUT formato XXXXXXX-X o XXXXXXXX-X (sin puntos, con guión).
# Acepta cuerpo de 7 u 8 dígitos + DV {0-9, K, k}. NO acepta path traversal,
# espacios, ni otros caracteres. Para uso en validación de path/header.
RUT_REGEX = re.compile(r"^\d{1,8}-[0-9Kk]$")

# Caso especial: namespace reservado para super_admin.
RUT_SYSTEM_NAMESPACE = "SYSTEM"


class RUTInvalidoError(ValueError):
    """RUT con formato inválido. Usado para fallar fast en path/header parsing."""


def validar_formato_rut(rut: str | None) -> str:
    """Valida que un RUT cumpla formato XXXXXXX-X estrictamente, sin DV check.

    Pensado para usar en validación de parámetros de path/header donde el
    objetivo es prevenir path traversal (`../`, `/`, `..`, etc.). NO valida
    el DV matemático — para eso usar `validar_rut()`.

    Acepta el namespace especial `"SYSTEM"` (super admin).

    Args:
        rut: string del RUT (sin puntos, con guión) o "SYSTEM".

    Returns:
        El RUT normalizado (uppercase, sin puntos/espacios).

    Raises:
        RUTInvalidoError: si el formato no cumple el regex.
    """
    if not rut or not isinstance(rut, str):
        raise RUTInvalidoError(f"RUT vacío o no es string: {rut!r}")
    rut_limpio = rut.upper().replace(".", "").replace(" ", "")
    if rut_limpio == RUT_SYSTEM_NAMESPACE:
        return rut_limpio
    if not RUT_REGEX.match(rut_limpio):
        raise RUTInvalidoError(
            f"RUT '{rut}' no cumple formato XXXXXXXX-X. "
            f"Esperado: 1-8 dígitos, guión, dígito verificador (0-9 o K)."
        )
    return rut_limpio


def validar_rut(rut: str) -> bool:
    """Valida un RUT chileno con formato XXXXXXXX-X."""
    rut = rut.upper().replace(".", "").replace(" ", "")
    if "-" not in rut:
        return False
    cuerpo, dv = rut.rsplit("-", 1)
    if not cuerpo.isdigit():
        return False
    return calcular_dv(cuerpo) == dv


def calcular_dv(cuerpo: str) -> str:
    """Calcula el dígito verificador de un RUT."""
    suma = 0
    multiplo = 2
    for d in reversed(cuerpo):
        suma += int(d) * multiplo
        multiplo = multiplo + 1 if multiplo < 7 else 2
    resto = suma % 11
    dv = 11 - resto
    if dv == 11:
        return "0"
    if dv == 10:
        return "K"
    return str(dv)


def formatear_rut(rut: str) -> str:
    """Formatea RUT con puntos y guión: 77.051.056-2."""
    rut = rut.upper().replace(".", "").replace(" ", "")
    if "-" not in rut:
        return rut
    cuerpo, dv = rut.rsplit("-", 1)
    cuerpo_fmt = ""
    for i, d in enumerate(reversed(cuerpo)):
        if i > 0 and i % 3 == 0:
            cuerpo_fmt = "." + cuerpo_fmt
        cuerpo_fmt = d + cuerpo_fmt
    return f"{cuerpo_fmt}-{dv}"


def limpiar_rut(rut: str) -> str:
    """Retorna RUT sin puntos: 77051056-2."""
    return rut.upper().replace(".", "").replace(" ", "")
