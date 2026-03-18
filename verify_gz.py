"""Verify compressed DB by fully decompressing and checking tables."""
import gzip
import os
import sqlite3
import tempfile

src = "data/platysearch.db.gz"
tmp = tempfile.mktemp(suffix=".db")

print(f"Decompressing {src} to {tmp}...")
with gzip.open(src, "rb") as fin, open(tmp, "wb") as fout:
    while True:
        chunk = fin.read(8 * 1024 * 1024)
        if not chunk:
            break
        fout.write(chunk)

size_mb = os.path.getsize(tmp) / (1024 * 1024)
print(f"Decompressed size: {size_mb:.1f} MB")

c = sqlite3.connect(tmp)
tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("Tables:", [t[0] for t in tables])
for t in tables:
    count = c.execute(f"SELECT COUNT(*) FROM [{t[0]}]").fetchone()[0]
    print(f"  {t[0]}: {count} rows")
c.close()
os.unlink(tmp)
