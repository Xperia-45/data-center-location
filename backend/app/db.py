import sqlite3, os, json
from pathlib import Path
DB_PATH = Path(os.getenv("DATABASE_URL_SQLITE", Path(__file__).parent.parent / "platform.db"))
def conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c
def init_db():
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sites(id TEXT PRIMARY KEY, name TEXT, lat REAL, lon REAL, radius_km REAL, footprint_ha REAL, water_mld REAL, power_mw REAL, cooling TEXT, built_up_m2 REAL, created_at TEXT);
    CREATE TABLE IF NOT EXISTS analyses(id TEXT PRIMARY KEY, site_id TEXT, payload TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS scenarios(id TEXT PRIMARY KEY, site_id TEXT, payload TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS legislation(id TEXT PRIMARY KEY, title TEXT, type TEXT, jurisdiction TEXT, authority TEXT, enacted TEXT, effective TEXT, status TEXT, source_url TEXT, source_authority TEXT, retrieved TEXT, verified TEXT, version TEXT, checksum TEXT);
    CREATE TABLE IF NOT EXISTS provisions(id TEXT PRIMARY KEY, legislation_id TEXT, section TEXT, subsection TEXT, original_text TEXT, explanation TEXT, applicability TEXT, authority TEXT);
    CREATE TABLE IF NOT EXISTS requirements(id TEXT PRIMARY KEY, legislation_id TEXT, provision_id TEXT, trigger_key TEXT, trigger_description TEXT, approval TEXT, authority TEXT, consequence_type TEXT, consequence TEXT);
    CREATE TABLE IF NOT EXISTS amendments(id TEXT PRIMARY KEY, legislation_id TEXT, amendment_date TEXT, effective TEXT, affected TEXT, document_url TEXT, status TEXT);
    """)
    # seed legal if empty
    if c.execute("select count(*) from legislation").fetchone()[0]==0:
        seed=json.loads((Path(__file__).parent/"legal_seed.json").read_text())
        for l in seed["legislation"]:
            c.execute("INSERT INTO legislation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(l[k] for k in ["id","title","type","jurisdiction","authority","enacted","effective","status","source_url","source_authority","retrieved","verified","version","checksum"]))
        for p in seed["provisions"]:
            c.execute("INSERT INTO provisions VALUES (?,?,?,?,?,?,?,?)", tuple(p[k] for k in ["id","legislation_id","section","subsection","original_text","explanation","applicability","authority"]))
        for r in seed["requirements"]:
            c.execute("INSERT INTO requirements VALUES (?,?,?,?,?,?,?,?,?)", tuple(r[k] for k in ["id","legislation_id","provision_id","trigger_key","trigger_description","approval","authority","consequence_type","consequence"]))
        for a in seed.get("amendments",[]):
            c.execute("INSERT INTO amendments VALUES (?,?,?,?,?,?,?)", tuple(a[k] for k in ["id","legislation_id","amendment_date","effective","affected","document_url","status"]))
    c.commit(); c.close()
