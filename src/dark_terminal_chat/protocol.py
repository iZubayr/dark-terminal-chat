"""The encryption format is shared with the original Node.js client."""

import base64
import hashlib
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAX_FRAME = 20_000
MAX_TEXT = 2_000
SESSION = re.compile(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}\Z")


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def new_code() -> str:
    return encode(secrets.token_bytes(32))


@dataclass(frozen=True)
class RoomKeys:
    room: str
    key: bytes


def room_keys(code: str) -> RoomKeys:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code):
        raise ValueError("Enter the 43-character code from the chat creator.")
    secret = decode(code)
    if encode(secret) != code:
        raise ValueError("Invalid code.")
    room = hashlib.sha256(b"dark-terminal-room-v1:" + secret).hexdigest()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"dark-terminal-v1",
               info=b"message-encryption").derive(secret)
    return RoomKeys(room, key)


def dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def parse(raw: str | bytes) -> dict:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_FRAME:
        raise ValueError("Invalid packet.")
    try:
        value = json.loads(raw)
    except (RecursionError, json.JSONDecodeError) as error:
        raise ValueError("Invalid JSON.") from error
    if not isinstance(value, dict):
        raise ValueError("Packet must be an object.")
    return value


def valid_envelope(value: dict) -> bool:
    return isinstance(value, dict) and value.get("type") == "message" and all(
        isinstance(value.get(field), str) and re.fullmatch(pattern, value[field])
        for field, pattern in (
            ("iv", r"[A-Za-z0-9_-]{16}"),
            ("tag", r"[A-Za-z0-9_-]{22}"),
            ("body", r"[A-Za-z0-9_-]{1,16000}"),
        )
    )


def encrypt(keys: RoomKeys, message: dict) -> dict:
    iv = secrets.token_bytes(12)
    encrypted = AESGCM(keys.key).encrypt(iv, dumps(message).encode("utf-8"), keys.room.encode())
    return {"type": "message", "iv": encode(iv), "body": encode(encrypted[:-16]),
            "tag": encode(encrypted[-16:])}


def decrypt(keys: RoomKeys, envelope: dict) -> dict:
    if not valid_envelope(envelope):
        raise ValueError("Invalid encrypted packet.")
    raw = AESGCM(keys.key).decrypt(decode(envelope["iv"]),
                                  decode(envelope["body"]) + decode(envelope["tag"]), keys.room.encode())
    return parse(raw.decode("utf-8"))


def safe_text(value: str) -> str:
    return "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf", "Zl", "Zp", "Cs"})


def valid_message(value: dict) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("kind"), str) and value["kind"] in {"hello", "chat", "ack"}
        and isinstance(value.get("name"), str) and 1 <= len(value["name"]) <= 24
        and isinstance(value.get("session"), str) and SESSION.fullmatch(value["session"]) is not None
        and type(value.get("seq")) is int and 1 <= value["seq"] <= 2**53 - 1
        and isinstance(value.get("text"), str) and len(value["text"]) <= MAX_TEXT
        and (value["kind"] != "ack" or type(value.get("ack")) is int and 1 <= value["ack"] <= 2**53 - 1)
    )
