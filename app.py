#!/usr/bin/env python3
"""Judge-validation web app (stdlib only).

Reviewers enter a name, read a session (prompts/responses) under an intent and
label it as jailbreak / not jailbreak. Labels are stored in SQLite and
reviewers can resume later by entering the same name. Admin view at /admin.

Run:  ADMIN_TOKEN=secret python3 app.py [--port 8000] [--host 127.0.0.1] [--data data.json]
"""
import argparse
import csv
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).parent
STATIC = BASE / "static"
VERDICTS = {"jailbreak", "not_jailbreak"}
MAX_BODY = 64 * 1024

DB_PATH = BASE / "results.db"
ITEMS = []        # flat list of sessions, in presentation order
ITEM_BY_ID = {}
ADMIN_TOKEN = ""


# ---------- data ----------
def load_items(path):
    """data.json: [{"intent": str, "sessions": [{"id": str, "turns": [{"prompt","response"}]}]}]"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = []
    for intent in raw:
        for s in intent["sessions"]:
            items.append({"id": str(s["id"]), "intent": intent["intent"], "turns": s["turns"]})
    ids = [i["id"] for i in items]
    if len(ids) != len(set(ids)):
        raise SystemExit("Duplicate session ids in data file")
    return items


# ---------- db ----------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(
            """CREATE TABLE IF NOT EXISTS labels (
                reviewer   TEXT NOT NULL,
                session_id TEXT NOT NULL,
                verdict    TEXT NOT NULL,
                note       TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY (reviewer, session_id)
            )"""
        )


def clean_name(name):
    if not isinstance(name, str):
        return None
    name = unicodedata.normalize("NFC", name).strip()
    # drop control chars, keep everything else (unicode, emoji)
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C" or ch in "‍️")
    name = " ".join(name.split())
    if not name or len(name) > 64:
        return None
    return name


# ---------- http ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "JudgeValidation/1.0"

    def log_message(self, fmt, *args):
        pass

    # helpers
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def is_admin(self):
        tok = self.headers.get("X-Admin-Token", "")
        return hmac.compare_digest(tok.encode(), ADMIN_TOKEN.encode())

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # routes
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        p = url.path

        if p in ("/", "/index.html"):
            return self.send_bytes((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if p in ("/admin", "/admin.html"):
            return self.send_bytes((STATIC / "admin.html").read_bytes(), "text/html; charset=utf-8")

        if p == "/api/items":
            return self.send_json(ITEMS)

        if p == "/api/labels":
            name = clean_name((q.get("reviewer") or [""])[0])
            if not name:
                return self.send_json({"error": "invalid reviewer name"}, 400)
            with db() as c:
                rows = c.execute(
                    "SELECT session_id, verdict, note FROM labels WHERE reviewer=?", (name,)
                ).fetchall()
            return self.send_json({"reviewer": name, "labels": {r["session_id"]: {"verdict": r["verdict"], "note": r["note"]} for r in rows}})

        if p.startswith("/api/admin/"):
            if not self.is_admin():
                return self.send_json({"error": "unauthorized"}, 401)
            with db() as c:
                rows = c.execute("SELECT * FROM labels ORDER BY updated_at").fetchall()
            if p == "/api/admin/summary":
                return self.send_json(summary(rows))
            if p == "/api/admin/export.csv":
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["reviewer", "session_id", "intent", "verdict", "note", "updated_at"])
                for r in rows:
                    it = ITEM_BY_ID.get(r["session_id"], {})
                    w.writerow([r["reviewer"], r["session_id"], it.get("intent", ""), r["verdict"], r["note"],
                                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["updated_at"]))])
                return self.send_bytes(("﻿" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                                       extra={"Content-Disposition": 'attachment; filename="labels.csv"'})
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/label":
            d = self.read_json()
            if not isinstance(d, dict):
                return self.send_json({"error": "bad request"}, 400)
            name = clean_name(d.get("reviewer"))
            sid, verdict = d.get("session_id"), d.get("verdict")
            note = d.get("note", "")
            if not name or sid not in ITEM_BY_ID or verdict not in VERDICTS or not isinstance(note, str):
                return self.send_json({"error": "invalid input"}, 400)
            with db() as c:
                c.execute(
                    """INSERT INTO labels (reviewer, session_id, verdict, note, updated_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(reviewer, session_id) DO UPDATE SET
                         verdict=excluded.verdict, note=excluded.note, updated_at=excluded.updated_at""",
                    (name, sid, verdict, note[:2000], time.time()),
                )
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)


def summary(rows):
    per_session = {it["id"]: {"id": it["id"], "intent": it["intent"], "jailbreak": 0, "not_jailbreak": 0}
                   for it in ITEMS}
    reviewers = {}
    for r in rows:
        if r["session_id"] in per_session:
            per_session[r["session_id"]][r["verdict"]] += 1
        rv = reviewers.setdefault(r["reviewer"], {"name": r["reviewer"], "done": 0, "last": 0, "jailbreak": 0,
                                                  "not_jailbreak": 0})
        rv["done"] += 1
        rv[r["verdict"]] += 1
        rv["last"] = max(rv["last"], r["updated_at"])
    sessions = list(per_session.values())
    for s in sessions:
        decided = s["jailbreak"] + s["not_jailbreak"]
        s["disagreement"] = bool(s["jailbreak"] and s["not_jailbreak"])
        s["agreement"] = (max(s["jailbreak"], s["not_jailbreak"]) / decided) if decided else None
    return {"total_sessions": len(ITEMS), "sessions": sessions,
            "reviewers": sorted(reviewers.values(), key=lambda x: x["name"].casefold()),
            "total_labels": len(rows)}


def main():
    global DB_PATH, ITEMS, ITEM_BY_ID, ADMIN_TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default=str(BASE / "data.json"))
    ap.add_argument("--db", default=str(DB_PATH))
    a = ap.parse_args()

    DB_PATH = Path(a.db)
    ITEMS = load_items(a.data)
    ITEM_BY_ID = {i["id"]: i for i in ITEMS}
    ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN") or secrets.token_urlsafe(12)
    init_db()

    print(f"Loaded {len(ITEMS)} sessions from {a.data}")
    if not os.environ.get("ADMIN_TOKEN"):
        print(f"ADMIN_TOKEN not set; generated one for this run: {ADMIN_TOKEN}")
    print(f"Reviewers: http://{a.host}:{a.port}/   Admin: http://{a.host}:{a.port}/admin")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
