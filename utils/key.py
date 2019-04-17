import binascii
import os
from typing import Callable

from little_boxes.key import Key

KEY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config")


def _new_key() -> str:
    return binascii.hexlify(os.urandom(32)).decode("utf-8")


def get_secret_key(name: str, new_key: Callable[[], str] = _new_key) -> str:
    """Loads or generates a cryptographic key."""
    key_path = os.path.join(KEY_DIR, f"{name}.key")
    if not os.path.exists(key_path):
        k = new_key()
        with open(key_path, "w+") as f:
            f.write(k)
        return k

    with open(key_path) as f:
        return f.read()


def get_key(owner: str, _id: str, user: str, domain: str) -> Key:
    """"Loads or generates an RSA key."""
    k = Key(owner, _id)
    user = user.replace(".", "_")
    domain = domain.replace(".", "_")
    key_path = os.path.join(KEY_DIR, f"key_{user}_{domain}.pem")
    if os.path.isfile(key_path):
        with open(key_path) as f:
            privkey_pem = f.read()
            k.load(privkey_pem)
    else:
        k.new()
        with open(key_path, "w") as f:
            f.write(k.privkey_pem)

    return k
