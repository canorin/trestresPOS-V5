"""Formato carta (215.9 x 279.4 mm) para impresoras láser/inkjet.

Layout estándar SII con recuadro rojo, tabla de detalle,
timbre PDF417, acuse de recibo y leyenda cedible.
"""
import os

from fpdf import FPDF

from .base import (
    DTEPrintData, TIPO_NOMBRE, TIPOS_CEDIBLES, LOGO_PATH,
    CRUMB_LOGO_PATH,
    format_rut, format_number, monto_en_palabras, fecha_display,
    generar_imagen_timbre,
)


class PDFCarta(FPDF):

    def __init__(self, data: DTEPrintData, cedible: bool = False):
        super().__init__(orientation="P", unit="mm", format="letter")
        self.data = data
        self.cedible = cedible
        self.set_auto_page_break(auto=False)
        self.LM = 10
        self.PW = 195.4  # letter=215.9 - 2*10.25

    def _lleva_cedible(self) -> bool:
        """Determina si este DTE debe incluir página cedible.

        Regla SII (manual_muestras_impresas.pdf pág 8):
        - Tipos 33, 34, 43, 46, 52 llevan cedible.
        - Excepción: guías de despacho (52) con IndTraslado 5 (traslado
          interno) o 6 (otros traslados no venta) NO llevan cedible.
          Solo las guías que constituyen venta o venta por efectuar
          requieren cedible para el acuse de recibo.
        """
        if self.data.tipo_dte not in TIPOS_CEDIBLES:
            return False
        if self.data.tipo_dte == 52 and self.data.ind_traslado in ("5", "6"):
            return False
        return True

    def _render_page(self, es_cedible: bool = False):
        """Renderiza una página completa del DTE."""
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
        self._logo_crumb()

        if es_cedible and self._lleva_cedible():
            self._acuse_recibo()
            self._leyenda_cedible()

    def generar(self) -> bytes:
        # Página 1: Original
        self._render_page(es_cedible=False)

        # Página 2: Cedible (solo para tipos con cedible, excluyendo
        # guías de traslado interno/no-venta)
        if self._lleva_cedible():
            self._render_page(es_cedible=True)

        return self.output()

    # ---------- ENCABEZADO ----------
    def _encabezado(self, y: float) -> float:
        x = self.LM

        # Recuadro rojo (derecha)
        box_x = 138
        box_y = 10
        box_w = 67
        box_h = 32

        # Logo
        logo = self.data.logo_path or LOGO_PATH
        max_logo_w = box_w * 0.8
        max_logo_h = max_logo_w * 0.5
        if logo and os.path.exists(logo):
            from PIL import Image as PILImage
            with PILImage.open(logo) as img:
                img_w, img_h = img.size
            aspect = img_w / img_h
            if aspect >= (max_logo_w / max_logo_h):
                logo_w = max_logo_w
                logo_h = logo_w / aspect
            else:
                logo_h = max_logo_h
                logo_w = logo_h * aspect
            self.image(logo, x=x, y=y, w=logo_w, h=logo_h)
            y += logo_h + 2

        # Razón social
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

        # Dirección casa matriz (siempre etiquetada)
        cm_parts = [self.data.emisor_dir, self.data.emisor_comuna, self.data.emisor_ciudad]
        cm_text = "Casa Matriz: " + ", ".join(p for p in cm_parts if p)
        self.set_xy(x, y)
        self.cell(120, 4, cm_text)

        # Sucursal (solo si difiere de la casa matriz)
        if self.data.emisor_sucursal_dir:
            y += 4
            suc_parts = [
                self.data.emisor_sucursal_dir,
                self.data.emisor_sucursal_comuna,
                self.data.emisor_sucursal_ciudad,
            ]
            suc_text = "Sucursal: " + ", ".join(p for p in suc_parts if p)
            self.set_xy(x, y)
            self.cell(120, 4, suc_text)

        # Recuadro rojo
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

        # Unidad SII (Dirección Regional), debajo del recuadro rojo.
        # Corresponde a la casa matriz (manual_muestras_impresas §1.1.4).
        self.set_text_color(255, 0, 0)
        self.set_font("Helvetica", "", 9)
        self.set_xy(box_x, box_y + box_h + 2)
        _unidad = self.data.emisor_unidad_sii or "SII"
        self.cell(box_w, 4, f"S.I.I. - {_unidad}", align="C")

        self.set_text_color(0, 0, 0)
        return box_y + box_h + 22

    # ---------- RECEPTOR ----------
    def _receptor(self, y: float) -> float:
        x = self.LM
        w = self.PW
        h = 30

        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.3)
        self.rect(x, y, w, h, round_corners=True, corner_radius=1)

        lx = x + 2
        ly = y + 2
        row_h = 6

        for label, valor in [
            ("SEÑOR(ES)", self.data.receptor_razon),
            ("DIRECCION", self.data.receptor_dir),
            ("R.U.T.", format_rut(self.data.receptor_rut)),
            ("GIRO", self.data.receptor_giro),
        ]:
            self.set_font("Helvetica", "B", 8)
            self.set_xy(lx, ly)
            self.cell(22, row_h, label)
            self.set_font("Helvetica", "", 8)
            self.set_xy(lx + 22, ly)
            self.cell(75, row_h, f": {valor}")
            ly += row_h

        rx = x + 105
        ry = y + 2

        # Construir condición de pago
        cond_pago = ""
        if self.data.fma_pago == 1:
            cond_pago = "Contado"
        elif self.data.fma_pago == 2:
            cond_pago = "Credito"
            if self.data.fecha_vencimiento:
                cond_pago += f" - Venc. {fecha_display(self.data.fecha_vencimiento)}"

        for label, valor in [
            ("FECHA EMISION", fecha_display(self.data.fecha_emision)),
            ("COMUNA", self.data.receptor_comuna),
            ("COND. DE PAGO", cond_pago),
        ]:
            self.set_font("Helvetica", "B", 8)
            self.set_xy(rx, ry)
            self.cell(30, row_h, label)
            self.set_font("Helvetica", "", 8)
            self.set_xy(rx + 30, ry)
            self.cell(55, row_h, f": {valor}")
            ry += row_h

        return y + h + 3

    # ---------- INFO GUÍA ----------
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
    # LÍMITE 2026-05-28: máximo 20 líneas de detalle para DTEs carta
    # (T33/T34/T52/T56/T61). Boletas excluidas: formato térmico.
    # Con encabezado hasta y≈97mm y timbre fijo a y=223.4mm, quedan 96mm útiles
    # para la tabla. A 5mm/fila + 6mm de header = 19 filas antes del timbre.
    # El límite duro (MAX_ITEMS_FACTURA_CARTA=20) se aplica upstream en
    # ServicioEmisionDTE._validar_request, ANTES de consumir folio, para que
    # nunca lleguen más de 20 ítems aquí. Este método NO corta ni pagina.
    def _detalle(self, y: float) -> float:
        x = self.LM
        w = self.PW

        cols = [
            (x, 75, "DESCRIPCION DEL ARTICULO", "L"),
            (x + 75, 25, "CANTIDAD", "R"),
            (x + 100, 35, "PRECIO UNITARIO", "R"),
            (x + 135, 25, "DESC/EXE", "R"),
            (x + 160, 35.4, "VALOR", "R"),
        ]

        n_rows = len(self.data.items) + len(self.data.descuentos_globales)
        row_h = 5
        header_h = 6
        min_rows = max(n_rows, 5)
        detail_h = min_rows * row_h
        total_h = header_h + detail_h

        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.3)
        self.rect(x, y, w, total_h, round_corners=True, corner_radius=1)

        self.set_font("Helvetica", "B", 7)
        self.line(x, y + header_h, x + w, y + header_h)

        for cx, cw, label, _ in cols:
            self.set_xy(cx, y)
            self.cell(cw, header_h, label, align="C")
            if cx > x:
                self.line(cx, y, cx, y + total_h)

        y += header_h

        self.set_font("Helvetica", "", 8)
        for item in self.data.items:
            for cx, cw, _, align in cols:
                self.set_xy(cx, y)
                if cx == x:
                    val = item["nombre"]
                    if item.get("ind_exe") == "1":
                        val += " (EXENTO)"
                    self.cell(cw, row_h, val)
                elif cx == x + 75:
                    self.cell(cw, row_h, item.get("qty", ""), align="R")
                elif cx == x + 100:
                    val = format_number(item["precio"]) if item.get("precio") else ""
                    self.cell(cw, row_h, val, align="R")
                elif cx == x + 135:
                    parts = []
                    if item.get("descuento_pct"):
                        parts.append(f"D{item['descuento_pct']}%")
                    if item.get("ind_exe") == "1":
                        parts.append("EX")
                    self.cell(cw, row_h, " ".join(parts), align="R")
                elif cx == x + 160:
                    val = format_number(item["monto"]) if item.get("monto") else "0"
                    self.cell(cw, row_h, val, align="R")
            y += row_h

        for desc in self.data.descuentos_globales:
            self.set_xy(x, y)
            tipo_val = "$" if desc.get("tipo_valor") == "$" else "%"
            glosa = desc.get("glosa", "Descuento Global")
            self.cell(cols[0][1], row_h, f"  {glosa} ({tipo_val})")
            for cx, cw, _, _ in cols[1:-1]:
                self.set_xy(cx, y)
                self.cell(cw, row_h, "")
            self.set_xy(cols[-1][0], y)
            self.cell(cols[-1][1], row_h, f"- {format_number(desc.get('valor', '0'))}", align="R")
            y += row_h

        empty_rows = min_rows - n_rows
        for _ in range(empty_rows):
            y += row_h

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
                tipo_key = ref.get("tipo_doc", ref.get("tipo", ""))
                tipo_nombre = tipo_doc_nombres.get(tipo_key, tipo_key)
                texto = f"Ref: {tipo_nombre} N° {ref.get('folio', '')}"
                if ref.get("fecha"):
                    texto += f" del {ref['fecha']}"
                if ref.get("razon"):
                    texto += f" - {ref['razon']}"
                self.cell(w, 4, texto)
                y += 4

        return y + 2

    # ---------- SON PESOS ----------
    def _son_pesos(self, y: float) -> float:
        x = self.LM
        w = self.PW

        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.3)
        self.rect(x, y, w, 6, round_corners=True, corner_radius=1)

        self.set_font("Helvetica", "B", 8)
        self.set_xy(x + 2, y)
        self.cell(w - 4, 6, f"Son: {monto_en_palabras(self.data.monto_total)}")
        return y + 8

    # ---------- TOTALES ----------
    def _totales(self, y: float) -> float:
        x = self.LM
        col_w = self.PW / 4
        total_h = 11

        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.3)
        self.rect(x, y, self.PW, total_h, round_corners=True, corner_radius=1)

        for i in range(1, 4):
            self.line(x + i * col_w, y, x + i * col_w, y + total_h)

        totales = [
            ("Total Neto", format_number(self.data.monto_neto) if self.data.monto_neto else "0"),
            ("Total Exento", format_number(self.data.monto_exento) if self.data.monto_exento else "0"),
            (f"IVA ({self.data.tasa_iva or 19}.0%)", format_number(self.data.iva) if self.data.iva else "0"),
            ("Total", format_number(self.data.monto_total)),
        ]

        for i, (label, valor) in enumerate(totales):
            cx = x + i * col_w
            self.set_font("Helvetica", "", 8)
            self.set_xy(cx, y)
            self.cell(col_w, 5, label, align="C")
            self.set_font("Helvetica", "B", 10 if i == 3 else 9)
            self.set_xy(cx, y + 5)
            self.cell(col_w, 6, f"$ {valor}", align="C")

        return y + total_h + 3

    # ---------- TIMBRE PDF417 ----------
    def _timbre(self):
        page_h = 279.4
        timbre_w = 82
        timbre_h = 30
        timbre_x = 10
        timbre_y = page_h - 56

        img_path = generar_imagen_timbre(self.data.ted_xml, columns=10, scale=3)
        if img_path:
            self.image(img_path, x=timbre_x, y=timbre_y, w=timbre_w, h=timbre_h)
            os.unlink(img_path)

        self.set_font("Helvetica", "", 6)
        self.set_text_color(0, 0, 0)
        self.set_xy(timbre_x, timbre_y + timbre_h + 1)
        self.cell(timbre_w, 3, "Timbre Electr\u00f3nico SII", align="C")
        self.set_xy(timbre_x, timbre_y + timbre_h + 4)
        self.cell(timbre_w, 3,
                  f"Res. {self.data.numero_resolucion} del {self.data.fecha_resolucion}."
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
        self.rect(ar_x, ar_y, ar_w, ar_h, round_corners=True, corner_radius=1)

        self.set_font("Helvetica", "B", 7)
        self.set_text_color(0, 0, 0)
        self.set_xy(ar_x + 3, ar_y + 2)
        self.cell(ar_w - 6, 4, "Acuse de Recibo")

        self.set_font("Helvetica", "", 5.5)
        texto = (
            "El acuse de recibo que se declara en este acto, de acuerdo "
            "a lo dispuesto en la letra b) del Art. 4\u00b0, y la letra c) del "
            "Art. 5\u00b0 de la Ley 19.983, acredita que la entrega de "
            "mercader\u00edas o servicio(s) prestado(s) ha(n) sido recibido(s)."
        )
        self.set_xy(ar_x + 3, ar_y + 7)
        self.multi_cell(ar_w - 6, 2.8, texto)

        col1_x = ar_x + 3
        col2_x = ar_x + 56
        row_h = 4
        cy = ar_y + 20

        self.set_font("Helvetica", "", 6)
        self.set_xy(col1_x, cy)
        self.cell(50, row_h, "Nombre: ___________________________")
        self.set_xy(col2_x, cy)
        self.cell(50, row_h, "RUT: ______________________")
        cy += row_h + 1

        self.set_xy(col1_x, cy)
        self.cell(50, row_h, "Fecha:  ___________________________")
        self.set_xy(col2_x, cy)
        self.cell(50, row_h, "Recinto: ___________________")
        cy += row_h + 1

        self.set_xy(col1_x, cy)
        self.cell(ar_w - 6, row_h, "Firma:  ___________________________")

    # ---------- LOGO CRUMB (pie inferior derecho, todas las páginas) ----------
    def _logo_crumb(self):
        if not os.path.exists(CRUMB_LOGO_PATH):
            return
        page_h = 279.4
        logo_w = 18  # ~50 px a 72 dpi
        logo_x = self.LM + self.PW - logo_w  # alineado al margen derecho
        logo_y = page_h - 9
        self.image(CRUMB_LOGO_PATH, x=logo_x, y=logo_y, w=logo_w)

    # ---------- LEYENDA CEDIBLE ----------
    def _leyenda_cedible(self):
        page_h = 279.4
        texto = "CEDIBLE CON SU FACTURA" if self.data.tipo_dte == 52 else "CEDIBLE"
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(0, 0, 0)
        # Desplazado 4mm arriba para no solapar el logo CrumbPOS
        self.set_xy(150, page_h - 16)
        self.cell(55, 5, texto, align="R")
