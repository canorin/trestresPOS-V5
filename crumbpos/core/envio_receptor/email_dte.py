"""Envio de DTE al receptor via email.

El SII exige que el emisor entregue el DTE al receptor electronico.
Este modulo envia el XML firmado como adjunto por email.
"""
import logging
import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TIPO_DTE_NOMBRES = {
    33: "Factura Electronica",
    34: "Factura No Afecta o Exenta",
    39: "Boleta Electronica",
    41: "Boleta Exenta Electronica",
    46: "Factura de Compra Electronica",
    52: "Guia de Despacho Electronica",
    56: "Nota de Debito Electronica",
    61: "Nota de Credito Electronica",
}


@dataclass
class EmailConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    from_email: str
    from_name: str
    use_tls: bool = True


def get_email_config_from_env() -> EmailConfig:
    """Build EmailConfig from environment variables."""
    return EmailConfig(
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        from_email=os.getenv("SMTP_FROM_EMAIL", ""),
        from_name=os.getenv("SMTP_FROM_NAME", ""),
        use_tls=True,
    )


def _build_html_body(
    emisor_razon: str,
    receptor_razon: str,
    tipo_dte: int,
    folio: int,
    monto_total: int,
) -> str:
    """Build professional HTML email body with document details."""
    tipo_nombre = TIPO_DTE_NOMBRES.get(tipo_dte, f"Documento Tipo {tipo_dte}")
    monto_fmt = f"${monto_total:,.0f}".replace(",", ".")

    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: Arial, Helvetica, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
  <div style="background: #1a1a2e; padding: 20px; text-align: center;">
    <h2 style="color: #ffffff; margin: 0;">{emisor_razon}</h2>
    <p style="color: #aaa; margin: 4px 0 0 0; font-size: 13px;">Documento Tributario Electronico</p>
  </div>
  <div style="padding: 24px; border: 1px solid #e0e0e0; border-top: none;">
    <p>Estimado/a <strong>{receptor_razon}</strong>,</p>
    <p>Adjunto encontrara el siguiente documento tributario electronico:</p>
    <table style="width: 100%; border-collapse: collapse; margin: 16px 0;">
      <tr style="background: #f5f5f5;">
        <td style="padding: 10px; border: 1px solid #ddd; font-weight: bold;">Tipo Documento</td>
        <td style="padding: 10px; border: 1px solid #ddd;">{tipo_nombre}</td>
      </tr>
      <tr>
        <td style="padding: 10px; border: 1px solid #ddd; font-weight: bold;">Folio</td>
        <td style="padding: 10px; border: 1px solid #ddd;">N&deg; {folio}</td>
      </tr>
      <tr style="background: #f5f5f5;">
        <td style="padding: 10px; border: 1px solid #ddd; font-weight: bold;">Monto Total</td>
        <td style="padding: 10px; border: 1px solid #ddd;">{monto_fmt}</td>
      </tr>
    </table>
    <p style="font-size: 13px; color: #666;">
      Este documento ha sido emitido de acuerdo a la normativa del Servicio de Impuestos Internos (SII) de Chile.
      El XML adjunto corresponde al DTE firmado electronicamente.
    </p>
    <p style="font-size: 13px; color: #666;">
      De acuerdo a la legislacion vigente, usted dispone de 8 dias desde la recepcion
      para aceptar o reclamar este documento.
    </p>
    <hr style="border: none; border-top: 1px solid #e0e0e0; margin: 20px 0;">
    <p style="font-size: 11px; color: #999; text-align: center;">
      Documento generado por {emisor_razon} &mdash; trestresPOS
    </p>
  </div>
</body>
</html>"""


def enviar_dte_email(
    config: EmailConfig,
    destinatario_email: str,
    emisor_razon: str,
    receptor_razon: str,
    tipo_dte: int,
    folio: int,
    monto_total: int,
    xml_bytes: bytes,
    pdf_bytes: bytes | None = None,
) -> dict:
    """Send DTE to receptor via email.

    Args:
        config: SMTP configuration
        destinatario_email: Receptor's email address
        emisor_razon: Emisor company name
        receptor_razon: Receptor company name
        tipo_dte: Document type (33, 34, etc.)
        folio: Document folio number
        monto_total: Total amount
        xml_bytes: Signed DTE XML as bytes
        pdf_bytes: Optional PDF representation

    Returns:
        dict with ok, mensaje, error fields
    """
    tipo_nombre = TIPO_DTE_NOMBRES.get(tipo_dte, f"Documento Tipo {tipo_dte}")

    try:
        # -- Build message --
        msg = MIMEMultipart("mixed")
        msg["From"] = f"{config.from_name} <{config.from_email}>"
        msg["To"] = destinatario_email
        msg["Subject"] = f"{tipo_nombre} N\u00b0 {folio} - {emisor_razon}"

        # HTML body
        html_body = _build_html_body(
            emisor_razon=emisor_razon,
            receptor_razon=receptor_razon,
            tipo_dte=tipo_dte,
            folio=folio,
            monto_total=monto_total,
        )
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        # XML attachment
        xml_attachment = MIMEApplication(xml_bytes, _subtype="xml")
        xml_filename = f"DTE_T{tipo_dte}_F{folio}.xml"
        xml_attachment.add_header(
            "Content-Disposition", "attachment", filename=xml_filename,
        )
        msg.attach(xml_attachment)

        # Optional PDF attachment
        if pdf_bytes:
            pdf_attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
            pdf_filename = f"DTE_T{tipo_dte}_F{folio}.pdf"
            pdf_attachment.add_header(
                "Content-Disposition", "attachment", filename=pdf_filename,
            )
            msg.attach(pdf_attachment)

        # -- Send via SMTP --
        logger.info(
            "Enviando DTE T%d F%d a %s via %s:%d",
            tipo_dte, folio, destinatario_email, config.smtp_host, config.smtp_port,
        )

        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as server:
            if config.use_tls:
                server.starttls()
            server.login(config.smtp_user, config.smtp_password)
            server.send_message(msg)

        logger.info(
            "DTE T%d F%d enviado exitosamente a %s",
            tipo_dte, folio, destinatario_email,
        )
        return {
            "ok": True,
            "mensaje": f"{tipo_nombre} N\u00b0 {folio} enviado a {destinatario_email}",
            "error": None,
        }

    except smtplib.SMTPAuthenticationError as exc:
        logger.error("Error autenticacion SMTP: %s", exc)
        return {
            "ok": False,
            "mensaje": "Error de autenticacion con servidor de correo",
            "error": str(exc),
        }
    except smtplib.SMTPException as exc:
        logger.error("Error SMTP enviando DTE T%d F%d: %s", tipo_dte, folio, exc)
        return {
            "ok": False,
            "mensaje": f"Error SMTP al enviar {tipo_nombre} N\u00b0 {folio}",
            "error": str(exc),
        }
    except Exception as exc:
        logger.exception("Error inesperado enviando DTE T%d F%d", tipo_dte, folio)
        return {
            "ok": False,
            "mensaje": f"Error inesperado al enviar {tipo_nombre} N\u00b0 {folio}",
            "error": str(exc),
        }
