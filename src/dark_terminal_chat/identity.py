"""Persistent identities and signed, sealed messages (libsodium primitives)."""

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass

from nacl.exceptions import BadSignatureError, CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey, VerifyKey

from .protocol import MAX_TEXT, decode, encode, safe_text

USER_ID = re.compile(r"[a-f0-9]{64}\Z")
MESSAGE_ID = re.compile(r"[a-f0-9]{32}\Z")
RETENTION = 30 * 24 * 60 * 60


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def unbase(value, length=None, maximum=16000):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid encoding.")
    raw = decode(value)
    if encode(raw) != value or (length is not None and len(raw) != length):
        raise ValueError("Invalid encoding.")
    return raw


def fingerprint(public):
    if not isinstance(public, dict) or set(public) != {"sign", "box"}:
        raise ValueError("Invalid public keys.")
    raw = unbase(public["sign"], 32) + unbase(public["box"], 32)
    return hashlib.sha256(b"dark-chat-identity-v1:" + raw).hexdigest()


def auth_data(nonce, public, register):
    unbase(nonce, 32)
    if type(register) is not bool:
        raise ValueError("Invalid authentication.")
    return b"dark-chat-auth-v1:" + canonical({"nonce": nonce, "id": fingerprint(public), "register": register})


def verify_signature(public, data, signature):
    try:
        VerifyKey(unbase(public["sign"], 32)).verify(data, unbase(signature, 64))
    except (BadSignatureError, KeyError) as error:
        raise ValueError("Invalid signature.") from error


@dataclass
class Identity:
    name: str
    signing: SigningKey
    encryption: PrivateKey
    storage_key: bytes

    @classmethod
    def create(cls, name):
        if not isinstance(name, str) or not 1 <= len(name) <= 24 or safe_text(name) != name:
            raise ValueError("Name must be 1-24 printable characters.")
        return cls(name, SigningKey.generate(), PrivateKey.generate(), secrets.token_bytes(32))

    @property
    def public(self):
        return {"sign": encode(bytes(self.signing.verify_key)), "box": encode(bytes(self.encryption.public_key))}

    @property
    def id(self):
        return fingerprint(self.public)

    def sign(self, data):
        return encode(self.signing.sign(data).signature)

    def export(self):
        return {"name": self.name, "sign": encode(bytes(self.signing)),
                "box": encode(bytes(self.encryption)), "storage": encode(self.storage_key)}

    @classmethod
    def restore(cls, data):
        if not isinstance(data, dict) or set(data) != {"name", "sign", "box", "storage"}:
            raise ValueError("Invalid identity file.")
        name = data["name"]
        if not isinstance(name, str) or not 1 <= len(name) <= 24 or safe_text(name) != name:
            raise ValueError("Invalid identity name.")
        return cls(name, SigningKey(unbase(data["sign"], 32)), PrivateKey(unbase(data["box"], 32)),
                   unbase(data["storage"], 32))


def seal_message(identity, recipient, text):
    peer_id = fingerprint(recipient)
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT or safe_text(text) != text:
        raise ValueError("Message must be 1-2000 printable characters.")
    frame = {"v": 1, "id": secrets.token_hex(16), "from": identity.id, "to": peer_id,
             "public": identity.public, "created": int(time.time()),
             "body": encode(SealedBox(PublicKey(unbase(recipient["box"], 32))).encrypt(
                 canonical({"name": identity.name, "text": text})))}
    frame["signature"] = identity.sign(b"dark-chat-message-v1:" + canonical(frame))
    return frame


def verify_message(frame, now=None):
    if (not isinstance(frame, dict) or set(frame) != {"v", "id", "from", "to", "public", "created", "body", "signature"}
            or type(frame["v"]) is not int or frame["v"] != 1
            or not isinstance(frame["id"], str) or not MESSAGE_ID.fullmatch(frame["id"])
            or not isinstance(frame["to"], str) or not USER_ID.fullmatch(frame["to"])
            or frame["from"] != fingerprint(frame["public"])
            or type(frame["created"]) is not int):
        raise ValueError("Invalid message envelope.")
    if now is not None and not now - RETENTION <= frame["created"] <= now + 300:
        raise ValueError("Message expired or device clock is incorrect.")
    if not 48 <= len(unbase(frame["body"], maximum=14000)) <= 10000:
        raise ValueError("Invalid ciphertext.")
    unsigned = {key: value for key, value in frame.items() if key != "signature"}
    verify_signature(frame["public"], b"dark-chat-message-v1:" + canonical(unsigned), frame["signature"])
    return frame


def open_message(identity, frame):
    verify_message(frame)
    if frame["to"] != identity.id:
        raise ValueError("Message is addressed to another identity.")
    try:
        data = json.loads(SealedBox(identity.encryption).decrypt(unbase(frame["body"])))
    except (CryptoError, ValueError, UnicodeError, RecursionError) as error:
        raise ValueError("Unable to decrypt message.") from error
    if (not isinstance(data, dict) or set(data) != {"name", "text"}
            or not isinstance(data["name"], str) or not 1 <= len(data["name"]) <= 24
            or not isinstance(data["text"], str) or not 1 <= len(data["text"]) <= MAX_TEXT):
        raise ValueError("Invalid message content.")
    return {"name": safe_text(data["name"]), "text": safe_text(data["text"])}
