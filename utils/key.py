import os
import binascii

from Crypto.PublicKey import RSA

KEY_DIR = 'config/'


def get_secret_key(name:str) -> str:
    key_path = f'{KEY_DIR}{name}.key'
    if not os.path.exists(key_path):
        k = binascii.hexlify(os.urandom(32)).decode('utf-8')
        with open(key_path, 'w+') as f:
            f.write(k)
        return k

    with open(key_path) as f:
        return f.read()


class Key(object):
    def __init__(self, user: str, domain: str, create: bool = True) -> None:
        user = user.replace('.', '_')
        domain = domain.replace('.', '_')
        key_path = f'{KEY_DIR}/key_{user}_{domain}.pem'
        if os.path.isfile(key_path):
            with open(key_path) as f:
                self.privkey_pem = f.read()
            self.privkey = RSA.importKey(self.privkey_pem)
            self.pubkey_pem = self.privkey.publickey().exportKey('PEM').decode('utf-8')
        else:
            if not create:
                raise Exception('must init private key first')
            k = RSA.generate(4096)
            self.privkey_pem = k.exportKey('PEM').decode('utf-8')
            self.pubkey_pem = k.publickey().exportKey('PEM').decode('utf-8')
            with open(key_path, 'w') as f:
                f.write(self.privkey_pem)
            self.privkey = k
