"""Аудит магазина — сервер пилота (FastAPI + SQLite + фото на диске).

Хранит визиты, задачи, фотоотчёты, оценки руководителей и комментарии как документы.
Клиент (index.html) забирает изменения по номеру последней синхронизации и отправляет
изменённые документы с проверкой версии. Права — по роли и магазинам пользователя.

Запуск: uvicorn app:app --host 127.0.0.1 --port 8010  (из папки server/)
Настройки — переменные окружения, см. README-DEPLOY.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

VERSION = "0.6.0"
HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("AUDIT_DB", HERE / "data" / "audit.db"))
PHOTO_DIR = Path(os.environ.get("AUDIT_PHOTOS", HERE / "data" / "photos"))
INDEX_HTML = Path(os.environ.get("AUDIT_INDEX", HERE.parent / "index.html"))
PHOTO_DAYS = int(os.environ.get("AUDIT_PHOTO_DAYS", "120"))
SESSION_DAYS = int(os.environ.get("AUDIT_SESSION_DAYS", "30"))
MAX_PHOTO = 8 * 1024 * 1024
MAX_DOC = 2 * 1024 * 1024
# Приложение с GitHub Pages обращается к серверу с другого адреса — разрешаем только его.
CORS_ORIGINS = [o.strip() for o in os.environ.get("AUDIT_CORS", "https://skbmaksim82-byte.github.io").split(",") if o.strip()]

REGION = json.loads((HERE / "region.json").read_text(encoding="utf-8"))
STORE_DIV: dict[str, str] = REGION["stores"]
_pilot = [s.strip() for s in os.environ.get("AUDIT_PILOT_STORES", "").split(",") if s.strip()]
ACTIVE_STORES = [s for s in _pilot if s in STORE_DIV] if _pilot else list(STORE_DIV)

KINDS = {"visits", "tasks", "reports", "reviews", "comments"}
ROLES = {"dp", "dm", "sales", "region"}
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{3,120}$")
PHOTO_RE = re.compile(r"^[a-z0-9]{6,60}$")

_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ db
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              login TEXT PRIMARY KEY, role TEXT NOT NULL, div TEXT, store TEXT,
              salt TEXT NOT NULL, pw_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT);
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY, login TEXT NOT NULL REFERENCES users(login) ON DELETE CASCADE,
              created_at TEXT, expires_at TEXT);
            CREATE TABLE IF NOT EXISTS docs (
              kind TEXT NOT NULL, id TEXT NOT NULL, store TEXT NOT NULL, data TEXT NOT NULL,
              ver INTEGER NOT NULL, seq INTEGER NOT NULL, updated_at TEXT, updated_by TEXT,
              PRIMARY KEY (kind, id));
            CREATE INDEX IF NOT EXISTS docs_seq ON docs(seq);
            CREATE TABLE IF NOT EXISTS photos (
              id TEXT PRIMARY KEY, store TEXT NOT NULL, ctype TEXT NOT NULL, size INTEGER,
              by TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            INSERT OR IGNORE INTO meta (k, v) VALUES ('seq', '0');
            """
        )


# ------------------------------------------------------------------ passwords & sessions
def hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def set_password(con: sqlite3.Connection, login: str, password: str) -> None:
    salt = secrets.token_hex(16)
    con.execute("UPDATE users SET salt=?, pw_hash=? WHERE login=?", (salt, hash_pw(password, salt), login))
    con.execute("DELETE FROM sessions WHERE login=?", (login,))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_fails: dict[str, list[float]] = {}


def throttled(key: str) -> bool:
    t = time.time()
    lst = [x for x in _fails.get(key, []) if t - x < 900]
    _fails[key] = lst
    return len(lst) >= 10


def user_public(row: sqlite3.Row) -> dict:
    return {"login": row["login"], "role": row["role"], "div": row["div"], "store": row["store"]}


def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Нужно войти")
    th = token_hash(authorization[7:].strip())
    with db() as con:
        row = con.execute(
            "SELECT u.* , s.expires_at FROM sessions s JOIN users u ON u.login = s.login WHERE s.token_hash=?", (th,)
        ).fetchone()
    if not row or not row["active"] or row["expires_at"] < now_iso():
        raise HTTPException(401, "Сессия истекла, войдите снова")
    return user_public(row)


def in_scope(user: dict, store: str | None) -> bool:
    if not store or store not in ACTIVE_STORES:
        return False
    if user["role"] == "dm":
        return store == user["store"]
    if user["role"] == "dp":
        return STORE_DIV.get(store) == user["div"]
    return user["role"] in ("sales", "region")


# ------------------------------------------------------------------ app
@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    yield


app = FastAPI(title="Аудит магазина", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"], max_age=86400,
    )


def client_ip(request: Request) -> str:
    """За туннелем или nginx все запросы приходят с 127.0.0.1 — берём адрес из X-Forwarded-For."""
    host = request.client.host if request.client else "?"
    if host in ("127.0.0.1", "::1"):
        fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if fwd:
            return fwd
    return host


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index() -> Response:
    if not INDEX_HTML.exists():
        return Response("index.html не найден", status_code=404)
    return FileResponse(INDEX_HTML, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "app": "audit-sibir", "version": VERSION, "stores": ACTIVE_STORES, "photo_days": PHOTO_DAYS}


@app.post("/api/login")
async def login(request: Request) -> dict:
    body = await request.json()
    lg = str(body.get("login", "")).strip().lower()
    pw = str(body.get("password", ""))
    ip = client_ip(request)
    if throttled(lg) or throttled(ip):
        raise HTTPException(429, "Слишком много попыток. Подождите 15 минут.")
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE login=?", (lg,)).fetchone()
        if not row or not row["active"] or not hmac.compare_digest(hash_pw(pw, row["salt"]), row["pw_hash"]):
            _fails.setdefault(lg, []).append(time.time())
            _fails.setdefault(ip, []).append(time.time())
            raise HTTPException(401, "Неверный логин или пароль")
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
        con.execute("INSERT INTO sessions VALUES (?,?,?,?)", (token_hash(token), lg, now_iso(), exp))
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
    return {"token": token, "user": user_public(row)}


@app.post("/api/logout")
def logout(authorization: str | None = Header(default=None)) -> dict:
    if authorization and authorization.startswith("Bearer "):
        with db() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(authorization[7:].strip()),))
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {"user": user}


@app.get("/api/sync")
def sync(since: int = 0, user: dict = Depends(current_user)) -> dict:
    with db() as con:
        seq = int(con.execute("SELECT v FROM meta WHERE k='seq'").fetchone()["v"])
        rows = con.execute("SELECT kind, id, store, data, ver, seq FROM docs WHERE seq > ? ORDER BY seq", (since,)).fetchall()
    docs = [{"kind": r["kind"], "id": r["id"], "ver": r["ver"], "data": json.loads(r["data"])} for r in rows if in_scope(user, r["store"])]
    return {"seq": seq, "docs": docs}


def _check_write(user: dict, kind: str, doc_id: str, data: dict, old: dict | None, con: sqlite3.Connection) -> None:
    role, lg = user["role"], user["login"]
    store = data.get("store")
    if not in_scope(user, store):
        raise HTTPException(403, "Магазин вне вашей зоны")
    if old is not None and old.get("store") != store:
        raise HTTPException(403, "Нельзя переносить запись в другой магазин")
    if data.get("id") != doc_id:
        raise HTTPException(400, "id документа не совпадает")
    if kind == "visits":
        if role != "dp":
            raise HTTPException(403, "Визит ведёт ДП")
    elif kind == "tasks":
        if role not in ("dp", "dm"):
            raise HTTPException(403, "Задачи меняют ДП и ДМ")
    elif kind == "reports":
        if role not in ("dp", "dm"):
            raise HTTPException(403, "Фотоотчёт меняют ДМ и ДП")
    elif kind == "reviews":
        if role not in ("sales", "region") or data.get("role") != role or data.get("by") != lg:
            raise HTTPException(403, "Оценку ставят директор по продажам и директор региона")
        if doc_id != f"rv-{data.get('visit')}-{role}":
            raise HTTPException(400, "Неверный id оценки")
        v = con.execute("SELECT data FROM docs WHERE kind='visits' AND id=?", (data.get("visit"),)).fetchone()
        if not v or json.loads(v["data"]).get("status") != "signed":
            raise HTTPException(409, "Визит ещё не подписан")
        if role == "region" and not con.execute("SELECT 1 FROM docs WHERE kind='reviews' AND id=?", (f"rv-{data.get('visit')}-sales",)).fetchone():
            raise HTTPException(409, "Сначала визит проверяет директор по продажам")
    elif kind == "comments":
        if old is None and data.get("by") != lg:
            raise HTTPException(403, "Комментарий можно оставить только от своего имени")
        if old is not None and old.get("by") != lg:
            raise HTTPException(403, "Чужой комментарий менять нельзя")


@app.put("/api/docs/{kind}/{doc_id}")
async def put_doc(kind: str, doc_id: str, request: Request, user: dict = Depends(current_user)) -> JSONResponse:
    if kind not in KINDS or not ID_RE.match(doc_id):
        raise HTTPException(404, "Нет такого типа записи")
    raw = await request.body()
    if len(raw) > MAX_DOC:
        raise HTTPException(413, "Запись слишком большая")
    body = json.loads(raw)
    data, base = body.get("data"), int(body.get("ver", 0))
    if not isinstance(data, dict):
        raise HTTPException(400, "Нет данных")
    with _lock, db() as con:
        row = con.execute("SELECT data, ver FROM docs WHERE kind=? AND id=?", (kind, doc_id)).fetchone()
        old = json.loads(row["data"]) if row else None
        cur_ver = row["ver"] if row else 0
        if base != cur_ver:
            if old is not None and in_scope(user, old.get("store")):
                return JSONResponse({"conflict": True, "ver": cur_ver, "data": old}, status_code=409)
            raise HTTPException(409, "Запись изменилась")
        _check_write(user, kind, doc_id, data, old, con)
        seq = int(con.execute("SELECT v FROM meta WHERE k='seq'").fetchone()["v"]) + 1
        con.execute("UPDATE meta SET v=? WHERE k='seq'", (str(seq),))
        con.execute(
            "INSERT INTO docs (kind,id,store,data,ver,seq,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data, ver=excluded.ver, seq=excluded.seq, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (kind, doc_id, data["store"], json.dumps(data, ensure_ascii=False), cur_ver + 1, seq, now_iso(), user["login"]),
        )
    return JSONResponse({"ver": cur_ver + 1, "seq": seq})


@app.post("/api/photos/{pid}")
async def put_photo(pid: str, store: str, request: Request, user: dict = Depends(current_user)) -> dict:
    if not PHOTO_RE.match(pid):
        raise HTTPException(400, "Неверный id фото")
    if not in_scope(user, store):
        raise HTTPException(403, "Магазин вне вашей зоны")
    ctype = request.headers.get("content-type", "").split(";")[0].strip()
    if ctype not in ("image/jpeg", "image/png"):
        raise HTTPException(415, "Нужно фото JPEG или PNG")
    raw = await request.body()
    if not raw or len(raw) > MAX_PHOTO:
        raise HTTPException(413, "Фото пустое или больше 8 МБ")
    ext = "jpg" if ctype == "image/jpeg" else "png"
    with _lock, db() as con:
        if con.execute("SELECT 1 FROM photos WHERE id=?", (pid,)).fetchone():
            return {"ok": True, "dup": True}
        path = PHOTO_DIR / f"{pid}.{ext}"
        tmp = path.with_suffix(".part")
        tmp.write_bytes(raw)
        tmp.replace(path)
        con.execute("INSERT INTO photos VALUES (?,?,?,?,?,?)", (pid, store, ctype, len(raw), user["login"], now_iso()))
    return {"ok": True}


@app.get("/api/photos/{pid}")
def get_photo(pid: str, user: dict = Depends(current_user)) -> Response:
    if not PHOTO_RE.match(pid):
        raise HTTPException(404, "Нет фото")
    with db() as con:
        row = con.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if not row or not in_scope(user, row["store"]):
        raise HTTPException(404, "Нет фото")
    path = PHOTO_DIR / f"{pid}.{'jpg' if row['ctype'] == 'image/jpeg' else 'png'}"
    if not path.exists():
        raise HTTPException(404, "Фото удалено по сроку хранения")
    return FileResponse(path, media_type=row["ctype"], headers={"Cache-Control": "private, max-age=86400"})


# ------------------------------------------------------------------ хранение фото: 4 месяца, подписи не удаляются
def cleanup_photos() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PHOTO_DAYS)).isoformat(timespec="seconds")
    n = 0
    with _lock, db() as con:
        for r in con.execute("SELECT id, ctype FROM photos WHERE at < ? AND id NOT LIKE 's%'", (cutoff,)).fetchall():
            (PHOTO_DIR / f"{r['id']}.{'jpg' if r['ctype'] == 'image/jpeg' else 'png'}").unlink(missing_ok=True)
            con.execute("DELETE FROM photos WHERE id=?", (r["id"],))
            n += 1
    return n


def _cleanup_loop() -> None:
    while True:
        try:
            cleanup_photos()
        except Exception:  # noqa: BLE001 — фоновая уборка не должна ронять сервер
            pass
        time.sleep(12 * 3600)
