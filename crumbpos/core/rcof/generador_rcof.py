"""Generador de XML para RCOF (Reporte de Consumo de Folios).

Genera el XML requerido por el SII para reportar el consumo diario de folios
de boletas electronicas (tipo 39 y 41).

IMPORTANTE: En boletas los precios INCLUYEN IVA.
  - T39 (afecta): MntNeto = round(total / 1.19), MntIva = total - neto
  - T41 (exenta): MntExento = total (sin MntNeto, MntIva, TasaIVA)
"""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict


def _round_sii(value) -> int:
    """Redondeo SII: ROUND_HALF_UP (no banker's rounding de Python)."""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def generar_rcof(
    rut_emisor: str,
    rut_envia: str,
    fecha_resolucion: str,
    numero_resolucion: int,
    fecha: str,
    boletas: list,
    sec_envio: int = 1,
) -> tuple[str, str]:
    """Genera el XML del RCOF (sin firmar).

    Args:
        rut_emisor: RUT de la empresa emisora (ej: "77051056-2")
        rut_envia: RUT del firmante (ej: "17586255-2")
        fecha_resolucion: Fecha resolucion SII "YYYY-MM-DD"
        numero_resolucion: Numero resolucion SII (0 para certificacion)
        fecha: Fecha del RCOF "YYYY-MM-DD"
        boletas: Lista de objetos DteEmitido (con tipo_dte, folio, monto_total, monto_exento)
        sec_envio: Numero de secuencia del envio

    Returns:
        Tupla (xml_string, rcof_id)
    """
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    rcof_id = f"RCOF_{fecha.replace('-', '')}"

    # Agrupar boletas por tipo_dte
    por_tipo = defaultdict(list)
    for b in boletas:
        por_tipo[b.tipo_dte].append(b)

    # Generar XML del documento
    resumen_xml = ""
    for tipo_dte in sorted(por_tipo.keys()):
        grupo = por_tipo[tipo_dte]
        resumen_xml += _generar_resumen(tipo_dte, grupo)

    xml = (
        f'<ConsumoFolios xmlns="http://www.sii.cl/SiiDte" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:schemaLocation="http://www.sii.cl/SiiDte ConsumoFolio_v10.xsd" '
        f'version="1.0">'
        f'<DocumentoConsumoFolios ID="{rcof_id}">'
        f'<Caratula version="1.0">'
        f'<RutEmisor>{rut_emisor}</RutEmisor>'
        f'<RutEnvia>{rut_envia}</RutEnvia>'
        f'<FchResol>{fecha_resolucion}</FchResol>'
        f'<NroResol>{numero_resolucion}</NroResol>'
        f'<FchInicio>{fecha}</FchInicio>'
        f'<FchFinal>{fecha}</FchFinal>'
        f'<SecEnvio>{sec_envio}</SecEnvio>'
        f'<TmstFirmaEnv>{timestamp}</TmstFirmaEnv>'
        f'</Caratula>'
        f'{resumen_xml}'
        f'</DocumentoConsumoFolios>'
        f'</ConsumoFolios>'
    )

    return xml, rcof_id


def _generar_resumen(tipo_dte: int, boletas: list) -> str:
    """Genera el bloque <Resumen> para un tipo de documento.

    Args:
        tipo_dte: 39 (afecta) o 41 (exenta)
        boletas: Lista de DteEmitido de este tipo

    Returns:
        XML string del bloque Resumen
    """
    total_neto = 0
    total_iva = 0
    total_exento = 0
    total_total = 0
    folios = []

    for b in boletas:
        monto_total = b.monto_total or 0
        monto_exento = b.monto_exento or 0

        if tipo_dte == 41:
            # Boleta exenta: todo es exento
            total_exento += monto_total
            total_total += monto_total
        else:
            # Boleta afecta (T39): precios incluyen IVA
            monto_afecto = monto_total - monto_exento
            neto = _round_sii(monto_afecto / 1.19)
            iva = monto_afecto - neto
            total_neto += neto
            total_iva += iva
            total_exento += monto_exento
            total_total += monto_total

        folios.append(b.folio)

    folios.sort()
    folios_emitidos = len(boletas)
    folios_anulados = 0
    folios_utilizados = folios_emitidos

    # Construir bloque Resumen (orden XSD: TipoDocumento, MntNeto?, MntIva?, TasaIVA?, MntExento?, MntTotal)
    xml = f'<Resumen><TipoDocumento>{tipo_dte}</TipoDocumento>'
    if total_neto:
        xml += f'<MntNeto>{total_neto}</MntNeto>'
    if total_iva:
        xml += f'<MntIva>{total_iva}</MntIva>'
    if total_neto or total_iva:
        # TasaIVA solo cuando hay montos afectos (no para T41 exenta)
        xml += '<TasaIVA>19</TasaIVA>'
    if total_exento:
        xml += f'<MntExento>{total_exento}</MntExento>'
    xml += (
        f'<MntTotal>{total_total}</MntTotal>'
        f'<FoliosEmitidos>{folios_emitidos}</FoliosEmitidos>'
        f'<FoliosAnulados>{folios_anulados}</FoliosAnulados>'
        f'<FoliosUtilizados>{folios_utilizados}</FoliosUtilizados>'
    )

    # Rangos utilizados (secuencias continuas de folios)
    rangos = _calcular_rangos(folios)
    for inicio, final in rangos:
        xml += (
            f'<RangoUtilizados>'
            f'<Inicial>{inicio}</Inicial>'
            f'<Final>{final}</Final>'
            f'</RangoUtilizados>'
        )

    xml += '</Resumen>'
    return xml


def _calcular_rangos(folios: list[int]) -> list[tuple[int, int]]:
    """Calcula rangos continuos de folios.

    Ej: [21, 22, 23, 25, 26] -> [(21, 23), (25, 26)]
    """
    if not folios:
        return []

    rangos = []
    inicio = folios[0]
    fin = folios[0]

    for f in folios[1:]:
        if f == fin + 1:
            fin = f
        else:
            rangos.append((inicio, fin))
            inicio = f
            fin = f

    rangos.append((inicio, fin))
    return rangos
