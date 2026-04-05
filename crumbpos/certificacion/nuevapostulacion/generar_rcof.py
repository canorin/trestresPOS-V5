"""
Genera y envía el RCOF (Reporte de Consumo de Folios) para boletas electrónicas.
Requerido por el SII para certificación de boleta electrónica.
"""
import sys
import base64
from datetime import datetime
from pathlib import Path

COMPANY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "nuevapostulacion"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from facturacion_electronica.firma import Firma
from crumbpos.config import settings
from crumbpos.core.sii_client.autenticacion import obtener_token_boleta, obtener_token
from crumbpos.core.sii_client.envio import enviar_dte
from crumbpos.core.firma.firma_digital import cargar_certificado_pfx

PFX_PATH = settings.CERT_DIR / "17586255-2.pfx"
PFX_PASSWORD = "2656"
OUTPUT_DIR = COMPANY_DIR / "output"

FECHA_HOY = datetime.now().strftime("%Y-%m-%d")
TIMESTAMP = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
RUT_EMISOR = "77051056-2"
RUT_FIRMANTE = "17586255-2"
FECHA_RESOLUCION = "2026-03-26"
NUMERO_RESOLUCION = 0


def cargar_firma():
    pfx_data = open(PFX_PATH, "rb").read()
    firma = Firma({
        "string_firma": base64.b64encode(pfx_data).decode(),
        "string_password": PFX_PASSWORD,
        "init_signature": True,
        "rut_firmante": RUT_FIRMANTE,
    })
    if not firma.firma_electronica:
        raise RuntimeError(f"Error cargando certificado: {firma.errores}")
    firma.verify = False
    return firma


def generar_rcof():
    """Genera el RCOF basado en las 5 boletas del set de pruebas.

    Boletas emitidas (T39):
    - F21: Total=29800 (Neto=25042, IVA=4758)
    - F22: Total=2040  (Neto=1714, IVA=326)
    - F23: Total=4100  (Neto=3445, IVA=655)
    - F24: Total=14720 (Neto=10689, Exe=2000, IVA=2031)
    - F25: Total=3500  (Neto=2941, IVA=559)
    """
    # Calcular totales de las 5 boletas
    # Los montos brutos (IVA incluido) de cada boleta:
    boletas = [
        {"folio": 21, "bruto_afecto": 29800, "exento": 0},
        {"folio": 22, "bruto_afecto": 2040, "exento": 0},
        {"folio": 23, "bruto_afecto": 4100, "exento": 0},
        {"folio": 24, "bruto_afecto": 12720, "exento": 2000},  # 8×1590 afecto, 2×1000 exento
        {"folio": 25, "bruto_afecto": 3500, "exento": 0},
    ]

    total_neto = 0
    total_iva = 0
    total_exento = 0
    total_total = 0

    for b in boletas:
        neto = round(b["bruto_afecto"] / 1.19)
        iva = b["bruto_afecto"] - neto
        total_neto += neto
        total_iva += iva
        total_exento += b["exento"]
        total_total += b["bruto_afecto"] + b["exento"]

    print(f"  T39: {len(boletas)} boletas")
    print(f"  Neto total: {total_neto}")
    print(f"  IVA total: {total_iva}")
    print(f"  Exento total: {total_exento}")
    print(f"  Monto total: {total_total}")

    rcof_id = f"RCOF_{FECHA_HOY.replace('-', '')}"

    xml = f"""<DocumentoConsumoFolios ID="{rcof_id}">
<Caratula version="1.0">
<RutEmisor>{RUT_EMISOR}</RutEmisor>
<RutEnvia>{RUT_FIRMANTE}</RutEnvia>
<FchResol>{FECHA_RESOLUCION}</FchResol>
<NroResol>{NUMERO_RESOLUCION}</NroResol>
<FchInicio>{FECHA_HOY}</FchInicio>
<FchFinal>{FECHA_HOY}</FchFinal>
<SecEnvio>1</SecEnvio>
<TmstFirmaEnv>{TIMESTAMP}</TmstFirmaEnv>
</Caratula>
<Resumen>
<TipoDocumento>39</TipoDocumento>
<MntNeto>{total_neto}</MntNeto>
<MntIva>{total_iva}</MntIva>
<TasaIVA>19</TasaIVA>
<MntExento>{total_exento}</MntExento>
<MntTotal>{total_total}</MntTotal>
<FoliosEmitidos>{len(boletas)}</FoliosEmitidos>
<FoliosAnulados>0</FoliosAnulados>
<FoliosUtilizados>{len(boletas)}</FoliosUtilizados>
<RangoUtilizados>
<Inicial>21</Inicial>
<Final>25</Final>
</RangoUtilizados>
</Resumen>
</DocumentoConsumoFolios>"""

    # Envolver en ConsumoFolios con namespaces
    full_xml = (
        f'<ConsumoFolios xmlns="http://www.sii.cl/SiiDte" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:schemaLocation="http://www.sii.cl/SiiDte ConsumoFolio_v10.xsd" '
        f'version="1.0">'
        f'{xml}'
        f'</ConsumoFolios>'
    )

    return full_xml, rcof_id


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("RCOF — Reporte de Consumo de Folios")
    print("=" * 60)

    print("\nGenerando RCOF...")
    xml_str, rcof_id = generar_rcof()

    # Guardar sin firmar
    path_sf = OUTPUT_DIR / "RCOF.xml"
    with open(path_sf, "w", encoding="ISO-8859-1") as f:
        f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + xml_str)
    print(f"\n  XML generado: {path_sf.name}")

    # Firmar
    print("\nCargando certificado...")
    firma = cargar_firma()
    print(f"  RUT firmante: {firma.rut_firmante}")

    signed = firma.firmar(xml_str, rcof_id, type="consu")
    if not signed:
        print(f"  ERROR firmando RCOF: {firma.errores}")
        # Intentar con type="libro_boleta"
        print("  Reintentando con type='libro_boleta'...")
        signed = firma.firmar(xml_str, rcof_id, type="libro_boleta")
        if not signed:
            print(f"  ERROR: {firma.errores}")
            return

    print("  RCOF firmado OK")

    path_f = OUTPUT_DIR / "RCOF_firmado.xml"
    with open(path_f, "w", encoding="ISO-8859-1") as f:
        f.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n' + signed)
    print(f"  -> {path_f.name}")

    # Enviar al SII via SOAP DTEUpload (mismo mecanismo que facturas/libros)
    # El RCOF usa el endpoint SOAP tradicional, NO la REST API de boletas
    print("\nEnviando RCOF al SII (SOAP DTEUpload)...")
    private_key_pem, cert_pem, cert_der = cargar_certificado_pfx(str(PFX_PATH), PFX_PASSWORD)
    token = obtener_token(private_key_pem, cert_der)
    print(f"  Token SOAP: {token}")

    with open(path_f, "rb") as f:
        xml_bytes = f.read()

    resultado = enviar_dte(
        xml_bytes=xml_bytes,
        token=token,
        rut_emisor=RUT_EMISOR,
        rut_envia=RUT_FIRMANTE,
    )

    print(f"\n  Status: {resultado['status']}")
    print(f"  TrackID: {resultado.get('track_id')}")
    print(f"  Glosa: {resultado.get('glosa')}")

    print(f"\n{'=' * 60}")
    print(f"RCOF enviado — TrackID: {resultado.get('track_id')}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
