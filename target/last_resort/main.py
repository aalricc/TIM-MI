from __future__ import annotations

from pathlib import Path
from shutil import copy2

from fastapi import FastAPI, HTTPException

app = FastAPI(title="MI Last Resort")

BACKUPS_DIR = Path("/target/backups")
DB_FILE = Path("/target/app/data/app.db")


@app.post("/restore")
def restore() -> dict[str, str]:
    backups = sorted(BACKUPS_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime)
    if not backups:
        raise HTTPException(status_code=500, detail="no backups available")
    source = backups[-1]
    if source.stat().st_size == 0:
        raise HTTPException(status_code=500, detail="backup corrupt")
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    copy2(source, DB_FILE)
    return {"backup_path": str(source)}
