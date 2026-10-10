"""Encrypted local keys/history and a bounded ciphertext-only relay mailbox."""

import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .identity import Identity, RETENTION, canonical, fingerprint, unbase, verify_message
from .protocol import encode

VAULT_AAD = b"dark-chat-vault-v1"


def private_directory(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink():
        raise ValueError("Data directory must not be a symlink.")
    if os.name == "posix":
        folder.chmod(0o700)
    return folder


def private_write(path, data):
    # Exclusive creation never replaces an existing identity or backup.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def default_home():
    return Path(os.environ.get("DARK_CHAT_HOME", str(Path.home() / ".dark-chat")))


def password_key(password, salt):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("Use a password of 12-1024 characters.")
    return Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password.encode("utf-8"))


def create_vault(folder, name, password):
    folder = private_directory(folder)
    if (folder / "identity.json").exists():
        raise ValueError("An identity already exists. Use --login.")
    identity = Identity.create(name)
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    encrypted = AESGCM(password_key(password, salt)).encrypt(nonce, canonical(identity.export()), VAULT_AAD)
    private_write(folder / "identity.json", canonical({"v": 1, "salt": encode(salt),
                                                       "nonce": encode(nonce), "body": encode(encrypted)}))
    return identity


def read_vault(path, password):
    path = Path(path)
    if path.stat().st_size > 8192:
        raise ValueError("Invalid identity file.")
    try:
        data = json.loads(path.read_bytes())
        if not isinstance(data, dict) or set(data) != {"v", "salt", "nonce", "body"} or data["v"] != 1:
            raise ValueError("Invalid identity file.")
        raw = AESGCM(password_key(password, unbase(data["salt"], 16))).decrypt(
            unbase(data["nonce"], 12), unbase(data["body"], maximum=6000), VAULT_AAD)
        return Identity.restore(json.loads(raw))
    except InvalidTag as error:
        raise ValueError("Wrong password or damaged identity file.") from error
    except (KeyError, TypeError, RecursionError) as error:
        raise ValueError("Invalid identity file.") from error


def database(path):
    path = Path(path)
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Database must not be a symlink.")
    if not path.exists():
        private_write(path, b"")
    if os.name == "posix":
        path.chmod(0o600)
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA secure_delete=ON")
    return db


class LocalStore:
    def __init__(self, folder, identity):
        self.folder, self.identity = Path(folder), identity
        self.db = database(self.folder / "history.db")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS owner (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS contacts (id TEXT PRIMARY KEY, body BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY, peer TEXT NOT NULL, direction TEXT NOT NULL,
                pending INTEGER NOT NULL, body BLOB NOT NULL);
        """)
        row = self.db.execute("SELECT id FROM owner").fetchone()
        if row and row[0] != identity.id:
            self.db.close()
            raise ValueError("This history belongs to another identity. Use a separate data directory.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO owner VALUES (?)", (identity.id,))

    def close(self):
        self.db.close()

    def encrypt(self, tag, value):
        nonce = secrets.token_bytes(12)
        return nonce + AESGCM(self.identity.storage_key).encrypt(nonce, canonical(value), tag.encode())

    def decrypt(self, tag, value):
        return json.loads(AESGCM(self.identity.storage_key).decrypt(value[:12], value[12:], tag.encode()))

    def contacts(self):
        return {uid: self.decrypt("contact:" + uid, body) for uid, body in self.db.execute("SELECT id,body FROM contacts ORDER BY rowid")}

    def add_contact(self, public, alias):
        uid = fingerprint(public)
        existing = self.contacts()
        # Display names never replace contact identity or a chosen local alias.
        if uid in existing:
            return uid
        if not alias or any(value["alias"] == alias for value in existing.values()):
            alias = uid
        with self.db:
            self.db.execute("INSERT INTO contacts VALUES (?,?)",
                            (uid, self.encrypt("contact:" + uid, {"public": public, "alias": alias})))
        return uid

    def rename_contact(self, uid, alias):
        from .protocol import safe_text
        contacts = self.contacts()
        if (uid not in contacts or not 1 <= len(alias) <= 24 or safe_text(alias) != alias or alias.isdecimal()
                or any(other != uid and contact["alias"] == alias for other, contact in contacts.items())):
            raise ValueError("Choose an unused contact name of 1-24 printable characters, not just a number.")
        with self.db:
            self.db.execute("UPDATE contacts SET body=? WHERE id=?",
                            (self.encrypt("contact:" + uid, {**contacts[uid], "alias": alias}), uid))

    def save(self, frame, content, direction, pending=False):
        uid = frame["to"] if direction == "out" else frame["from"]
        tag = f"message:{frame['id']}:{uid}:{direction}"
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?,?)",
                                     (frame["id"], uid, direction, int(pending), self.encrypt(tag, {"envelope": frame, **content})))
        return cursor.rowcount == 1

    def messages(self, peer=None, pending=False, limit=100, exclude_peers=()):
        sql, values = "SELECT id,peer,direction,pending,body FROM history WHERE 1=1", []
        if peer:
            sql += " AND peer=?"
            values.append(peer)
        if pending:
            sql += " AND pending=1"
        if exclude_peers:
            sql += " AND peer NOT IN (" + ",".join("?" for _ in exclude_peers) + ")"
            values.extend(exclude_peers)
        sql += " ORDER BY rowid " + ("ASC" if pending else "DESC") + " LIMIT ?"
        values.append(limit)
        rows = self.db.execute(sql, values).fetchall()
        return [{"id": mid, "peer": uid, "direction": direction, "pending": bool(queued),
                 **self.decrypt(f"message:{mid}:{uid}:{direction}", body)}
                for mid, uid, direction, queued, body in (rows if pending else reversed(rows))]

    def sent(self, mid):
        with self.db:
            self.db.execute("UPDATE history SET pending=0 WHERE id=? AND direction='out'", (mid,))

    def expire(self, mid):
        row = self.db.execute("SELECT peer,body FROM history WHERE id=? AND direction='out' AND pending=1", (mid,)).fetchone()
        if row:
            uid, body = row
            tag = f"message:{mid}:{uid}:out"
            content = {**self.decrypt(tag, body), "expired": True}
            with self.db:
                self.db.execute("UPDATE history SET pending=0,body=? WHERE id=?", (self.encrypt(tag, content), mid))

    def backup(self):
        destination = private_directory(self.folder / "backups") / ("identity-" + secrets.token_hex(8) + ".json")
        private_write(destination, (self.folder / "identity.json").read_bytes())
        return destination


class Mailbox:
    def __init__(self, path, max_users=1000, max_pending=1000, per_user=100, max_receipts=10000):
        self.db = database(path)
        self.max_users, self.max_pending = max_users, max_pending
        self.per_user, self.max_receipts = per_user, max_receipts
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, public TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mail (
                id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
                created INTEGER NOT NULL, digest TEXT NOT NULL, body TEXT);
            CREATE INDEX IF NOT EXISTS inbox ON mail(recipient,created);
        """)

    def close(self):
        self.db.close()

    def register(self, public):
        uid = fingerprint(public)
        if not self.lookup(uid):
            if self.db.execute("SELECT count(*) FROM users").fetchone()[0] >= self.max_users:
                raise ValueError("Registration capacity reached.")
            with self.db:
                self.db.execute("INSERT INTO users VALUES (?,?)", (uid, canonical(public).decode()))
        return uid

    def lookup(self, uid):
        row = self.db.execute("SELECT public FROM users WHERE id=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else None

    def purge(self):
        with self.db:
            self.db.execute("DELETE FROM mail WHERE created < ?", (int(time.time()) - RETENTION,))

    def send(self, sender, frame):
        import hashlib
        verify_message(frame, now=int(time.time()))
        if frame["from"] != sender or not self.lookup(frame["to"]):
            raise ValueError("Unknown recipient or invalid sender.")
        self.purge()
        body = canonical(frame).decode()
        digest = hashlib.sha256(body.encode()).hexdigest()
        old = self.db.execute("SELECT digest FROM mail WHERE id=?", (frame["id"],)).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError("Message ID conflict.")
            return
        total, pending = self.db.execute("SELECT count(*),count(body) FROM mail").fetchone()
        inbox = self.db.execute("SELECT count(*) FROM mail WHERE recipient=? AND body IS NOT NULL", (frame["to"],)).fetchone()[0]
        if total >= self.max_receipts or pending >= self.max_pending or inbox >= self.per_user:
            raise ValueError("Mailbox is full. Message kept on your device.")
        with self.db:
            self.db.execute("INSERT INTO mail VALUES (?,?,?,?,?,?)",
                            (frame["id"], sender, frame["to"], frame["created"], digest, body))

    def fetch(self, uid):
        self.purge()
        # One packet per fetch keeps the WebSocket frame bound independent of text size.
        row = self.db.execute("SELECT body FROM mail WHERE recipient=? AND body IS NOT NULL ORDER BY created,rowid LIMIT 1", (uid,)).fetchone()
        return json.loads(row[0]) if row else None

    def acknowledge(self, uid, mid):
        with self.db:
            self.db.execute("UPDATE mail SET body=NULL WHERE id=? AND recipient=?", (mid, uid))
