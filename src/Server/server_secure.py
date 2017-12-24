from src.Client.cipher_utils import *
from src.Server import certificates, server, log
from cryptography.exceptions import *
from OpenSSL import crypto
import os
import base64
import json
import logging


class ServerSecure:
    @staticmethod
    def get_cipher_suite(cipher_spec):
        specs = cipher_spec.split('-')
        aes = specs[1].split('_')
        rsa = specs[2].split('_')
        hash = specs[3]

        cipher_suite = {
            'aes': {
                'key_size': int(aes[0][3:]) // 8,
                'mode': aes[1]
            },
            'rsa': {
                'key_size': int(rsa[0][3:]),
                'padding': rsa[1]
            },
            'sha': {
                'size': int(hash[3:])
            }
        }
        return cipher_suite

    @staticmethod
    def serialize_key(pub_value):
        return base64.b64encode(pub_value.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

    @staticmethod
    def deserialize_key(pub_value):
        return serialization.load_pem_public_key(base64.b64decode(
            pub_value.encode()), default_backend())

    @staticmethod
    def serialize_certificate(cert):
        return base64.b64encode(crypto.dump_certificate(crypto.FILETYPE_PEM, cert)).decode()

    @staticmethod
    def deserialize_certificate(cert):
        return crypto.load_certificate(crypto.FILETYPE_PEM, base64.b64decode(cert.encode()))

    def __init__(self, cipher_spec=None):
        self.cipher_spec = cipher_spec
        self.cipher_suite = {}
        self.number_of_hash_derivations = None
        self.salt = None
        self.server_cert = server.Server.certificates.cert
        self.priv_value = None
        self.pub_value = None
        self.peer_pub_value = None
        self.peer_salt = None
        self.private_key = server.Server.certificates.priv_key
        self.public_key = server.Server.certificates.pub_key

        self.user_certificates = {}

    def uncapsulate_insecure_message(self, payload):
        log.log(logging.DEBUG, "INSECURE MESSAGE RECEIVED: %r" % payload)

        if payload['cipher_spec'] is not None:
            self.cipher_spec = payload['cipher_spec']
            self.cipher_suite = ServerSecure.get_cipher_suite(self.cipher_spec)
        self.peer_pub_value = ServerSecure.deserialize_key(
            payload['secdata']['dhpubvalue'])
        self.peer_salt = base64.b64decode(payload['secdata']['salt'].encode())
        self.number_of_hash_derivations = payload['secdata']['index']

        return {'type': 'init'}, payload['nounce']

    def encapsulate_secure_message(self, payload, nounce):
        # Values used in key exchange
        self.salt = os.urandom(16)
        self.priv_value, self.pub_value = generate_ecdh_keypair()

        # Derive AES key and cipher payload
        aes_key = derive_key_from_ecdh(
            self.priv_value,
            self.peer_pub_value,
            self.salt,
            self.peer_salt,
            self.cipher_suite['aes']['key_size'],
            self.cipher_suite['sha']['size'],
            self.number_of_hash_derivations,
        )

        aes_cipher, aes_iv = generate_aes_cipher(
            aes_key, self.cipher_suite['aes']['mode'])

        encryptor = aes_cipher.encryptor()
        ciphered_payload = encryptor.update(json.dumps(payload).encode())\
                           + encryptor.finalize()

        # Sign payload with CC authentication public key
        message_payload = json.dumps({
            'message': base64.b64encode(ciphered_payload).decode(),
            'nounce': nounce,
            'secdata': {
                'dhpubvalue': ServerSecure.serialize_key(self.pub_value),
                'salt': base64.b64encode(self.salt).decode(),
                'iv': base64.b64encode(aes_iv).decode(),
                'index': self.number_of_hash_derivations
            }
        }).encode()

        signature = None
        """
        signature = rsa_sign(
            self.private_key,
            message_payload,
            self.cipher_suite['sha']['size']
        )
        """
        # Build message
        message = {
            'type': 'secure',
            'payload': base64.b64encode(message_payload).decode(),
            'signature': signature,
            'certificate': ServerSecure.serialize_certificate(self.server_cert),
            'cipher_spec': self.cipher_spec
        }

        log.log(logging.DEBUG, "SECURE MESSAGE SENT: %r" % message)

        return message

    def uncapsulate_secure_message(self, message):
        assert message['cipher_spec'] == self.cipher_spec
        """
        # Verify signature and certificate validity
        peer_certificate = ServerSecure.deserialize(message['certificate'])
        assert server.serv.certificates.validate_cert(peer_certificate])
        try:
            peer_certificate.get_pubkey().to_cryptography_key().verify(
                message['signature'],
                base64.b64decode(message['payload'].encode()),
                self.cipher_suite['rsa']['padding'],
                self.cipher_suite['sha']['size']
            )
        except InvalidSignature:
            return "Invalid signature"
        """

        message['payload'] = json.loads(
            base64.b64decode(message['payload'].encode()))

        log.log(logging.DEBUG, "SECURE MESSAGE RECEIVED: %r" % message)

        # Derive AES key and decipher payload
        self.number_of_hash_derivations = message['payload']['secdata']['index']

        self.peer_pub_value = ServerSecure.deserialize_key(
            message['payload']['secdata']['dhpubvalue'])
        self.peer_salt = base64.b64decode(
            message['payload']['secdata']['salt'].encode())

        aes_key = derive_key_from_ecdh(
            self.priv_value,
            self.peer_pub_value,
            self.peer_salt,
            self.salt,
            self.cipher_suite['aes']['key_size'],
            self.cipher_suite['sha']['size'],
            self.number_of_hash_derivations,
        )

        aes_cipher, aes_iv = generate_aes_cipher(
            aes_key,
            self.cipher_suite['aes']['mode'],
            base64.b64decode(message['payload']['secdata']['iv'].encode())
        )

        decryptor = aes_cipher.decryptor()
        deciphered_payload = decryptor.update(base64.b64decode(
            message['payload']['message'].encode())) + decryptor.finalize()
        deciphered_payload = json.loads(deciphered_payload.decode())

        return deciphered_payload, message['payload']['nounce']