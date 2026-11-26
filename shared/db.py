import os, time
from pathlib import Path
import psycopg

DATABASE_URL=os.environ["DATABASE_URL"]

def connect():
    return psycopg.connect(DATABASE_URL)

def initialize_database(retries=30,delay=2):
    sql=Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    last=None
    for _ in range(retries):
        try:
            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                conn.commit()
            return
        except Exception as exc:
            last=exc
            time.sleep(delay)
    raise RuntimeError(last)
