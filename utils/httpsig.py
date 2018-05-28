"""Implements HTTP signature for Flask requests.

Mastodon instances won't accept requests that are not signed using this scheme.

"""
from datetime import datetime
from urllib.parse import urlparse
from typing import Any, Dict
import base64
import hashlib
import logging

from flask import request
from requests.auth import AuthBase

from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA256

logger = logging.getLogger(__name__)


def _build_signed_string(signed_headers: str, method: str, path: str, headers: Any, body_digest: str) -> str:
    out = []
    for signed_header in signed_headers.split(' '):
        if signed_header == '(request-target)':
            out.append('(request-target): '+method.lower()+' '+path)
        elif signed_header == 'digest':
            out.append('digest: '+body_digest)
        else:
            out.append(signed_header+': '+headers[signed_header])
    return '\n'.join(out)


def _parse_sig_header(val: str) -> Dict[str, str]:
    out = {}
    for data in val.split(','):
        k, v = data.split('=', 1)
        out[k] = v[1:len(v)-1]
    return out


def _verify_h(signed_string, signature, pubkey):
    signer = PKCS1_v1_5.new(pubkey)
    digest = SHA256.new()
    digest.update(signed_string.encode('utf-8'))
    return signer.verify(digest, signature)


def _body_digest() -> str:
    h = hashlib.new('sha256')
    h.update(request.data)
    return 'SHA-256='+base64.b64encode(h.digest()).decode('utf-8')


def verify_request(actor_service) -> bool:
    hsig = _parse_sig_header(request.headers.get('Signature'))
    logger.debug(f'hsig={hsig}')
    signed_string = _build_signed_string(hsig['headers'], request.method, request.path, request.headers, _body_digest())
    _, rk = actor_service.get_public_key(hsig['keyId'])
    return _verify_h(signed_string, base64.b64decode(hsig['signature']), rk)


class HTTPSigAuth(AuthBase):
    def __init__(self, keyid, privkey):
        self.keyid = keyid
        self.privkey = privkey

    def __call__(self, r):
        logger.info(f'keyid={self.keyid}')
        host = urlparse(r.url).netloc
        bh = hashlib.new('sha256')
        bh.update(r.body.encode('utf-8'))
        bodydigest = 'SHA-256='+base64.b64encode(bh.digest()).decode('utf-8')
        date = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
        r.headers.update({'Digest': bodydigest, 'Date': date})
        r.headers.update({'Host': host})
        sigheaders = '(request-target) user-agent host date digest content-type'
        to_be_signed = _build_signed_string(sigheaders, r.method, r.path_url, r.headers, bodydigest)
        signer = PKCS1_v1_5.new(self.privkey)
        digest = SHA256.new()
        digest.update(to_be_signed.encode('utf-8'))
        sig = base64.b64encode(signer.sign(digest))
        sig = sig.decode('utf-8')
        headers = {
            'Signature': f'keyId="{self.keyid}",algorithm="rsa-sha256",headers="{sigheaders}",signature="{sig}"'
        }
        logger.info(f'signed request headers={headers}')
        r.headers.update(headers)
        return r
