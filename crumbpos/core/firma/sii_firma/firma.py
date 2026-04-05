# -#- coding: utf-8 -#-
from .signature_cert import SignatureCert
from . import clase_util as util
from .clase_util import UserError
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.hashes import SHA1, SHA256
from OpenSSL import crypto
import textwrap
from lxml import etree
import struct
import base64
import hashlib
import sys
import logging


_logger = logging.getLogger(__name__)
type_ = crypto.FILETYPE_PEM
USING_PYTHON2 = True if sys.version_info < (3, 0) else False


class Firma(object):
    def __init__(self, vals={}):
        self.firma_electronica = vals

    @property
    def errores(self):
        if not hasattr(self, '_errores'):
            if hasattr(self, '_firma_electronica'):
                return self._firma_electronica.errores
            return []
        return self._errores

    @errores.setter
    def errores(self, val):
        _errores = self.errores
        _errores.append(val)
        self._errores = _errores

    @property
    def firma(self):
        return self.firma_electronica

    @firma.setter
    def firma(self, val):
        self._firma_electronica = val

    @property
    def firma_electronica(self):
        if not hasattr(self, '_firma_electronica'):
            return False
        if self._firma_electronica.errores:
            return False
        return self._firma_electronica

    @firma_electronica.setter
    def firma_electronica(self, val):
        if not val:
            return
        self._firma_electronica = SignatureCert(val)
        if self._firma_electronica.errores:
            return
        self.privkey = self._firma_electronica.priv_key
        if self._firma_electronica.cert:
            self.cert = self._firma_electronica.cert
        if self._firma_electronica.subject_serial_number:
            self.rut_firmante = self._firma_electronica.subject_serial_number

    @property
    def privkey(self):
        if not hasattr(self, '_priv_key'):
            return False
        return self._priv_key

    @privkey.setter
    def privkey(self, val):
        self._priv_key = val

    @property
    def cert(self):
        if not hasattr(self, '_cert'):
            return False
        return self._cert

    @cert.setter
    def cert(self, val):
        if type(val) is bytes:
            val = val.decode('ISO-8859-1')
        bc = '''-----BEGIN CERTIFICATE-----\n'''
        ec = '''\n-----END CERTIFICATE-----\n'''
        self._cert = val.replace(
            bc, '').replace(ec, '').replace('\n', '')

    @property
    def rut_firmante(self):
        if not hasattr(self, '_rut_firmante'):
            raise UserError("La firma no tiene rut de firmante, debe acompañar el rut del firmante en {'rut_firmante':12312, 'string_firma':.....}")
            return False
        return self._rut_firmante

    @rut_firmante.setter
    def rut_firmante(self, val):
        self._rut_firmante = util.formatear_rut(val)

    @property
    def key(self):
        return load_pem_private_key(
                self.privkey,
                password=None,
                backend=default_backend()
            )

    @property
    def key_pub(self):
        bc = "-----BEGIN CERTIFICATE-----\n"
        ec = "\n-----END CERTIFICATE-----\n"
        cert_str = bc + self.cert + ec
        return crypto.load_certificate(crypto.FILETYPE_PEM, cert_str)

    @property
    def verify(self):
        if not hasattr(self, '_verify'):
            return True
        return self._verify

    @verify.setter
    def verify(self, val):
        self._verify = val

    def _iniciar(self):
        self.firma = None
        self.rut_firmante = None

    def ensure_str(self, x, encoding="utf-8", none_ok=False):
        if none_ok is True and x is None:
            return x
        if not isinstance(x, str):
            x = x.decode(encoding)
        return x

    def long_to_bytes(self, n, blocksize=0):
        s = b''
        pack = struct.pack
        while n > 0:
            s = pack(b'>I', n & 0xffffffff) + s
            n = n >> 32
        for i in range(len(s)):
            if s[i] != b'\000'[0]:
                break
        else:
            s = b'\000'
            i = 0
        s = s[i:]
        if blocksize > 0 and len(s) % blocksize:
            s = (blocksize - len(s) % blocksize) * b'\000' + s
        return s

    def append_sig(self, tag, string, firma, type):
        tag = "</%s>" % tag.split('}')[-1]
        result = string.replace(tag, "%s\n%s" %(firma, tag))
        if type != 'token' and self.verify:
            if type == 'libro_boleta':
                xmlns = 'xmlns="http://www.w3.org/2000/09/xmldsig#"'
                xmlns_sii = 'xmlns="http://www.sii.cl/SiiDte"'
                result = result.replace(xmlns, xmlns_sii)
            result = result if util.validar_xml(result, type) else ''
        return result

    def firmar(self, string, uri=False, type="doc"):
        el = etree.fromstring(string)
        if type == 'token':
            string_to_canon = etree.tostring(el)
        else:
            string_to_canon = etree.tostring(el[0], encoding='ISO-8859-1', xml_declaration=False).decode('ISO-8859-1')
        mess = etree.tostring(etree.fromstring(string_to_canon), method="c14n")
        digest = base64.b64encode(self.digest(mess))
        reference_uri = '#'+uri if uri else ''
        signed_info = etree.Element("SignedInfo")
        etree.SubElement(
                    signed_info,
                    "CanonicalizationMethod",
                    Algorithm='http://www.w3.org/TR/2001/REC-xml-c14n-20010315'
                )
        etree.SubElement(
                    signed_info,
                    "SignatureMethod",
                    Algorithm='http://www.w3.org/2000/09/xmldsig#rsa-sha1'
                )
        reference = etree.SubElement(
                    signed_info,
                    "Reference",
                    URI=reference_uri
                )
        transforms = etree.SubElement(
                    reference,
                    "Transforms"
                )
        etree.SubElement(
                    transforms,
                    "Transform",
                    Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
                )
        etree.SubElement(
                    reference,
                    "DigestMethod",
                    Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"
                )
        digest_value = etree.SubElement(
                    reference,
                    "DigestValue"
                )
        digest_value.text = digest
        signed_info_c14n = etree.tostring(
                    signed_info,
                    method="c14n",
                    exclusive=False,
                    with_comments=False,
                    inclusive_ns_prefixes=None
                )
        att = 'xmlns="http://www.w3.org/2000/09/xmldsig#"'
        if type not in ['doc', 'recep']:
            att += ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        signed_info_c14n = signed_info_c14n.decode().replace(
                "<SignedInfo>",
                "<SignedInfo %s>" % att
            )
        sig_root = etree.Element(
            "Signature",
            attrib={'xmlns': 'http://www.w3.org/2000/09/xmldsig#'}
        )
        sig_root.append(etree.fromstring(signed_info_c14n))
        signature_value = etree.SubElement(sig_root, "SignatureValue")
        algo = 'sha1'
        if type == 'token':
            algo = 'sha256'
        signature = self.generar_firma(signed_info_c14n, algo)
        signature_value.text = textwrap.fill(
                    signature,
                    64
                )
        key_info = etree.SubElement(sig_root, "KeyInfo")
        key_value = etree.SubElement(key_info, "KeyValue")
        rsa_key_value = etree.SubElement(key_value, "RSAKeyValue")
        modulus = etree.SubElement(rsa_key_value, "Modulus")
        modulus.text = textwrap.fill(
                    base64.b64encode(
                            self.long_to_bytes(
                                    self.key.public_key().public_numbers().n
                                )
                        ).decode(),
                    64
                )
        exponent = etree.SubElement(rsa_key_value, "Exponent")
        exponent.text = self.ensure_str(
                    base64.b64encode(
                            self.long_to_bytes(
                                        self.key.public_key().public_numbers().e
                                    )
                                )
                    )
        x509_data = etree.SubElement(key_info, "X509Data")
        x509_certificate = etree.SubElement(x509_data, "X509Certificate")
        x509_certificate.text = '\n' + textwrap.fill(self.cert, 64)
        firma = etree.tostring(sig_root).decode('ISO-8859-1')
        if not util.validar_xml(firma, 'sig'):
            return False
        return self.append_sig(el.tag, string, firma, type)

    def digest(self, data):
        sha1 = hashlib.new('sha1', data)
        return sha1.digest()

    def generar_firma(self, texto, algo="sha1"):
        sha_algo = SHA1
        if algo == 'sha256':
            sha_algo = SHA256
        if type(texto) is not bytes:
            texto=texto.encode()
        signature = self.key.sign(
                texto,
                padding=PKCS1v15(),
                algorithm=sha_algo()
            )
        text = base64.b64encode(signature).decode()
        return text

    def verificar_digest(self, digest, mess):
        our = base64.b64encode(self.digest(mess)).decode()
        return our == digest

    def verificar_firma_xml(self, xml_verificar):
        xml = etree.fromstring(xml_verificar.encode('ISO-8859-1'))
        string = etree.tostring(xml[0])
        mess = etree.tostring(etree.fromstring(string), method="c14n")
        ns = "{http://www.w3.org/2000/09/xmldsig#}"
        query = "{0}Signature/{0}SignedInfo/{0}Reference/{0}DigestValue".format(ns)
        digest_value = xml.find(query).text
        if not self.verificar_digest(digest_value, mess):
            return 2, 'Rechazado - Error en Digest'
        query = "{0}Signature/{0}KeyInfo/{0}X509Data/{0}X509Certificate".format(ns)
        self._cert = xml.find(query).text
        query = "{0}Signature/{0}SignedInfo".format(ns)
        signed_info_c14n = etree.tostring(
                    xml.find(query),
                    method="c14n",
                    exclusive=False,
                    with_comments=False,
                    inclusive_ns_prefixes=None
                ).replace(b' xmlns=""', b'')
        query = "{0}Signature/{0}SignatureValue".format(ns)
        if not self.verificar_firma(xml.find(query).text, signed_info_c14n, 'sha1'):
            return 2, 'Rechazado - Error en Firma'
        return 0, ''

    def verificar_firma(self, firma, texto, algo='sha1'):
        try:
            crypto.verify(self.key_pub,
                             base64.b64decode(firma),
                             texto,
                             algo)
            return True
        except:
            _logger.warning("Error en verificar firma", exc_info=True)
            return False
