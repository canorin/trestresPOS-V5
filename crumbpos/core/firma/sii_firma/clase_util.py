# -#- coding: utf-8 -#-
import pytz
from datetime import datetime
import os
import re
from lxml import etree
import collections
import decimal
import logging
_logger = logging.getLogger(__name__)


class UserError(Exception):
    """Clase perdida"""
    pass

def round0(val):
    decimal.getcontext().rounding = decimal.ROUND_HALF_UP
    return int(decimal.Decimal(val).to_integral_value())

def set_from_keys(obj, vals={}, priorizar=[]):
    for p in priorizar:
        val = vals.get(p)
        if val:
            if hasattr(obj, p):
                setattr(obj, p, val)
                del vals[p]
            else:
                _logger.warning("Atributo no encontrado en %s: %s" % (
                    type(obj), p))
    for k, v in vals.items():
        if type(k) is not str:
            continue
        if hasattr(obj, k):
            setattr(obj, k, v)
        else:
            _logger.warning("Atributo no encontrado en %s: %s" % (type(obj), k))


def create_xml(to_xml, root=None):
    if type(to_xml) is list:
        for r in to_xml:
            if type(r) in [list, dict, collections.OrderedDict]:
                create_xml(r, root)
            else:
                root.text = str(r)
    else:
        for k, v in to_xml.items():
            if root is not None:
                el = etree.SubElement(root, k)
            else:
                root = etree.Element(k)
                el = root
            if type(v) in [list, dict, collections.OrderedDict]:
                create_xml(v, el)
            elif type(v) is not bool:
                el.text = str(v)
            else:
                _logger.warning("%s %s" %(k, v))
    return root

def limpiar_items(xml):
    xml =re.sub(r"<items[a-zA-Z]*>", r"", xml)
    xml =re.sub(r"</items[a-zA-Z]*>", r"", xml)
    xml =re.sub(r"<item>", r"", xml)
    xml =re.sub(r"</item>", r"", xml)
    xml =re.sub(r"<item/>", r"", xml)
    return xml

def xml_to_string(dict_xml):
    xml = etree.tostring(
        dict_xml,
        encoding="ISO-8859-1",
        xml_declaration=False,
        pretty_print=True,
        method='xml').decode('ISO-8859-1')
    return limpiar_items(xml)

def get_fecha(val):
    _fecha = False
    f_type = type(val)
    if f_type is bytes:
        val = val.decode('ISO-8859-1')
    elif f_type is datetime:
        _logger.warning("campo fecha en formato objeto")
        return val
    elif f_type is not str:
        raise UserError("campo fecha no es str o un formato objeto fecha compatible: %s" %f_type)

    def _get_fecha(fecha, formato="%d-%m-%Y"):
            date = fecha.replace('/', '-')
            try:
                return datetime.strptime(date, formato).strftime("%Y-%m-%d")
            except:
                pass
    formatos = ["%d-%m-%Y", "%d-%m-%y", "%Y-%m-%d"]
    for f in formatos:
        _fecha = _get_fecha(val, f)
        if _fecha:
            break
    return _fecha


def verificar_rut(rut=False):
    if not rut:
        return rut
    _rut_ = (re.sub('[^1234567890Kk]', '', str(rut))).zfill(9).upper()
    return True


def formatear_rut(value):
    value = value.replace('.', '').replace('k', 'K')
    if not value or value == '' or value == 0:
        value ="66666666-6"
    rut = value
    return rut


def time_stamp(formato='%Y-%m-%dT%H:%M:%S'):
    tz = pytz.timezone('America/Santiago')
    return datetime.now(tz).strftime(formato)


def validar_xml(some_xml_string, validacion='doc'):
    if validacion == 'bol':
        return some_xml_string
    validacion_type = {
        'aec': 'AEC_v10.xsd',
        'cesion': 'Cesion_v10.xsd',
        'consu': 'ConsumoFolio_v10.xsd',
        'doc': 'DTE_v10.xsd',
        'dte_cedido': 'DTECedido_v10.xsd',
        'env': 'EnvioDTE_v10.xsd',
        'env_boleta': 'EnvioBOLETA_v11.xsd',
        'env_recep': 'EnvioRecibos_v10.xsd',
        'env_resp': 'RespuestaEnvioDTE_v10.xsd',
        'libro': 'LibroCV_v10.xsd',
        'libro_s': 'LibroCVS_v10.xsd',
        'libro_boleta': 'LibroBOLETA_v10.xsd',
        'libro_guia': 'LibroGuia_v10.xsd',
        'recep': 'Recibos_v10.xsd',
        'sig': 'xmldsignature_v10.xsd',
    }
    xsdpath = os.path.dirname(os.path.realpath(__file__)) + '/xsd/'
    xsd_file = xsdpath + validacion_type[validacion]
    try:
        xmlschema_doc = etree.parse(xsd_file)
        xmlschema = etree.XMLSchema(xmlschema_doc)
        xml_doc = etree.fromstring(some_xml_string)
        result = xmlschema.validate(xml_doc)
        if not result:
            xmlschema.assert_(xml_doc)
        return result
    except AssertionError as e:
        message = 'XML Malformed Error:  %s' % e.args
        _logger.warning(message, exc_info=True)
        _logger.warning(etree.tostring(xml_doc, encoding="iso-8859-1"))
        raise UserError(message)


def recursive_xml(el):
    if el.text and bool(el.text.strip()):
        return el.text
    res = {}
    for e in el:
        res.setdefault(e.tag, recursive_xml(e))
    return res


def estado_envio(estado, estadistica=False):
    if estado in ["REC"]:
        if estadistica:
            return 'Aceptado'
        return 'Enviado'
    if estado in ['PRD']:
        return "EnProceso"
    if estado in ["EPR", "LOK", "EOK", "RPR"]:
        return "Aceptado"
    elif estado in ["RCT", "RCH", "LRH", "RFR", "LRH",
                         "RSC", "LNC", "FNA", "LRF", "LRS", "106", "LRC",
                         "RDC", "RCR", "RCO", "LRP", "RCS"]:
        return "Rechazado"
    return "Enviado"


def procesar_respuesta_envio(respuesta):
    status = {
        'status': 'NoDefinido',
        'xml_resp': respuesta,
        'glosa': ''
    }
    resp = etree.XML(respuesta.replace(
            '<?xml version="1.0" encoding="UTF-8"?>', '')\
        .replace('SII:', '')\
        .replace(' xmlns="http://www.sii.cl/XMLSchema"', ''))
    if resp.find('RESP_HDR/GLOSA_ESTADO') is not None:
        status['glosa'] = resp.find('RESP_HDR/GLOSA_ESTADO').text
    elif resp.find('RESP_HDR/GLOSA') is not None:
        status['glosa'] = resp.find('RESP_HDR/GLOSA').text
    elif resp.find('RESP_BODY/DESC_ESTADO') is not None:
        status['glosa'] = resp.find('RESP_BODY/DESC_ESTADO').text
    estado = resp.find('RESP_BODY/ESTADO_ENVIO')
    if estado is None:
        estado = resp.find('RESP_HDR/ESTADO')
        if estado is None:
            return status
    if estado.text == "-11":
        if resp.find('RESP_HDR/ERR_CODE').text == "2":
            status = {
                'warning': {
                    'title': 'Estado -11 2',
                    'message': "Estado -11: Espere a que sea aceptado por el SII, intente en 5s más",
                    },
                }
        else:
            status = {
                'warning': {
                    'title': 'Estado -11',
                    'message': "Estado -11: error ¡Algo ha salido mal, revisar carátula!",
                    },
                }
    estadistica = resp.find('RESP_BODY') is not None
    status['status'] = estado_envio(estado.text, estadistica)
    if resp.find('RESP_BODY/RECHAZADOS') is not None:
        if resp.find('RESP_BODY/RECHAZADOS').text == "1":
            status['status'] = "Rechazado"
    return status

def estado_documento(codigo, cesion=False):
    if cesion and codigo == "0":
        return "Cedido"
    if codigo in ['2', '4']:
        if cesion:
            return "Aceptado"
        return "Enviado"
    if codigo in ["EPR", "MMC", "DOK", "TMC", "AND", "MMD", "ANC"]:
        return "Proceso"
    elif codigo in ["DNK"]:
        return "Reparo"
    elif codigo in ["RCT", "RCH", "FAU", "FNA", "RDC", "-13"]:
        return "Rechazado"
    elif codigo in ["FAN", "ANC"]:
        return "Anulado"
    return "Enviado"


def procesar_respuesta_dte(respuesta, cesion=False):
    status = {
        'status': 'NoDefinido',
        'xml_resp': respuesta,
        'glosa': ''
    }
    resp = etree.XML(
        respuesta.replace('<?xml version="1.0" encoding="UTF-8"?>', '')\
        .replace('SII:', '')\
        .replace(' xmlns="http://www.sii.cl/XMLSchema"', '')
        )
    estado = resp.find('RESP_HDR/ESTADO').text
    if resp.find('RESP_HDR/GLOSA_ESTADO') is not None:
        status['glosa'] = resp.find('RESP_HDR/GLOSA_ESTADO').text
    if resp.find('RESP_HDR/GLOSA') is not None:
        status['glosa'] = resp.find('RESP_HDR/GLOSA').text
    elif resp.find('RESP_BODY') is not None:
        if estado == '0':
            status.update({
                'rut_tenedor': resp.find('RESP_BODY/RUT_TENEDOR').text,
                'fecha_ult_anot': resp.find('RESP_BODY/FECHA_ULT_ANOT').text,
                'glosa': 'Documento Cedido',
            })
            if resp.find('RESP_BODY/DEC_JURADA') is not None:
                status['dec_jurada'] = resp.find('RESP_BODY/DEC_JURADA').text
        else:
            status['glosa'] = "Contenido no esperado"

    status['status'] = estado_documento(estado, cesion)
    return status
