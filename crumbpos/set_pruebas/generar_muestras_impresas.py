"""
Generador de Muestras Impresas para Certificación SII.
Diseño basado en formato estándar de factura electrónica chilena.
"""
import os
import re
import sys
import tempfile
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lxml import etree
from fpdf import FPDF
from pdf417 import encode, render_image
from num2words import num2words

from crumbpos.config import settings

LOGO_PATH = str(Path(__file__).resolve().parent.parent / "config" / "logo.png")
NUMERO_RESOLUCION = 0
FECHA_RESOLUCION = "2026-03-26"

TIPO_NOMBRE = {
    33: ("FACTURA", "ELECTRONICA"),
    34: ("FACTURA NO AFECTA", "O EXENTA ELECTRONICA"),
    39: ("BOLETA", "ELECTRONICA"),
    41: ("BOLETA EXENTA", "ELECTRONICA"),
    46: ("FACTURA DE COMPRA", "ELECTRONICA"),
    52: ("GUIA DE DESPACHO", "ELECTRONICA"),
    56: ("NOTA DE DEBITO", "ELECTRONICA"),
    61: ("NOTA DE CREDITO", "ELECTRONICA"),
}

TIPOS_CEDIBLES = {33, 34, 52}
SUCURSAL_SII = "SANTIAGO ORIENTE"


class DTEData:
    """Datos extraídos de un DTE XML firmado."""
    def __init__(self):
        self.tipo_dte = 0
        self.folio = 0
        self.fecha_emision = ""
        self.emisor_rut = ""
        self.emisor_razon = ""
        self.emisor_giro = ""
        self.emisor_dir = ""
        self.emisor_comuna = ""
        self.emisor_ciudad = ""
        self.emisor_acteco = ""
        self.receptor_rut = ""
        self.receptor_razon = ""
        self.receptor_giro = ""
        self.receptor_dir = ""
        self.receptor_comuna = ""
        self.receptor_ciudad = ""
        self.monto_neto = None
        self.monto_exento = None
        self.tasa_iva = None
        self.iva = None
        self.monto_total = 0
        self.items = []
        self.referencias = []
        self.descuentos_globales = []
        self.ted_xml = ""
        self._ted_element = None
        self.ind_traslado = None
        self.tipo_despacho = None
        self.ind_servicio = None


def parse_dte_xml(doc_element) -> DTEData:
    """Extrae datos de un elemento Documento XML."""
    data = DTEData()

    def find_text(parent, tag):
        for el in parent.iter():
            t = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if t == tag:
                return el.text or ""
        return ""

    def find_el(parent, tag):
        for el in parent.iter():
            t = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if t == tag:
                return el
        return None

    enc = find_el(doc_element, "Encabezado")
    if enc is not None:
        data.tipo_dte = int(find_text(enc, "TipoDTE") or "0")
        data.folio = int(find_text(enc, "Folio") or "0")
        data.fecha_emision = find_text(enc, "FchEmis")

        id_doc = find_el(enc, "IdDoc")
        if id_doc is not None:
            data.ind_traslado = find_text(id_doc, "IndTraslado") or None
            data.tipo_despacho = find_text(id_doc, "TipoDespacho") or None
            data.ind_servicio = find_text(id_doc, "IndServicio") or None

        emisor = find_el(enc, "Emisor")
        if emisor is not None:
            data.emisor_rut = find_text(emisor, "RUTEmisor")
            data.emisor_razon = find_text(emisor, "RznSoc") or find_text(emisor, "RznSocEmisor")
            data.emisor_giro = find_text(emisor, "GiroEmis") or find_text(emisor, "GiroEmisor")
            data.emisor_dir = find_text(emisor, "DirOrigen")
            data.emisor_comuna = find_text(emisor, "CmnaOrigen")
            data.emisor_ciudad = find_text(emisor, "CiudadOrigen")
            data.emisor_acteco = find_text(emisor, "Acteco")

        receptor = find_el(enc, "Receptor")
        if receptor is not None:
            data.receptor_rut = find_text(receptor, "RUTRecep")
            data.receptor_razon = find_text(receptor, "RznSocRecep")
            data.receptor_giro = find_text(receptor, "GiroRecep")
            data.receptor_dir = find_text(receptor, "DirRecep")
            data.receptor_comuna = find_text(receptor, "CmnaRecep")
            data.receptor_ciudad = find_text(receptor, "CiudadRecep")

        totales = find_el(enc, "Totales")
        if totales is not None:
            mn = find_text(totales, "MntNeto")
            data.monto_neto = int(mn) if mn else None
            me = find_text(totales, "MntExe")
            data.monto_exento = int(me) if me else None
            ti = find_text(totales, "TasaIVA")
            data.tasa_iva = int(ti) if ti else None
            iv = find_text(totales, "IVA")
            data.iva = int(iv) if iv else None
            mt = find_text(totales, "MntTotal")
            data.monto_total = int(mt) if mt else 0

    for el in doc_element.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "Detalle":
            item = {
                "nro": find_text(el, "NroLinDet"),
                "nombre": find_text(el, "NmbItem"),
                "qty": find_text(el, "QtyItem"),
                "unidad": find_text(el, "UnmdItem"),
                "precio": find_text(el, "PrcItem"),
                "descuento_pct": find_text(el, "DescuentoPct"),
                "descuento_monto": find_text(el, "DescuentoMonto"),
                "monto": find_text(el, "MontoItem"),
                "ind_exe": find_text(el, "IndExe"),
            }
            data.items.append(item)

    for el in doc_element.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "Referencia":
            ref = {
                "nro": find_text(el, "NroLinRef"),
                "tipo_doc": find_text(el, "TpoDocRef"),
                "folio": find_text(el, "FolioRef"),
                "fecha": find_text(el, "FchRef"),
                "codigo": find_text(el, "CodRef"),
                "razon": find_text(el, "RazonRef"),
            }
            data.referencias.append(ref)

    for el in doc_element.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "DscRcgGlobal":
            desc = {
                "nro": find_text(el, "NroLinDR"),
                "tipo": find_text(el, "TpoMov"),
                "glosa": find_text(el, "GlosaDR"),
                "tipo_valor": find_text(el, "TpoValor"),
                "valor": find_text(el, "ValorDR"),
            }
            data.descuentos_globales.append(desc)

    ted_el = find_el(doc_element, "TED")
    if ted_el is not None:
        # Guardar referencia al elemento para extracción raw posterior
        data._ted_element = ted_el

    return data


def format_rut(rut: str) -> str:
    parts = rut.split("-")
    if len(parts) != 2:
        return rut
    num, dv = parts
    formatted = ""
    for i, ch in enumerate(reversed(num)):
        if i > 0 and i % 3 == 0:
            formatted = "." + formatted
        formatted = ch + formatted
    return f"{formatted}-{dv}"


def format_number(amount) -> str:
    if amount is None:
        return "0"
    n = int(amount)
    return f"{n:,}".replace(",", ".")


def monto_en_palabras(monto: int) -> str:
    try:
        palabras = num2words(monto, lang='es')
        return palabras.upper() + " PESOS"
    except Exception:
        return str(monto)


# ===================== PDF GENERATOR =====================

class MuestraImpresaPDF(FPDF):

    def __init__(self, data: DTEData, cedible: bool = False):
        super().__init__(orientation="P", unit="mm", format="letter")
        self.data = data
        self.cedible = cedible
        self.set_auto_page_break(auto=False)
        self.LM = 10  # left margin
        self.PW = 195.4  # page width usable (letter=215.9 - 2*10.25)

    def rr(self, x, y, w, h, r=3, style="D"):
        """Rectángulo con esquinas redondeadas (wrapper nativo fpdf2)."""
        self.rect(x, y, w, h, style=style, round_corners=True, corner_radius=r)

    def generar(self) -> bytes:
        self.add_page()
        self.set_margins(10, 10, 10)

        y = 10
        y = self._encabezado(y)
        y = self._receptor(y)
        if self.data.tipo_dte == 52 and self.data.ind_traslado:
            y = self._info_guia(y)
        y = self._detalle(y)
        y = self._son_pesos(y)
        y = self._totales(y)
        self._timbre()

        if self.cedible:
            self._acuse_recibo()
            self._leyenda_cedible()

        return self.output()

    # ---------- ENCABEZADO ----------
    def _encabezado(self, y: float) -> float:
        x = self.LM

        # ---- RECUADRO ROJO (derecha, SIN esquinas redondeadas) ----
        box_x = 138
        box_y = 10
        box_w = 67
        box_h = 32

        # Logo encima de la razón social (~80% del ancho del recuadro rojo)
        logo_path = getattr(self, 'logo_path', LOGO_PATH)
        max_logo_w = box_w * 0.8  # ~54mm
        max_logo_h = max_logo_w * 0.5  # ~27mm alto máximo
        if logo_path and os.path.exists(logo_path):
            from PIL import Image as PILImage
            with PILImage.open(logo_path) as img:
                img_w, img_h = img.size
            aspect = img_w / img_h
            # Escalar manteniendo proporción dentro del espacio máximo
            if aspect >= (max_logo_w / max_logo_h):
                # Imagen más ancha: limitar por ancho
                logo_w = max_logo_w
                logo_h = logo_w / aspect
            else:
                # Imagen más alta: limitar por alto
                logo_h = max_logo_h
                logo_w = logo_h * aspect
            self.image(logo_path, x=x, y=y, w=logo_w, h=logo_h)
            y += logo_h + 2

        # Emisor: razón social grande
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(0, 0, 0)
        self.set_xy(x, y)
        self.cell(120, 7, self.data.emisor_razon)
        y += 8

        # Giro
        self.set_font("Helvetica", "", 9)
        self.set_xy(x, y)
        self.cell(120, 4, self.data.emisor_giro)
        y += 5

        # Dirección en una sola línea
        dir_parts = [self.data.emisor_dir, self.data.emisor_comuna, self.data.emisor_ciudad]
        dir_completa = ", ".join(p for p in dir_parts if p)
        self.set_xy(x, y)
        self.cell(120, 4, dir_completa)

        self.set_draw_color(255, 0, 0)
        self.set_line_width(0.8)
        self.rect(box_x, box_y, box_w, box_h)

        self.set_text_color(255, 0, 0)
        self.set_font("Helvetica", "B", 12)
        self.set_xy(box_x, box_y + 3)
        self.cell(box_w, 6, f"R.U.T.: {format_rut(self.data.emisor_rut)}", align="C")

        tipo_lineas = TIPO_NOMBRE.get(self.data.tipo_dte, ("DOCUMENTO", "ELECTRONICO"))
        self.set_font("Helvetica", "B", 13)
        self.set_xy(box_x, box_y + 10)
        self.cell(box_w, 6, tipo_lineas[0], align="C")
        self.set_xy(box_x, box_y + 17)
        self.cell(box_w, 6, tipo_lineas[1], align="C")

        self.set_font("Helvetica", "B", 14)
        self.set_xy(box_x, box_y + 24)
        self.cell(box_w, 6, f"N\u00ba {self.data.folio}", align="C")

        # Sucursal SII debajo — en ROJO
        self.set_text_color(255, 0, 0)
        self.set_font("Helvetica", "", 9)
        self.set_xy(box_x, box_y + box_h + 2)
        self.cell(box_w, 4, f"S.I.I. - {SUCURSAL_SII}", align="C")

        # Restaurar color negro
        self.set_text_color(0, 0, 0)

        return box_y + box_h + 22

    # ---------- RECEPTOR (recuadro 2 columnas) ----------
    def _receptor(self, y: float) -> float:
        x = self.LM
        w = self.PW
        h = 30

        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.3)
        self.rr(x, y, w, h, r=1)

        # Columna izquierda
        lx = x + 2
        ly = y + 2
        row_h = 6

        campos_izq = [
            ("SEÑOR(ES)", self.data.receptor_razon),
            ("DIRECCION", self.data.receptor_dir),
            ("R.U.T.", format_rut(self.data.receptor_rut)),
            ("GIRO", self.data.receptor_giro),
        ]

        for label, valor in campos_izq:
            self.set_font("Helvetica", "B", 8)
            self.set_xy(lx, ly)
            self.cell(22, row_h, label)
            self.set_font("Helvetica", "", 8)
            self.set_xy(lx + 22, ly)
            self.cell(75, row_h, f": {valor}")
            ly += row_h

        # Columna derecha
        rx = x + 105
        ry = y + 2

        # Formatear fecha dd-mm-aaaa
        fecha_display = self.data.fecha_emision
        if "-" in fecha_display:
            parts = fecha_display.split("-")
            if len(parts) == 3:
                fecha_display = f"{parts[2]}-{parts[1]}-{parts[0]}"

        campos_der = [
            ("FECHA EMISION", fecha_display),
            ("COMUNA", self.data.receptor_comuna),
            ("COND. DE PAGO", ""),
        ]

        for label, valor in campos_der:
            self.set_font("Helvetica", "B", 8)
            self.set_xy(rx, ry)
            self.cell(30, row_h, label)
            self.set_font("Helvetica", "", 8)
            self.set_xy(rx + 30, ry)
            self.cell(55, row_h, f": {valor}")
            ry += row_h

        return y + h + 3

    # ---------- INFO GUÍA DESPACHO ----------
    def _info_guia(self, y: float) -> float:
        traslado_desc = {
            "1": "1: Operación constituye venta",
            "2": "2: Ventas por efectuar",
            "5": "5: Traslado interno",
            "6": "6: Otros traslados no venta",
        }
        self.set_font("Helvetica", "B", 8)
        self.set_xy(self.LM, y)
        self.cell(30, 5, "TIPO TRASLADO")
        self.set_font("Helvetica", "", 8)
        self.cell(100, 5, f": {traslado_desc.get(self.data.ind_traslado, self.data.ind_traslado)}")
        return y + 6

    # ---------- DETALLE ----------
    def _detalle(self, y: float) -> float:
        x = self.LM
        w = self.PW

        # Definir columnas
        cols = [
            (x, 75, "DESCRIPCION DEL ARTICULO", "L"),
            (x + 75, 25, "CANTIDAD", "R"),
            (x + 100, 35, "PRECIO UNITARIO", "R"),
            (x + 135, 25, "DESC/EXE", "R"),
            (x + 160, 35.4, "VALOR", "R"),
        ]

        # Calcular dimensiones totales de la tabla
        n_rows = len(self.data.items) + len(self.data.descuentos_globales)
        row_h = 5
        header_h = 6
        min_rows = max(n_rows, 5)
        detail_h = min_rows * row_h
        total_h = header_h + detail_h

        # Dibujar rectángulo redondeado exterior de toda la tabla
        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.3)
        self.rr(x, y, w, total_h, r=1)

        # Encabezado (líneas interiores)
        self.set_fill_color(255, 255, 255)
        self.set_font("Helvetica", "B", 7)

        # Línea horizontal bajo encabezado
        self.line(x, y + header_h, x + w, y + header_h)

        # Separadores verticales de columnas
        for cx, cw, label, _ in cols:
            self.set_xy(cx, y)
            self.cell(cw, header_h, label, border=0, align="C")
            # Línea vertical (excepto borde izquierdo de primera y derecho de última)
            if cx > x:
                self.line(cx, y, cx, y + total_h)

        y += header_h

        # Dibujar items
        self.set_font("Helvetica", "", 8)
        for item in self.data.items:
            for cx, cw, _, align in cols:
                self.set_xy(cx, y)
                if cx == x:
                    val = item["nombre"]
                    if item["ind_exe"] == "1":
                        val += " (EXENTO)"
                    self.cell(cw, row_h, val, border=0)
                elif cx == x + 75:
                    val = item["qty"] if item["qty"] else ""
                    self.cell(cw, row_h, val, border=0, align="R")
                elif cx == x + 100:
                    val = format_number(item["precio"]) if item["precio"] else ""
                    self.cell(cw, row_h, val, border=0, align="R")
                elif cx == x + 135:
                    parts = []
                    if item["descuento_pct"]:
                        parts.append(f"D{item['descuento_pct']}%")
                    if item["ind_exe"] == "1":
                        parts.append("EX")
                    val = " ".join(parts)
                    self.cell(cw, row_h, val, border=0, align="R")
                elif cx == x + 160:
                    val = format_number(item["monto"]) if item["monto"] else "0"
                    self.cell(cw, row_h, val, border=0, align="R")
            y += row_h

        # Descuentos globales
        for desc in self.data.descuentos_globales:
            self.set_xy(x, y)
            tipo_val = "$" if desc["tipo_valor"] == "$" else "%"
            glosa = desc.get("glosa", "Descuento Global")
            self.cell(cols[0][1], row_h, f"  {glosa} ({tipo_val})", border=0)
            for cx, cw, _, _ in cols[1:-1]:
                self.set_xy(cx, y)
                self.cell(cw, row_h, "", border=0)
            self.set_xy(cols[-1][0], y)
            val = desc.get("valor", "0")
            self.cell(cols[-1][1], row_h, f"- {format_number(val)}", border=0, align="R")
            y += row_h

        # Filas vacías para completar espacio
        empty_rows = min_rows - n_rows
        for _ in range(empty_rows):
            y += row_h

        # Referencias (dentro o debajo de la tabla)
        if self.data.referencias:
            y += 1
            self.set_font("Helvetica", "I", 7)
            tipo_doc_nombres = {
                "33": "Factura Elect.", "34": "Factura Exenta",
                "52": "Guía Despacho", "56": "Nota Débito",
                "61": "Nota Crédito", "801": "Orden Compra", "SET": "SET",
            }
            for ref in self.data.referencias:
                self.set_xy(x, y)
                tipo_nombre = tipo_doc_nombres.get(ref["tipo_doc"], ref["tipo_doc"])
                texto = f"Ref: {tipo_nombre} N° {ref['folio']}"
                if ref["fecha"]:
                    texto += f" del {ref['fecha']}"
                if ref["razon"]:
                    texto += f" - {ref['razon']}"
                self.cell(w, 4, texto)
                y += 4

        return y + 2

    # ---------- SON: MONTO EN PALABRAS ----------
    def _son_pesos(self, y: float) -> float:
        x = self.LM
        w = self.PW

        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.3)
        self.rr(x, y, w, 6, r=1)

        self.set_font("Helvetica", "B", 8)
        self.set_xy(x + 2, y)
        palabras = monto_en_palabras(self.data.monto_total)
        self.cell(w - 4, 6, f"Son: {palabras}")

        return y + 8

    # ---------- TOTALES (fila horizontal) ----------
    def _totales(self, y: float) -> float:
        x = self.LM
        col_w = self.PW / 4
        total_h = 11

        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.3)

        # Rectángulo redondeado exterior
        self.rr(x, y, self.PW, total_h, r=1)

        # Separadores verticales internos
        for i in range(1, 4):
            sep_x = x + i * col_w
            self.line(sep_x, y, sep_x, y + total_h)

        totales = [
            ("Total Neto", format_number(self.data.monto_neto) if self.data.monto_neto else "0"),
            ("Total Exento", format_number(self.data.monto_exento) if self.data.monto_exento else "0"),
            (f"IVA ({self.data.tasa_iva or 19}.0%)", format_number(self.data.iva) if self.data.iva else "0"),
            ("Total", format_number(self.data.monto_total)),
        ]

        for i, (label, valor) in enumerate(totales):
            cx = x + i * col_w

            # Label
            self.set_font("Helvetica", "", 8)
            self.set_xy(cx, y)
            self.cell(col_w, 5, label, border=0, align="C")

            # Valor
            is_total = (i == 3)
            self.set_font("Helvetica", "B", 10 if is_total else 9)
            self.set_xy(cx, y + 5)
            self.cell(col_w, 6, f"$ {valor}", border=0, align="C")

        return y + total_h + 3

    # ---------- TIMBRE PDF417 ----------
    def _timbre(self):
        page_h = 279.4
        timbre_w = 82
        timbre_h = 30
        timbre_x = 10
        timbre_y = page_h - 56

        if self.data.ted_xml:
            try:
                ted_clean = self.data.ted_xml.strip()
                # CRÍTICO: Compactar el TED eliminando whitespace entre tags.
                # El SII verifica la firma FRMA del CAF contra el DA sin whitespace.
                # Si el CAF tiene newlines (del archivo XML), la FRMA no valida.
                ted_clean = re.sub(r'>\s+<', '><', ted_clean)
                # Codificar como bytes ISO-8859-1 para el PDF417
                ted_bytes = ted_clean.encode("ISO-8859-1")
                codes = encode(ted_bytes, columns=10, security_level=5)
                img = render_image(codes, scale=3, ratio=3, padding=2)

                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                img.save(tmp.name)
                tmp.close()

                self.image(tmp.name, x=timbre_x, y=timbre_y, w=timbre_w, h=timbre_h)
                os.unlink(tmp.name)
            except Exception as e:
                self.set_font("Helvetica", "", 6)
                self.set_xy(timbre_x, timbre_y)
                self.cell(timbre_w, 4, f"[Timbre Error: {e}]")

        self.set_font("Helvetica", "", 6)
        self.set_text_color(0, 0, 0)
        self.set_xy(timbre_x, timbre_y + timbre_h + 1)
        self.cell(timbre_w, 3, "Timbre Electr\u00f3nico SII", align="C")
        self.set_xy(timbre_x, timbre_y + timbre_h + 4)
        self.cell(timbre_w, 3,
                  f"Res. {NUMERO_RESOLUCION} del {FECHA_RESOLUCION}."
                  f" - Verifique documento: http://www.sii.cl",
                  align="C")

    # ---------- ACUSE DE RECIBO ----------
    def _acuse_recibo(self):
        page_h = 279.4
        ar_x = 95
        ar_y = page_h - 55
        ar_w = 110
        ar_h = 38

        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.3)
        self.rr(ar_x, ar_y, ar_w, ar_h, r=1)

        # Título
        self.set_font("Helvetica", "B", 7)
        self.set_text_color(0, 0, 0)
        self.set_xy(ar_x + 3, ar_y + 2)
        self.cell(ar_w - 6, 4, "Acuse de Recibo")

        # Texto legal
        self.set_font("Helvetica", "", 5.5)
        texto = (
            "El acuse de recibo que se declara en este acto, de acuerdo "
            "a lo dispuesto en la letra b) del Art. 4\u00b0, y la letra c) del "
            "Art. 5\u00b0 de la Ley 19.983, acredita que la entrega de "
            "mercader\u00edas o servicio(s) prestado(s) ha(n) sido recibido(s)."
        )
        self.set_xy(ar_x + 3, ar_y + 7)
        self.multi_cell(ar_w - 6, 2.8, texto)

        # Campos en grilla ordenada (2 columnas × 3 filas)
        col1_x = ar_x + 3
        col2_x = ar_x + 56
        field_w = 50
        row_h = 4
        cy = ar_y + 20

        self.set_font("Helvetica", "", 6)

        # Fila 1: Nombre / RUT
        self.set_xy(col1_x, cy)
        self.cell(field_w, row_h, "Nombre: ___________________________")
        self.set_xy(col2_x, cy)
        self.cell(field_w, row_h, "RUT: ______________________")
        cy += row_h + 1

        # Fila 2: Fecha / Recinto
        self.set_xy(col1_x, cy)
        self.cell(field_w, row_h, "Fecha:  ___________________________")
        self.set_xy(col2_x, cy)
        self.cell(field_w, row_h, "Recinto: ___________________")
        cy += row_h + 1

        # Fila 3: Firma centrada
        self.set_xy(col1_x, cy)
        self.cell(ar_w - 6, row_h, "Firma:  ___________________________")

    # ---------- LEYENDA CEDIBLE ----------
    def _leyenda_cedible(self):
        page_h = 279.4
        texto = "CEDIBLE CON SU FACTURA" if self.data.tipo_dte == 52 else "CEDIBLE"
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(0, 0, 0)
        self.set_xy(150, page_h - 12)
        self.cell(55, 5, texto, align="R")


# ===================== EXTRACTION & GENERATION =====================

def _extraer_teds_raw(xml_path: str) -> list[bytes]:
    """Extrae cada bloque <TED>...</TED> como bytes raw del XML firmado."""
    with open(xml_path, "rb") as f:
        raw = f.read()
    teds = []
    for m in re.finditer(rb'<TED version.*?</TED>', raw, re.DOTALL):
        teds.append(m.group(0))
    return teds


def extraer_dtes_de_envio(xml_path: str) -> list:
    tree = etree.parse(xml_path)
    root = tree.getroot()
    documentos = []
    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "Documento":
            documentos.append(el)
    return documentos


def generar_muestras(xml_files: list[str], output_dir: str, seleccion: dict = None):
    os.makedirs(output_dir, exist_ok=True)
    total_pdfs = 0

    for xml_file in xml_files:
        if not os.path.exists(xml_file):
            print(f"  SKIP (no existe): {xml_file}")
            continue
        print(f"\nProcesando: {Path(xml_file).name}")
        docs = extraer_dtes_de_envio(xml_file)
        teds_raw = _extraer_teds_raw(xml_file)
        print(f"  {len(docs)} documentos, {len(teds_raw)} TEDs raw")

        conteo_tipo = {}

        for idx, doc in enumerate(docs):
            data = parse_dte_xml(doc)
            # Asignar TED raw (bytes ISO-8859-1) al data
            if idx < len(teds_raw):
                data.ted_xml = teds_raw[idx].decode("ISO-8859-1")
            else:
                data.ted_xml = ""

            if seleccion:
                tipo = data.tipo_dte
                conteo_tipo[tipo] = conteo_tipo.get(tipo, 0) + 1
                if conteo_tipo[tipo] > seleccion.get(tipo, 0):
                    continue

            es_cedible = data.tipo_dte in TIPOS_CEDIBLES

            # Ejemplar tributario
            pdf = MuestraImpresaPDF(data, cedible=False)
            nombre = f"T{data.tipo_dte}_F{data.folio}_tributario.pdf"
            with open(os.path.join(output_dir, nombre), "wb") as f:
                f.write(pdf.generar())
            print(f"  ✓ {nombre}")
            total_pdfs += 1

            # Copia cedible
            if es_cedible:
                pdf_ced = MuestraImpresaPDF(data, cedible=True)
                nombre_ced = f"T{data.tipo_dte}_F{data.folio}_cedible.pdf"
                with open(os.path.join(output_dir, nombre_ced), "wb") as f:
                    f.write(pdf_ced.generar())
                print(f"  ✓ {nombre_ced}")
                total_pdfs += 1

    print(f"\n{'='*50}")
    print(f"Total PDFs: {total_pdfs}")
    print(f"Dir: {output_dir}")


def main():
    # Usar output de nuevapostulacion (donde están los XMLs firmados del nuevo set)
    output_base = str(Path(__file__).resolve().parent.parent.parent / "nuevapostulacion" / "output")

    set_pruebas_files = [
        os.path.join(output_base, "EnvioDTE_SetBasico_firmado.xml"),
        os.path.join(output_base, "EnvioDTE_SetExentas_firmado.xml"),
        os.path.join(output_base, "EnvioDTE_SetGuias_firmado.xml"),
    ]
    simulacion_files = [
        os.path.join(output_base, "simulacion", "EnvioSimulacion_firmado.xml"),
    ]

    muestras_dir = os.path.join(output_base, "muestras_impresas")

    print("=" * 60)
    print("MUESTRAS IMPRESAS - CERTIFICACIÓN SII")
    print("=" * 60)

    print("\n--- SET DE PRUEBAS (todos) ---")
    generar_muestras(set_pruebas_files, os.path.join(muestras_dir, "set_pruebas"))

    print("\n--- SET DE SIMULACIÓN (10 representativos) ---")
    generar_muestras(simulacion_files, os.path.join(muestras_dir, "simulacion"),
                     seleccion={33: 4, 34: 2, 52: 2, 61: 1, 56: 1})


if __name__ == "__main__":
    main()
