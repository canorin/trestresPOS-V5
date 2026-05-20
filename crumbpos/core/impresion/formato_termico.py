"""Formato térmico 80mm para impresoras POS.

Layout optimizado para rollo térmico de 80mm (ancho útil ~72mm).
Altura variable según contenido.
Sin copia cedible (no aplica en punto de venta).
"""
import os

from fpdf import FPDF

from .base import (
    DTEPrintData, TIPO_NOMBRE, LOGO_PATH,
    format_rut, format_number, fecha_display,
    generar_imagen_timbre,
)


class PDFTermico(FPDF):
    """PDF para impresora térmica de 80mm.

    Dimensiones:
    - Ancho papel: 80mm
    - Ancho útil: 72mm (4mm margen cada lado)
    - Alto: variable (se calcula según contenido)

    Diseño simplificado en una sola columna.
    """

    # Constantes de layout
    PAPER_W = 80          # ancho papel mm
    MARGIN = 4            # margen lateral mm
    USABLE_W = 72         # ancho útil mm
    LINE_H = 3.5          # altura línea normal mm
    SECTION_GAP = 2       # espacio entre secciones mm

    # Boletas: layout simplificado
    TIPOS_BOLETA = {39, 41}

    def __init__(self, data: DTEPrintData):
        self.es_boleta = data.tipo_dte in self.TIPOS_BOLETA

        n_items = len(data.items)
        n_refs = len(data.referencias)
        n_desc = len(data.descuentos_globales)

        if self.es_boleta:
            # Boleta: encabezado + logo + emisor + detalle + totales + timbre PDF417
            # Igual que DTE normal — las boletas también requieren PDF417
            # impreso (manual_muestras_impresas.pdf §3, Boletas Electrónicas).
            estimated_h = 140 + (n_items * 12) + (n_desc * 5) + 20
        else:
            # DTE normal: recuadro + logo + emisor + receptor + items + timbre barcode
            estimated_h = 160 + (n_items * 12) + (n_refs * 5) + (n_desc * 5)

        super().__init__(orientation="P", unit="mm", format=(self.PAPER_W, max(estimated_h, 120)))
        self.data = data
        self.set_auto_page_break(auto=False)

    def generar(self) -> bytes:
        self.add_page()
        self.set_margins(self.MARGIN, self.MARGIN, self.MARGIN)

        y = self.MARGIN

        # Encabezado igual para todos: recuadro + logo + emisor
        y = self._encabezado(y)
        y = self._separador(y)

        if self.es_boleta:
            # Boleta: receptor simplificado (solo RUT + Fecha)
            y = self._receptor_boleta(y)
        else:
            y = self._receptor(y)
        y = self._separador(y)

        if self.data.tipo_dte == 52 and self.data.ind_traslado:
            y = self._info_guia(y)

        y = self._detalle(y)
        y = self._separador(y)
        y = self._totales(y)
        y = self._separador(y)

        if not self.es_boleta and self.data.referencias:
            y = self._referencias(y)
            y = self._separador(y)

        # Timbre PDF417 — obligatorio para TODOS los DTE incluyendo
        # boletas electrónicas (manual_muestras_impresas.pdf §3).
        y = self._timbre(y)

        y = self._pie(y)

        return self.output()

    def _separador(self, y: float) -> float:
        """Línea punteada separadora."""
        self.set_draw_color(150, 150, 150)
        self.set_line_width(0.1)
        # Simular línea punteada con segmentos cortos
        x = self.MARGIN
        x_end = self.MARGIN + self.USABLE_W
        while x < x_end:
            self.line(x, y, min(x + 1, x_end), y)
            x += 2
        return y + self.SECTION_GAP

    def _texto_centrado(self, y: float, texto: str, size: int = 8, bold: bool = False) -> float:
        style = "B" if bold else ""
        self.set_font("Helvetica", style, size)
        self.set_xy(self.MARGIN, y)
        self.cell(self.USABLE_W, self.LINE_H, texto, align="C")
        return y + self.LINE_H

    def _texto_izq(self, y: float, texto: str, size: int = 7, bold: bool = False) -> float:
        style = "B" if bold else ""
        self.set_font("Helvetica", style, size)
        self.set_xy(self.MARGIN, y)
        self.cell(self.USABLE_W, self.LINE_H, texto)
        return y + self.LINE_H

    def _texto_dos_cols(self, y: float, izq: str, der: str, size: int = 7) -> float:
        self.set_font("Helvetica", "", size)
        self.set_xy(self.MARGIN, y)
        self.cell(self.USABLE_W / 2, self.LINE_H, izq)
        self.set_xy(self.MARGIN + self.USABLE_W / 2, y)
        self.cell(self.USABLE_W / 2, self.LINE_H, der, align="R")
        return y + self.LINE_H

    # ---------- RECEPTOR BOLETA (simplificado) ----------
    def _receptor_boleta(self, y: float) -> float:
        """Receptor simplificado para boletas: solo RUT emisor y Fecha."""
        campos = [
            ("RUT", format_rut(self.data.emisor_rut)),
            ("Fecha", fecha_display(self.data.fecha_emision)),
        ]
        for label, valor in campos:
            if valor:
                self.set_font("Helvetica", "B", 7)
                self.set_xy(self.MARGIN, y)
                self.cell(14, self.LINE_H, f"{label}:")
                self.set_font("Helvetica", "", 7)
                self.set_xy(self.MARGIN + 14, y)
                self.cell(self.USABLE_W - 14, self.LINE_H, valor)
                y += self.LINE_H
        return y

    # ---------- ENCABEZADO ----------
    def _encabezado(self, y: float) -> float:
        x = self.MARGIN

        # ---- RECUADRO NEGRO (como carta pero negro) ----
        box_w = self.USABLE_W
        box_h = 24
        box_x = x

        self.set_draw_color(0, 0, 0)
        self.set_line_width(0.8)
        self.rect(box_x, y, box_w, box_h)

        self.set_text_color(0, 0, 0)
        self.set_font("Helvetica", "B", 9)
        self.set_xy(box_x, y + 2)
        self.cell(box_w, 4, f"R.U.T.: {format_rut(self.data.emisor_rut)}", align="C")

        tipo_lineas = TIPO_NOMBRE.get(self.data.tipo_dte, ("DOCUMENTO", "ELECTRONICO"))
        self.set_font("Helvetica", "B", 10)
        self.set_xy(box_x, y + 7)
        self.cell(box_w, 5, f"{tipo_lineas[0]} {tipo_lineas[1]}", align="C")

        self.set_font("Helvetica", "B", 11)
        self.set_xy(box_x, y + 14)
        self.cell(box_w, 5, f"N\u00ba {self.data.folio}", align="C")

        # Unidad SII (Dirección Regional), debajo del recuadro.
        # Corresponde a la casa matriz (manual_muestras_impresas §1.1.4).
        self.set_font("Helvetica", "", 6)
        self.set_xy(box_x, y + box_h + 1)
        _unidad = self.data.emisor_unidad_sii or "SII"
        self.cell(box_w, 3, f"S.I.I. - {_unidad}", align="C")

        y += box_h + 5

        # ---- LOGO centrado, 80% del ancho ----
        logo = self.data.logo_path or LOGO_PATH
        if logo and os.path.exists(logo):
            logo_w = self.USABLE_W * 0.8
            logo_x = x + (self.USABLE_W - logo_w) / 2
            from PIL import Image as PILImage
            with PILImage.open(logo) as img:
                aspect = img.size[0] / img.size[1]
            logo_h = logo_w / aspect
            self.image(logo, x=logo_x, y=y, w=logo_w, h=logo_h)
            y += logo_h + 2

        # ---- Info emisor ----
        y = self._texto_centrado(y, self.data.emisor_razon, size=9, bold=True)
        y = self._texto_centrado(y, self.data.emisor_giro, size=7)
        # Dirección casa matriz (siempre etiquetada en térmico)
        cm_parts = [self.data.emisor_dir, self.data.emisor_comuna]
        cm_text = "Casa Matriz: " + ", ".join(p for p in cm_parts if p)
        y = self._texto_centrado(y, cm_text, size=6)
        # Sucursal (solo si difiere de la casa matriz)
        if self.data.emisor_sucursal_dir:
            suc_parts = [self.data.emisor_sucursal_dir, self.data.emisor_sucursal_comuna]
            suc_text = "Sucursal: " + ", ".join(p for p in suc_parts if p)
            y = self._texto_centrado(y, suc_text, size=6)

        return y + self.SECTION_GAP

    # ---------- RECEPTOR ----------
    def _receptor(self, y: float) -> float:
        campos = [
            ("Receptor", self.data.receptor_razon),
            ("RUT", format_rut(self.data.receptor_rut)),
            ("Giro", self.data.receptor_giro),
            ("Dir.", self.data.receptor_dir),
            ("Comuna", self.data.receptor_comuna),
            ("Fecha", fecha_display(self.data.fecha_emision)),
        ]
        for label, valor in campos:
            if valor:
                self.set_font("Helvetica", "B", 7)
                self.set_xy(self.MARGIN, y)
                self.cell(14, self.LINE_H, f"{label}:")
                self.set_font("Helvetica", "", 7)
                self.set_xy(self.MARGIN + 14, y)
                self.cell(self.USABLE_W - 14, self.LINE_H, valor)
                y += self.LINE_H

        return y

    # ---------- INFO GUÍA ----------
    def _info_guia(self, y: float) -> float:
        traslado_desc = {
            "1": "Venta", "2": "Ventas por efectuar",
            "5": "Traslado interno", "6": "Otros traslados",
        }
        desc = traslado_desc.get(self.data.ind_traslado, self.data.ind_traslado or "")
        y = self._texto_izq(y, f"Traslado: {desc}", size=7, bold=True)
        return y + 1

    # ---------- DETALLE ----------
    def _detalle(self, y: float) -> float:
        # Encabezado tabla compacta
        self.set_font("Helvetica", "B", 6)
        col_desc_w = 32
        col_qty_w = 8
        col_prc_w = 14
        col_tot_w = 18

        self.set_xy(self.MARGIN, y)
        self.cell(col_desc_w, self.LINE_H, "ARTICULO")
        self.cell(col_qty_w, self.LINE_H, "CANT", align="R")
        self.cell(col_prc_w, self.LINE_H, "P.UNIT", align="R")
        self.cell(col_tot_w, self.LINE_H, "TOTAL", align="R")
        y += self.LINE_H

        # Línea bajo encabezado
        self.set_line_width(0.1)
        self.line(self.MARGIN, y, self.MARGIN + self.USABLE_W, y)
        y += 0.5

        # Items — una línea si cabe, dos si el nombre es largo
        self.set_font("Helvetica", "", 6)
        for item in self.data.items:
            nombre = item.get("nombre", "")
            qty = item.get("qty", "")
            precio = format_number(item.get("precio")) if item.get("precio") else ""
            monto = format_number(item.get("monto")) if item.get("monto") else "0"

            if self.get_string_width(nombre) <= col_desc_w:
                # Cabe en una línea
                self.set_xy(self.MARGIN, y)
                self.cell(col_desc_w, self.LINE_H, nombre)
                self.cell(col_qty_w, self.LINE_H, str(qty), align="R")
                self.cell(col_prc_w, self.LINE_H, precio, align="R")
                self.cell(col_tot_w, self.LINE_H, monto, align="R")
                y += self.LINE_H
            else:
                # Nombre largo: partir en dos líneas, números alineados a la primera
                palabras = nombre.split()
                linea1 = ""
                for p in palabras:
                    test = f"{linea1} {p}".strip() if linea1 else p
                    if self.get_string_width(test) > col_desc_w:
                        break
                    linea1 = test
                linea2 = nombre[len(linea1):].strip()

                # Primera línea: nombre + números alineados aquí
                self.set_xy(self.MARGIN, y)
                self.cell(col_desc_w, self.LINE_H, linea1)
                self.cell(col_qty_w, self.LINE_H, str(qty), align="R")
                self.cell(col_prc_w, self.LINE_H, precio, align="R")
                self.cell(col_tot_w, self.LINE_H, monto, align="R")
                y += 2.8  # interlineado reducido entre líneas del mismo nombre

                # Segunda línea: solo resto del nombre
                self.set_xy(self.MARGIN, y)
                self.cell(col_desc_w, self.LINE_H, linea2)
                y += self.LINE_H

            # Espacio entre artículos
            y += 1.5

            # Indicadores bajo el item
            extras = []
            if item.get("ind_exe") == "1":
                extras.append("EXENTO")
            if item.get("descuento_pct"):
                extras.append(f"Desc: {item['descuento_pct']}%")
            if extras:
                self.set_font("Helvetica", "I", 5)
                self.set_xy(self.MARGIN + 2, y)
                self.cell(self.USABLE_W, 2.5, "  ".join(extras))
                y += 2.5
                self.set_font("Helvetica", "", 6)

        # Descuentos globales
        for desc in self.data.descuentos_globales:
            glosa = desc.get("glosa", "Descuento")
            valor = desc.get("valor", "0")
            tipo_val = "%" if desc.get("tipo_valor") == "%" else "$"
            self.set_font("Helvetica", "I", 6)
            self.set_xy(self.MARGIN, y)
            self.cell(self.USABLE_W - col_tot_w, self.LINE_H, f"  {glosa} ({tipo_val})")
            self.cell(col_tot_w, self.LINE_H, f"-{format_number(valor)}", align="R")
            y += self.LINE_H

        return y

    # ---------- TOTALES ----------
    def _totales(self, y: float) -> float:
        filas = []
        if self.data.monto_neto:
            filas.append(("Neto", format_number(self.data.monto_neto)))
        if self.data.monto_exento:
            filas.append(("Exento", format_number(self.data.monto_exento)))
        if self.data.iva:
            filas.append((f"IVA ({self.data.tasa_iva or 19}%)", format_number(self.data.iva)))

        for label, valor in filas:
            self.set_font("Helvetica", "", 7)
            self.set_xy(self.MARGIN, y)
            self.cell(self.USABLE_W - 25, self.LINE_H, label, align="R")
            self.cell(25, self.LINE_H, f"$ {valor}", align="R")
            y += self.LINE_H

        # Total grande
        y += 0.5
        self.set_font("Helvetica", "B", 10)
        self.set_xy(self.MARGIN, y)
        self.cell(self.USABLE_W - 30, 5, "TOTAL", align="R")
        self.cell(30, 5, f"$ {format_number(self.data.monto_total)}", align="R")
        y += 6

        return y

    # ---------- REFERENCIAS ----------
    def _referencias(self, y: float) -> float:
        tipo_doc_nombres = {
            "33": "Fact.", "34": "F.Exenta",
            "52": "Guía", "56": "N.Déb.",
            "61": "N.Créd.", "801": "O.Compra",
        }
        self.set_font("Helvetica", "I", 6)
        for ref in self.data.referencias:
            tipo = tipo_doc_nombres.get(ref.get("tipo_doc", ""), ref.get("tipo_doc", ""))
            texto = f"Ref: {tipo} N°{ref.get('folio', '')}"
            if ref.get("razon"):
                texto += f" - {ref['razon']}"
            self.set_xy(self.MARGIN, y)
            self.cell(self.USABLE_W, self.LINE_H, texto)
            y += self.LINE_H

        return y

    # ---------- TIMBRE PDF417 ----------
    def _timbre(self, y: float) -> float:
        # PDF417 para 80mm: 8 columnas (6 genera demasiadas filas)
        timbre_w = 68
        timbre_h = 25
        timbre_x = self.MARGIN + (self.USABLE_W - timbre_w) / 2

        img_path = generar_imagen_timbre(self.data.ted_xml, columns=8, scale=2)
        if img_path:
            self.image(img_path, x=timbre_x, y=y, w=timbre_w, h=timbre_h)
            os.unlink(img_path)
            y += timbre_h + 1

        self.set_font("Helvetica", "", 5)
        self.set_text_color(0, 0, 0)
        self.set_xy(self.MARGIN, y)
        self.cell(self.USABLE_W, 2.5, "Timbre Electr\u00f3nico SII", align="C")
        y += 2.5
        self.set_xy(self.MARGIN, y)
        self.cell(self.USABLE_W, 2.5,
                  f"Res. {self.data.numero_resolucion} del {self.data.fecha_resolucion}"
                  f" - Verifique: www.sii.cl",
                  align="C")
        y += 3

        return y

    # ---------- PIE ----------
    def _pie(self, y: float) -> float:
        self.set_font("Helvetica", "", 5)
        self.set_xy(self.MARGIN, y)
        self.cell(self.USABLE_W, 2.5, "Verifique documento: www.sii.cl", align="C")
        y += 3
        return y
