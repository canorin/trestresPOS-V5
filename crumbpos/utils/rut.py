"""Utilidades para manejo de RUT chileno."""


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
