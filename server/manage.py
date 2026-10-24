"""Пользователи сервера «Аудит магазина».

  python3 manage.py pilot 11472          — создать участников пилота (ДП подразделения, ДМ магазина,
                                           sales, region) и выдать каждому новый пароль
  python3 manage.py add dm11472          — добавить пользователя (роль и магазин по логину), пароль выдаётся
  python3 manage.py passwd sales         — выдать новый пароль (старые входы сбрасываются)
  python3 manage.py off dp1 / on dp1     — отключить / включить вход
  python3 manage.py list                 — список пользователей
  python3 manage.py cleanup              — сразу удалить фото старше срока хранения

Логины: dp1…dp5 (ДП подразделений), dm<код магазина>, sales (директор по продажам), region (директор региона).
Пароли печатаются только здесь, в консоли сервера: передайте их людям лично.
"""
import secrets
import sys

import app as srv

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def new_password() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(10))


def describe(login: str) -> tuple[str, str | None, str | None]:
    if login in ("sales", "region"):
        return login, None, None
    if login.startswith("dm") and login[2:] in srv.STORE_DIV:
        return "dm", srv.STORE_DIV[login[2:]], login[2:]
    for d in srv.REGION["dps"]:
        if d["login"] == login:
            return "dp", d["div"], None
    raise SystemExit(f"Не понимаю логин {login}: нужен dp1…dp5, dm<код магазина>, sales или region")


def add(con, login: str) -> str:
    role, div, store = describe(login)
    pw = new_password()
    salt = secrets.token_hex(16)
    con.execute(
        "INSERT INTO users (login, role, div, store, salt, pw_hash, active, created_at) VALUES (?,?,?,?,?,?,1,?) "
        "ON CONFLICT(login) DO UPDATE SET role=excluded.role, div=excluded.div, store=excluded.store, active=1",
        (login, role, div, store, salt, srv.hash_pw(pw, salt), srv.now_iso()),
    )
    srv.set_password(con, login, pw)
    return pw


def main(argv: list[str]) -> None:
    srv.init_db()
    if len(argv) < 2:
        print(__doc__)
        return
    cmd, args = argv[1], argv[2:]
    with srv.db() as con:
        if cmd == "pilot":
            stores = args or srv.ACTIVE_STORES
            logins = ["sales", "region"]
            for s in stores:
                if s not in srv.STORE_DIV:
                    raise SystemExit(f"Нет магазина {s} в справочнике")
                dp = next(d["login"] for d in srv.REGION["dps"] if d["div"] == srv.STORE_DIV[s])
                logins += [dp, f"dm{s}"]
            print("Логин        Пароль")
            for lg in dict.fromkeys(logins):
                print(f"{lg:<12} {add(con, lg)}")
        elif cmd == "add":
            for lg in args:
                print(f"{lg:<12} {add(con, lg)}")
        elif cmd == "passwd":
            for lg in args:
                if not con.execute("SELECT 1 FROM users WHERE login=?", (lg,)).fetchone():
                    raise SystemExit(f"Нет пользователя {lg}")
                pw = new_password()
                srv.set_password(con, lg, pw)
                print(f"{lg:<12} {pw}")
        elif cmd in ("off", "on"):
            for lg in args:
                con.execute("UPDATE users SET active=? WHERE login=?", (1 if cmd == "on" else 0, lg))
                if cmd == "off":
                    con.execute("DELETE FROM sessions WHERE login=?", (lg,))
            print("Готово")
        elif cmd == "list":
            for r in con.execute("SELECT login, role, div, store, active FROM users ORDER BY role, login"):
                print(f"{r['login']:<12} {r['role']:<7} {r['div'] or '':<15} {r['store'] or '':<7} {'вход разрешён' if r['active'] else 'отключён'}")
        elif cmd == "cleanup":
            print("Удалено фото:", srv.cleanup_photos())
        else:
            print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
