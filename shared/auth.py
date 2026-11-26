import os
from werkzeug.security import generate_password_hash
from shared.db import connect


def ensure_admin_user():
    username=os.environ["PORTAL_USERNAME"]
    password=os.environ["PORTAL_PASSWORD"]
    password_hash=generate_password_hash(password)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO config.users(username,password_hash,role,active,updated_at)
            VALUES(%s,%s,'admin',TRUE,NOW())
            ON CONFLICT(username) DO UPDATE SET
              password_hash=EXCLUDED.password_hash,role='admin',active=TRUE,updated_at=NOW()
        """,(username,password_hash))
        conn.commit()
