from log import logger
from cipher_utils import *
import lib
from OpenSSL import crypto
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.x509 import oid, extensions
from datetime import datetime, timedelta
from subprocess import check_output, DEVNULL
import wget
import os
import shutil
import logging
import json
import base64


# Only accepts OpenSSL X509 Objects
class X509Certificates:
    @classmethod
    def download_crl(cls, cert, download_type):
        url = download_type(cert)
        if url is None:
            return None, None

        crl_download = wget.download(url, out=lib.CRLS_DIR[:-1])

        with open(crl_download, 'rb') as f:
            crl = crypto.load_crl(crypto.FILETYPE_ASN1, f.read())

        return crl, crl_download

    @classmethod
    def get_cert_id(cls, cert, subject_notissuer=True):
        cert_id = cert.get_subject().serialNumber \
            if subject_notissuer else cert.get_issuer().serialNumber

        if cert_id is None:
            cert_id = cert.get_subject().commonName \
                if subject_notissuer else cert.get_issuer().commonName

        return cert_id

    @classmethod
    def get_extension(cls, cert, short_name):
        for i in range(0, cert.get_extension_count()):
            extension = cert.get_extension(i)
            if extension.get_short_name() == short_name:
                return extension

    @classmethod
    def get_crl_url(cls, cert):
        extension = cls.get_extension(cert, b'crlDistributionPoints')
        try:
            value = extension.get_data()
            url = 'http' + value.split(b'http')[1].decode()
            return url
        except:
            return None

    @classmethod
    def get_delta_url(cls, cert):
        extension = cls.get_extension(cert, b'freshestCRL')
        try:
            value = extension.get_data()
            url = 'http' + value.split(b'http')[1].decode()
            return url
        except:
            return None

    @classmethod
    def get_ocsp_url(cls, cert):
        extension = cls.get_extension(cert, b'authorityInfoAccess')
        try:
            value = extension.get_data()
            url = 'http' + value.split(b'http')[1].decode()
            return url
        except:
            return None

    @classmethod
    def get_ocsp_response(cls, cert_path, issuer_path, ocsp_url):
        try:
            response = check_output(
                ['openssl', 'ocsp', '-issuer', issuer_path, '-cert', cert_path,
                 '-url', ocsp_url, '-CAfile', lib.CERTS_DIR + 'ca'],
                stderr=DEVNULL
            )

            for line in response.decode().split('\n'):
                if cert_path in line:
                    r = line.split(':')[1].strip()
                    return True if r == 'good' else False

            return False
        except Exception:
            return False

    @classmethod
    def create_folders(cls):
        if not os.path.exists(lib.CERTS_DIR):
            os.makedirs(lib.CERTS_DIR)
            # TODO: script to download them

        if not os.path.exists(lib.USER_CERTS_DIR):
            os.makedirs(lib.USER_CERTS_DIR)

        if os.path.exists(lib.CRLS_DIR):
            shutil.rmtree(lib.CRLS_DIR)

        os.makedirs(lib.CRLS_DIR)

    def __init__(self, users):
        self.priv_key = None
        self.pub_key = None
        self.cert = None
        self.ca_cert = None
        self.crls = {}
        self.certs = {}
        self.valid_certs = {}

        X509Certificates.create_folders()

        self.import_certs(lib.XCA_DIR)
        self.import_certs(lib.CERTS_DIR)
        self.import_keys()
        self.import_user_certs(users)

    def import_user_certs(self, users):
        for uid in users:
            user = users[uid]
            cc_cert = deserialize_certificate(json.loads(base64.b64decode(
                user['description']['secdata'].encode()).decode())['cccertificate'])
            cert_id = X509Certificates.get_cert_id(cc_cert)

            # Validate saved certs in user description
            path = lib.USER_CERTS_DIR + cert_id
            if self.validate_cert(cc_cert):
                self.certs[cert_id] = {'cert': cc_cert, 'path': path}

    def get_user_cert(self, uuid, cert):
        if uuid not in self.certs or \
                (self.certs[uuid]['cert'].get_serial_number()
                     != cert.get_serial_number()
                 and not self.check_expiration_or_revoked(self.certs[uuid])):

            # Save to file if it does not exist yet
            # Or if the provided cert is an update over the cached one, replace
            path = lib.USER_CERTS_DIR + uuid
            if not os.path.isfile(path):
                with open(path, 'wb') as f:
                    f.write(
                        crypto.dump_certificate(crypto.FILETYPE_PEM, cert))

            self.certs[uuid] = {'cert': cert, 'path': path}

        return self.certs[uuid]

    def import_certs(self, directory):
        files = [f for f in os.listdir(directory)]

        for f_name in files:
            cert = None
            path = directory + f_name

            # Make sure is not a directory
            if os.path.isdir(path):
                continue

            # Trying to read it as PEM
            try:
                f = open(path, 'rb')
                cert = crypto.load_certificate(crypto.FILETYPE_PEM, f.read())
            except crypto.Error:
                logger.log(logging.DEBUG, "Not a PEM Certificate: %r" % f_name)
            finally:
                f.close()

            if cert is None:
                # Trying to read it as DER
                try:
                    f = open(path, 'rb')
                    cert = crypto.load_certificate(
                        crypto.FILETYPE_ASN1, f.read())
                    logger.log(logging.DEBUG,
                               "Loaded DER Certificate: %r" % f_name)
                    f.close()
                except crypto.Error:
                    logger.log(logging.DEBUG,
                               "Unable to load certificate: %r" % f_name)
                    f.close()
                    continue

            cert_id = X509Certificates.get_cert_id(cert)
            if cert_id == 'SecurityServer':
                self.cert = cert
            elif cert_id == 'ServerCA':
                self.ca_cert = cert
            elif cert_id not in self.certs.keys():
                self.certs[cert_id] = {'cert': cert, 'path': path}

    def import_keys(self):
        f = open(lib.XCA_DIR + 'SecurityServer.pem', 'rb')

        self.priv_key = serialization.load_pem_private_key(
            f.read(), None, default_backend())
        self.pub_key = self.cert.get_pubkey().to_cryptography_key()

    def check_expiration_or_revoked(self, cert_entry):
        cert = cert_entry['cert']
        issuer = X509Certificates.get_cert_id(cert, False)

        # Check time validity
        if cert.has_expired():
            return False

        # Try first OCSP
        ocsp_url = X509Certificates.get_ocsp_url(cert)
        if ocsp_url is not None:
            return X509Certificates.get_ocsp_response(
                cert_entry['path'], self.certs[issuer]['path'], ocsp_url)

        # Download CRLs and deltas
        if issuer not in self.crls or datetime.today() > \
                self.crls[issuer]['crl'].to_cryptography().next_update:
            # Download CRL
            crl, crl_path = X509Certificates.download_crl(
                cert, X509Certificates.get_crl_url)

            # If CRL is inexistant, certificate is valid
            if crl is None:
                return True

            self.crls[issuer] = {'path': crl_path, 'crl': crl, 'delta': None}

        if self.crls[issuer]['delta'] is None or datetime.today() > \
                self.crls[issuer]['delta']['crl'].to_cryptography().next_update:
            # Download delta CRL
            delta, delta_path = X509Certificates.download_crl(
                cert, X509Certificates.get_delta_url)

            if delta is not None:
                self.crls[issuer]['delta'] = {'path': delta_path, 'crl': delta}

        # Check if the certificate has been revoked
        revoked_serials = []
        for issuer in self.crls.keys():
            crl = self.crls[issuer]
            rev = crl['crl'].get_revoked() \
                if crl['crl'].get_revoked() is not None else []
            revoked_serials += [int(c.get_serial(), 16) for c in rev] \
                if rev is not None else []

            rev = crl['delta']['crl'].get_revoked() \
                if crl['delta'] is not None \
                   and crl['delta']['crl'].get_revoked() is not None else []
            revoked_serials += [int(c.get_serial(), 16) for c in rev]

        return cert.get_serial_number() not in revoked_serials

    def validate_cert(self, cert):
        cert_id = X509Certificates.get_cert_id(cert)
        logger.log(logging.DEBUG, "Verifying certificate validity: %r"
                   % cert_id)

        c = self.get_user_cert(cert_id, cert)

        # Verify first the cache
        if cert_id in self.valid_certs \
                and self.valid_certs[cert_id]['serial'] == \
                    cert.get_serial_number() \
                and self.valid_certs[cert_id]['date'] \
                    + timedelta(days=1) > datetime.today():
            logger.log(logging.DEBUG, "[Cache] Valid certificate: %r" % cert_id)
            return True

        # Check if it has extension KeyUsage with digital signature
        try:
            ext = cert.to_cryptography().extensions.get_extension_for_oid(
                oid.ExtensionOID.KEY_USAGE)

            if not ext.value.digital_signature:
                logger.log(logging.DEBUG, "Invalid certificate: %r" % cert_id)
                return False
        except extensions.ExtensionNotFound:
            return False

        # Check if all certificates in the chain are valid
        while True:
            subject = X509Certificates.get_cert_id(c['cert'])
            issuer = X509Certificates.get_cert_id(c['cert'], False)
            if issuer not in self.certs.keys():
                logger.log(logging.DEBUG, "Invalid certificate: %r" % cert_id)
                return False

            # Self-signed -> stop chain
            if issuer == subject:
                break

            # Check validity
            if not self.check_expiration_or_revoked(c):
                logger.log(logging.DEBUG, "Invalid certificate: %r" % cert_id)
                return False

            c = self.certs[issuer]

        # Check if the chain is valid
        try:
            # Create certificate store
            store = crypto.X509Store()
            store.set_flags(crypto.X509StoreFlags.CRL_CHECK_ALL)
            for subject in self.certs.keys():
                store.add_cert(self.certs[subject]['cert'])

            for subject in self.crls.keys():
                store.add_crl(self.crls[subject]['crl'])
                if self.crls[subject]['delta'] is not None:
                    store.add_crl(self.crls[subject]['delta']['crl'])

            # Create a certificate context using the store and
            # the certificate to be verified
            store_ctx = crypto.X509StoreContext(store, cert)

            # Verify the certificate, returns None
            # if it can validate the certificate
            store_ctx.verify_certificate()

            # If it gets here, it means it's valid
            logger.log(logging.DEBUG, "Valid certificate: %r" % cert_id)

            # Update cache
            self.valid_certs[cert_id] = {
                'serial': cert.get_serial_number(),
                'date': datetime.today()
            }

            return True

        except Exception as e:
            logger.log(logging.DEBUG, "Invalid certificate: %r" % cert_id)

            # Remove from cache
            if cert_id in self.valid_certs:
                del self.valid_certs[cert_id]

            return False
