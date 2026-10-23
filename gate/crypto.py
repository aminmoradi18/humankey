"""Signatures used by the gate (ARCHITECTURE.md section 2)."""
import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from common.jsonl import canonical


def sha256(text):
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def b64(b):
    return base64.b64encode(b).decode()


def decision_message(req, contract_sha, text_sha, decision, counter):
    return f"{req}|{contract_sha}|{text_sha}|{decision}|{counter}".encode()


def public_key_b64(private_key):
    return b64(private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def verify_ed25519(pub_b64, message, sig_b64):
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64)).verify(base64.b64decode(sig_b64), message)
        return True
    except (InvalidSignature, ValueError):
        return False


def new_private_key():
    return Ed25519PrivateKey.generate()


def contract_secret(path):
    """HMAC secret for contracts, readable only by the gate's OS user."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(32))
        path.chmod(0o600)
    return path.read_bytes()


def sign_contract(contract, secret):
    body = {k: v for k, v in contract.items() if k != "signature"}
    return hmac.new(secret, canonical(body).encode(), hashlib.sha256).hexdigest()


def contract_ok(contract, secret):
    return hmac.compare_digest(sign_contract(contract, secret), contract.get("signature", ""))
