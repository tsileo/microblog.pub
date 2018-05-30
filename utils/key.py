import os
import binascii

from Crypto.PublicKey import RSA
from typing import Callable

KEY_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'config'
)


def _new_key() -> str:
    return binascii.hexlify(os.urandom(32)).decode('utf-8')

def get_secret_key(name: str, new_key: Callable[[], str] = _new_key) -> str:
    key_path = os.path.join(KEY_DIR, f'{name}.key')
    if not os.path.exists(key_path):
        k = new_key()
        with open(key_path, 'w+') as f:
            f.write(k)
        return k

    with open(key_path) as f:
        return f.read()


class Key(object):
    def __init__(self, user: str, domain: str, create: bool = True) -> None:
        user = user.replace('.', '_')
        domain = domain.replace('.', '_')
        key_path = os.path.join(KEY_DIR, f'key_{user}_{domain}.pem')
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
