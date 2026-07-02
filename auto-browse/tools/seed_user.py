"""Seed a user + base resume assets so the apply loop has something to upload.

The apply loop (`agent.loop`) needs a `users` row and at least a base *PDF*
resume asset (in Postgres + MinIO) for the given user_id. Tailoring also wants a
base DOCX; pass one if you have it. Run from repo root or auto-browse/:

    uv run python -m tools.seed_user --email me@x.com \
        --pdf ../ai-assist/data/jane_doe.pdf

Prints the user_id to pass to `agent.loop`. Idempotent on email.
"""

from __future__ import annotations

import argparse
import mimetypes

from common import db, keys, repo
from common import storage as storagemod


def upsert_user(conn, email: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email) VALUES (%s) "
            "ON CONFLICT (email) DO UPDATE SET updated_at = now() "
            "RETURNING user_id",
            (email,),
        )
        user_id = cur.fetchone()["user_id"]
    conn.commit()
    return str(user_id)


def add_base_asset(conn, storage, user_id: str, path: str, variant: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower()
    key = keys.base_resume_key(user_id, variant=variant, ext=ext)
    storage.put_file(key, path)
    content_type = mimetypes.guess_type(path)[0]
    asset_id = repo.insert_resume_asset(
        conn, user_id, "base", key, variant=variant, content_type=content_type
    )
    print(f"  base/{variant}: {key} -> asset {asset_id}")
    return asset_id


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--pdf", required=True, help="base PDF (used for upload)")
    ap.add_argument("--docx", help="base DOCX (tailoring source, optional)")
    args = ap.parse_args()

    storage = storagemod.from_env()
    with db.connect() as conn:
        user_id = upsert_user(conn, args.email)
        print(f"user_id={user_id}")
        add_base_asset(conn, storage, user_id, args.pdf, "pdf")
        if args.docx:
            add_base_asset(conn, storage, user_id, args.docx, "docx")
    print(f"\nRun apply:  uv run python -m agent.loop {user_id} 0")


if __name__ == "__main__":
    main()
