from OpenSSL import crypto
from cryptography.hazmat.backends import default_backend
from cryptography import x509
import wget
import os


# Only accepts OpenSSL X509 Objects
class X509Certificates:
    @classmethod
    def get_extension(cls, cert, short_name):
        for i in (0, cert.get_extension_count()):
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
    def get_ocsp_url(cls, cert):
        extension = cls.get_extension(cert, b'authorityInfoAccess')
        try:
            value = extension.get_data()
            url = 'http' + value.split(b'http')[1].decode()
            return url
        except:
            return None

    def __init__(self):
        self.crls = {}
        self.certs = {}
        self.store = crypto.X509Store()

        self.import_certs()

    def import_certs(self):
        files = [f for f in os.listdir('./certs')]

        for f_name in files:
            if f_name.split('.')[1] not in ['der', 'cer', 'crt']:
                continue

            mode = 'rb' if '.cer' in f_name else 'r'
            f = open('./certs/' + f_name, mode)
            if mode == 'r':
                cert = crypto.X509.from_cryptography(
                    x509.load_pem_x509_certificate(f.read().encode(),
                                                   default_backend()))
            else:
                cert = crypto.X509.from_cryptography(
                    x509.load_der_x509_certificate(f.read(), default_backend()))

            if cert.get_subject().commonName not in self.certs.keys():
                self.certs[cert.get_subject().commonName] = cert

        for subject in self.certs.keys():
            self.store.add_cert(self.certs[subject])

    # TODO: Check CRL date periodically
    def check_expiration_or_revoked(self, cert):
        # Check time validity
        if cert.has_expired():
            return False

        # Check if it has been revoked
        issuer = cert.get_issuer().commonName
        if issuer not in self.crls:
            dir_path = os.path.dirname(os.path.realpath(__file__)) + '/crl/'
            crl_download = wget.download(X509Certificates.get_crl_url(cert),
                                         out=dir_path)
            f = open(dir_path + crl_download, 'rb')
            crl = crypto.CRL.from_cryptography(
                x509.load_der_x509_crl(f.read(), default_backend())
            )

            self.crls[issuer] = dir_path + crl_download
        else:
            f = open(self.crls[issuer], 'rb')
            crl = crypto.CRL.from_cryptography(
                x509.load_der_x509_crl(f.read(), default_backend())
            )

        revoked_serials = [int(c.get_serial(), 16) for c in crl.get_revoked()]
        return cert.get_serial_number() not in revoked_serials


    # TODO: Check all the chain
    def validate_cert(self, cert):
        c = cert
        # Check if all certificates in the chain are valid
        while True:
            assert c.get_issuer().commonName in self.certs

            if c.get_issuer().commonName == c.get_subject().commonName:
                break

            if not self.check_expiration_or_revoked(c):
                return False
            c = self.certs[c.get_issuer().commonName]

        # Check if the chain is valid
        try:
            # Create a certificate context using the store and
            # the certificate to be verified
            store_ctx = crypto.X509StoreContext(self.store, cert)

            # Verify the certificate, returns None
            # if it can validate the certificate
            store_ctx.verify_certificate()

            # If it gets here, it means it's valid
            return True

        except Exception as e:
            return False
