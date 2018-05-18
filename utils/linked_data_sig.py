from pyld import jsonld
import hashlib
from datetime import datetime

from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA256
import base64


def options_hash(doc):
    doc = dict(doc['signature'])
    for k in ['type', 'id', 'signatureValue']:
        if k in doc:
            del doc[k]
    doc['@context'] = 'https://w3id.org/identity/v1'
    normalized = jsonld.normalize(doc, {'algorithm': 'URDNA2015', 'format': 'application/nquads'})
    h = hashlib.new('sha256')
    h.update(normalized.encode('utf-8'))
    return h.hexdigest()


def doc_hash(doc):
    doc = dict(doc)
    if 'signature' in doc:
        del doc['signature']
    normalized = jsonld.normalize(doc, {'algorithm': 'URDNA2015', 'format': 'application/nquads'})
    h = hashlib.new('sha256')
    h.update(normalized.encode('utf-8'))
    return h.hexdigest()


def verify_signature(doc, pubkey):
    to_be_signed = options_hash(doc) + doc_hash(doc)
    signature = doc['signature']['signatureValue']
    signer = PKCS1_v1_5.new(pubkey)
    digest = SHA256.new()
    digest.update(to_be_signed.encode('utf-8'))
    return signer.verify(digest, base64.b64decode(signature))


def generate_signature(doc, privkey):
    options = {
      'type': 'RsaSignature2017',
      'creator': doc['actor'] + '#main-key',
      'created': datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
    }
    doc['signature'] = options
    to_be_signed = options_hash(doc) + doc_hash(doc)
    signer = PKCS1_v1_5.new(privkey)
    digest = SHA256.new()
    digest.update(to_be_signed.encode('utf-8'))
    sig = base64.b64encode(signer.sign(digest))
    options['signatureValue'] = sig.decode('utf-8')
