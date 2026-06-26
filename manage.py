"""One-off command-line admin tasks.

Usage:
  python manage.py init-db
  python manage.py create-admin --username alice [--password ...]
  python manage.py create-user  --username bob   --role user
  python manage.py list-feedback
  python manage.py reconcile

create-admin is limited to initial administrator provisioning and refuses to run
when an administrator account already exists.
"""

import argparse
import getpass
import sys

from config import Config
from app import db
from app.auth import admin_exists, create_user


def cmd_init_db(args):
    db.init_db(Config.DB_PATH)
    print(f"Database ready at {Config.DB_PATH}")


def _read_password(args):
    if args.password:
        pw = args.password
    else:
        pw1 = getpass.getpass("Password: ")
        pw2 = getpass.getpass("Repeat password: ")
        if pw1 != pw2:
            sys.exit("Passwords do not match.")
        pw = pw1
    if not pw:
        sys.exit("Password must not be empty.")
    if len(pw) < 12:
        sys.exit("Password must be at least 12 characters.")
    return pw


def cmd_create_admin(args):
    conn = db.connect(Config.DB_PATH)
    try:
        if admin_exists(conn):
            sys.exit("Refusing: an admin account already exists.")
        password = _read_password(args)
        conn.execute("BEGIN IMMEDIATE")
        try:
            create_user(conn, args.username, password, "admin")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        print(f"Admin '{args.username}' created.")
    finally:
        conn.close()


def cmd_create_user(args):
    conn = db.connect(Config.DB_PATH)
    try:
        password = _read_password(args)
        conn.execute("BEGIN IMMEDIATE")
        try:
            create_user(conn, args.username, password, args.role)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        print(f"User '{args.username}' ({args.role}) created.")
    finally:
        conn.close()


def cmd_list_feedback(args):
    conn = db.connect(Config.DB_PATH)
    try:
        rows = db.list_feedback(conn)
        if not rows:
            print("No feedback yet.")
            return
        for r in rows:
            who = r["username"] or "(deleted user)"
            print(f"#{r['id']}  {r['timestamp']}  {who}  [{r['page']}]")
            print(f"    {r['message']}")
    finally:
        conn.close()


def cmd_reconcile(args):
    conn = db.connect(Config.DB_PATH)
    try:
        rows = db.reconcile(conn)
        bad = [r for r in rows if not r["ok"]]
        if not bad:
            print(f"OK: all {len(rows)} batch(es) reconcile.")
            return
        print(f"MISMATCH: {len(bad)} of {len(rows)} batch(es) do not balance:")
        for r in bad:
            print(f"  supply #{r['supply_number']} ({r['product_name']}): "
                  f"received {r['received_total']}, accounted {r['accounted']} "
                  f"[{r['reason']}]")
        sys.exit(1)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="stock manager admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create/upgrade the database").set_defaults(func=cmd_init_db)

    p_admin = sub.add_parser("create-admin", help="create the first admin")
    p_admin.add_argument("--username", required=True)
    p_admin.add_argument("--password", help="omit to be prompted (safer)")
    p_admin.set_defaults(func=cmd_create_admin)

    p_user = sub.add_parser("create-user", help="create an additional account")
    p_user.add_argument("--username", required=True)
    p_user.add_argument("--password", help="omit to be prompted (safer)")
    p_user.add_argument("--role", choices=["admin", "user"], default="user")
    p_user.set_defaults(func=cmd_create_user)

    sub.add_parser("list-feedback",
                   help="print all in-app feedback"
                   ).set_defaults(func=cmd_list_feedback)

    sub.add_parser("reconcile",
                   help="check conservation per batch; exit non-zero on mismatch"
                   ).set_defaults(func=cmd_reconcile)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
