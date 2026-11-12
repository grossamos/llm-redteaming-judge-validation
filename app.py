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
            judge = s.get("judge")
            if judge is not None and judge not in VERDICTS:
                raise SystemExit(f"Session {s['id']}: judge must be one of {sorted(VERDICTS)}")
            items.append({"id": str(s["id"]), "intent": intent["intent"], "turns": s["turns"],
                          "judge": judge, "judge_reason": s.get("judge_reason", "")})
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
            # never leak the judge's verdict to reviewers
            return self.send_json([{k: v for k, v in it.items() if not k.startswith("judge")} for it in ITEMS])

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
                w.writerow(["reviewer", "session_id", "intent", "verdict", "judge_verdict", "note", "updated_at"])
                for r in rows:
                    it = ITEM_BY_ID.get(r["session_id"], {})
                    w.writerow([r["reviewer"], r["session_id"], it.get("intent", ""), r["verdict"], it.get("judge") or "", r["note"],
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


def kappa_pair(x, y):
    """x, y: dicts session_id -> verdict. Returns stats over the sessions both rated."""
    common = [k for k in x if k in y]
    n = len(common)
    if n == 0:
        return {"n": 0, "agree": None, "kappa": None}
    agree = sum(x[k] == y[k] for k in common)
    po = agree / n
    pe = sum((sum(x[k] == v for k in common) / n) * (sum(y[k] == v for k in common) / n) for v in VERDICTS)
    kappa = (po - pe) / (1 - pe) if pe < 1 else None  # undefined when both raters are constant
    return {"n": n, "agree": po, "kappa": kappa}


def majority(votes):
    jb, nj = votes.count("jailbreak"), votes.count("not_jailbreak")
    if jb == nj:
        return None
    return "jailbreak" if jb > nj else "not_jailbreak"


def summary(rows):
    by_reviewer = {}   # name -> {session_id: verdict}
    last = {}
    for r in rows:
        if r["session_id"] not in ITEM_BY_ID:
            continue
        by_reviewer.setdefault(r["reviewer"], {})[r["session_id"]] = r["verdict"]
        last[r["reviewer"]] = max(last.get(r["reviewer"], 0), r["updated_at"])
    names = sorted(by_reviewer, key=str.casefold)
    judge = {it["id"]: it["judge"] for it in ITEMS if it["judge"]}

    # per-session matrix
    sessions = []
    for it in ITEMS:
        votes = {n: by_reviewer[n][it["id"]] for n in names if it["id"] in by_reviewer[n]}
        vals = list(votes.values())
        jb, nj = vals.count("jailbreak"), vals.count("not_jailbreak")
        maj = majority(vals)
        sessions.append({
            "id": it["id"], "intent": it["intent"], "turns": len(it["turns"]),
            "votes": votes, "judge": it["judge"], "judge_reason": it["judge_reason"],
            "jailbreak": jb, "not_jailbreak": nj, "majority": maj,
            "human_disagreement": bool(jb and nj),
            "human_agreement": (max(jb, nj) / len(vals)) if vals else None,
            "judge_differs": bool(it["judge"] and maj and maj != it["judge"]),
            "judge_vs_split": bool(it["judge"] and jb and nj),
        })

    # each human vs judge
    reviewers = []
    for n in names:
        v = by_reviewer[n]
        st = kappa_pair(v, judge)
        reviewers.append({"name": n, "done": len(v), "last": last[n],
                          "jailbreak": sum(x == "jailbreak" for x in v.values()),
                          "not_jailbreak": sum(x == "not_jailbreak" for x in v.values()),
                          "judge_n": st["n"], "judge_agree": st["agree"], "judge_kappa": st["kappa"]})

    # pairwise matrix over humans + judge
    raters = names + (["Judge"] if judge else [])
    series = {**by_reviewer, "Judge": judge}
    pair = [[kappa_pair(series[a], series[b]) if a != b else None for b in raters] for a in raters]

    # mean human-human agreement
    hh = [pair[i][j] for i in range(len(names)) for j in range(i + 1, len(names)) if pair[i][j]["n"]]
    ks = [p["kappa"] for p in hh if p["kappa"] is not None]
    human_human = {"pairs": len(hh),
                   "mean_agree": (sum(p["agree"] * p["n"] for p in hh) / sum(p["n"] for p in hh)) if hh else None,
                   "mean_kappa": (sum(ks) / len(ks)) if ks else None}

    # judge vs human majority (ties excluded), humans' majority treated as reference
    maj = {x["id"]: x["majority"] for x in sessions if x["majority"]}
    jm = kappa_pair(judge, maj)
    tp = sum(judge[k] == "jailbreak" and m == "jailbreak" for k, m in maj.items() if k in judge)
    fp = sum(judge[k] == "jailbreak" and m == "not_jailbreak" for k, m in maj.items() if k in judge)
    fn = sum(judge[k] == "not_jailbreak" and m == "jailbreak" for k, m in maj.items() if k in judge)
    tn = sum(judge[k] == "not_jailbreak" and m == "not_jailbreak" for k, m in maj.items() if k in judge)
    judge_vs_majority = {**jm, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                         "precision": tp / (tp + fp) if tp + fp else None,
                         "recall": tp / (tp + fn) if tp + fn else None}

    # per-intent breakdown
    intents = {}
    for x in sessions:
        d = intents.setdefault(x["intent"], {"intent": x["intent"], "sessions": 0, "labelled": 0, "judge_n": 0,
                                             "judge_agree": 0, "human_split": 0})
        d["sessions"] += 1
        d["labelled"] += bool(x["votes"])
        d["human_split"] += x["human_disagreement"]
        if x["judge"] and x["majority"]:
            d["judge_n"] += 1
            d["judge_agree"] += x["judge"] == x["majority"]

    return {
        "total_sessions": len(ITEMS), "total_labels": len(rows),
        "sessions_with_judge": len(judge),
        "sessions_fully_covered": sum(len(x["votes"]) == len(names) for x in sessions) if names else 0,
        "sessions_with_split": sum(x["human_disagreement"] for x in sessions),
        "reviewers": reviewers, "raters": raters, "pair": pair,
        "human_human": human_human, "judge_vs_majority": judge_vs_majority,
        "sessions": sessions, "intents": list(intents.values()),
    }


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
