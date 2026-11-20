"""One-use embed codes independent of document storage."""

import sqlite3
from pathlib import Path


class AuthStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.staging = directory / "staging"
        self.staging.mkdir(exist_ok=True)
        self.path = directory / "auth.sqlite3"
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS redeemed_codes (code_id TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")

    def redeem(self, code_id, expires_at, current):
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM redeemed_codes WHERE expires_at <= ?", (current,))
            return connection.execute("INSERT OR IGNORE INTO redeemed_codes VALUES (?, ?)", (code_id, expires_at)).rowcount == 1
