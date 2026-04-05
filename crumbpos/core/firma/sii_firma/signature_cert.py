# -*- coding: utf-8 -*-
from . import clase_util as util
import datetime
from cryptography.hazmat.primitives.serialization import pkcs12, Encoding, PrivateFormat, NoEncryption
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa, dsa, ec
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
import base64
import logging

_logger = logging.getLogger(__name__)

class SignatureCert(object):

    def __init__(self, signature={}):
        if not signature:
            return
        util.set_from_keys(self, signature, priorizar=['string_firma', 'string_password'])

    @property
    def errores(self):
        if not hasattr(self, '_errores'):
            return []
        return self._errores

    @errores.setter
    def errores(self, val):
        _errores = self.errores
        _errores.append(val)
        self._errores = _errores

    @property
    def cert(self):
        if not hasattr(self, '_cert'):
            return False
        return self._cert

    @cert.setter
    def cert(self, val):
        self._cert = val

    @property
    def init_signature(self):
        if not hasattr(self, '_init_signature'):
            return True
        return self._init_signature

    @init_signature.setter
    def init_signature(self, val):
        self._init_signature = val
        if not val:
            return
        try:
            p12 = pkcs12.load_key_and_certificates(
                self.string_firma,
                self.string_password,
                None
            )
            private_key, cert, additional_certificates = p12
        except Exception as e:
            err = str(e)
            if 'mac verify failure' in err:
                self.errores = "Error en clave del Certificado, verificar que esté correcta: %s" % err
            else:
                self.errores = "Error en apertura de archivo: %s" % err
            _logger.warning(str(e), exc_info=True)
            return
        try:
            issuer = cert.issuer
            subject = cert.subject
            self.not_before = cert.not_valid_before
            self.not_after = cert.not_valid_after

            def get_attribute(entity, oid):
                try:
                    return entity.get_attributes_for_oid(oid)[0].value
                except IndexError:
                    return None

            self.subject_c = get_attribute(subject, NameOID.COUNTRY_NAME)
            self.subject_title = get_attribute(subject, NameOID.TITLE)
            self.subject_common_name = get_attribute(subject, NameOID.COMMON_NAME)
            subject_serial_number = get_attribute(subject, NameOID.SERIAL_NUMBER)
            if subject_serial_number:
                self.subject_serial_number = get_attribute(subject, NameOID.SERIAL_NUMBER)
            self.subject_email_address = get_attribute(subject, NameOID.EMAIL_ADDRESS)

            self.issuer_country = get_attribute(issuer, NameOID.COUNTRY_NAME)
            self.issuer_organization = get_attribute(issuer, NameOID.ORGANIZATION_NAME)
            self.issuer_common_name = get_attribute(issuer, NameOID.COMMON_NAME)
            self.issuer_serial_number = get_attribute(issuer, NameOID.SERIAL_NUMBER)
            self.issuer_email_address = get_attribute(issuer, NameOID.EMAIL_ADDRESS)

            now = datetime.datetime.now()
            self.status = 'expired' if now > self.not_after else 'valid'

            self.cert_serial_number = cert.serial_number
            self.cert_signature_algor = cert.signature_algorithm_oid._name
            self.cert_version = cert.version.value
            cert_subject_bytes = subject.public_bytes(Encoding.DER)
            digest = hashes.Hash(hashes.SHA1())
            digest.update(cert_subject_bytes)
            self.cert_hash = digest.finalize().hex()
            self.private_key_bits = private_key.key_size

            if isinstance(private_key, rsa.RSAPrivateKey):
                self.private_key_type = 'RSA'
            elif isinstance(private_key, dsa.DSAPrivateKey):
                self.private_key_type = 'DSA'
            elif isinstance(private_key, ec.EllipticCurvePrivateKey):
                self.private_key_type = 'EC'
            else:
                self.private_key_type = 'Unknown'

            self.priv_key = private_key.private_bytes(
                encoding=Encoding.PEM,
                format=PrivateFormat.PKCS8,
                encryption_algorithm=NoEncryption()
            )
            self.cert = cert.public_bytes(Encoding.PEM)
        except Exception as e:
            msg = "Error en obtención de datos de la firma: %s" % str(e)
            _logger.warning(msg, exc_info=True)
            self.errores = msg

    @property
    def priv_key(self):
        if not hasattr(self, '_priv_key'):
            return False
        return self._priv_key

    @priv_key.setter
    def priv_key(self, val):
        if isinstance(val, str):
            val = val.encode()
        self._priv_key = val

    @property
    def rut_firmante(self):
        return self.subject_serial_number

    @rut_firmante.setter
    def rut_firmante(self, val):
        self.subject_serial_number = val

    @property
    def string_password(self):
        if not hasattr(self, '_string_password'):
            return False
        return self._string_password

    @string_password.setter
    def string_password(self, val):
        if isinstance(val, str):
            val = val.encode('ISO-8859-1')
        self._string_password = val

    @property
    def string_firma(self):
        if not hasattr(self, '_string_firma'):
            return False
        return self._string_firma

    @string_firma.setter
    def string_firma(self, val):
        self._string_firma = base64.b64decode(val)

    @property
    def subject_serial_number(self):
        if not hasattr(self, '_subject_serial_number'):
            return False
        return self._subject_serial_number

    @subject_serial_number.setter
    def subject_serial_number(self, val):
        self._subject_serial_number = val
