import base64
from typing import Any

from Crypto.PublicKey import RSA
from Crypto.Util import number

from app.config import KEY_PATH


def key_exists() -> bool:
    return KEY_PATH.exists()


def generate_key() -> None:
    if key_exists():
        raise ValueError(f"Key at {KEY_PATH} already exists")
    k = RSA.generate(2048)
    privkey_pem = k.exportKey("PEM").decode("utf-8")
    KEY_PATH.write_text(privkey_pem)


def get_pubkey_as_pem() -> str:
    text = KEY_PATH.read_text()
    return RSA.import_key(text).public_key().export_key("PEM").decode("utf-8")


def get_key() -> str:
    return KEY_PATH.read_text()


class Key(object):
    DEFAULT_KEY_SIZE = 2048

    def __init__(self, owner: str, id_: str | None = None) -> None:
        self.owner = owner
        self.privkey_pem: str | None = None
        self.pubkey_pem: str | None = None
        self.privkey: RSA.RsaKey | None = None
        self.pubkey: RSA.RsaKey | None = None
        self.id_ = id_

    def load_pub(self, pubkey_pem: str) -> None:
        self.pubkey_pem = pubkey_pem
        self.pubkey = RSA.importKey(pubkey_pem)

    def load(self, privkey_pem: str) -> None:
        self.privkey_pem = privkey_pem
        self.privkey = RSA.importKey(self.privkey_pem)
        self.pubkey_pem = self.privkey.publickey().exportKey("PEM").decode("utf-8")

    def new(self) -> None:
        k = RSA.generate(self.DEFAULT_KEY_SIZE)
        self.privkey_pem = k.exportKey("PEM").decode("utf-8")
        self.pubkey_pem = k.publickey().exportKey("PEM").decode("utf-8")
        self.privkey = k

    def key_id(self) -> str:
        return self.id_ or f"{self.owner}#main-key"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.key_id(),
            "owner": self.owner,
            "publicKeyPem": self.pubkey_pem,
            "type": "Key",
        }

    @classmethod
    def from_dict(cls, data):
        try:
            k = cls(data["owner"], data["id"])
            k.load_pub(data["publicKeyPem"])
        except KeyError:
            raise ValueError(f"bad key data {data!r}")
        return k

    def to_magic_key(self) -> str:
        mod = base64.urlsafe_b64encode(
            number.long_to_bytes(self.privkey.n)  # type: ignore
        ).decode("utf-8")
        pubexp = base64.urlsafe_b64encode(
            number.long_to_bytes(self.privkey.e)  # type: ignore
        ).decode("utf-8")
        return f"data:application/magic-public-key,RSA.{mod}.{pubexp}"
