"""Create (or promote) an admin account without seeding the dev users.

    python -m scripts.create_admin admin@example.com        # prompts for a password
    ADMIN_PASSWORD=... python -m scripts.create_admin admin@example.com
"""
import getpass
import os
import sys

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models.auth import User


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m scripts.create_admin <email>")
    email = sys.argv[1].strip().lower()
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password (min 10 chars): ")
    if len(password) < 10 or len(password.encode()) > 72:
        raise SystemExit("password must be 10-72 bytes")
    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user:
            user.role, user.is_active = "admin", True
            user.password_hash = hash_password(password)
        else:
            db.add(User(email=email, password_hash=hash_password(password), role="admin"))
        db.commit()
    print(f"admin ready: {email}")


if __name__ == "__main__":
    main()
