"""Manual legal import pipeline: python -m app.import_legal <file.json>
Validates structure, preserves original text, requires source_url/verified, dedupes by id."""
import sys, json, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import conn, init_db
def main():
    data=json.loads(Path(sys.argv[1]).read_text()); init_db(); c=conn(); added=0
    for l in data.get("legislation",[]):
        assert l.get("source_url") and l.get("verified"), "provenance required: source_url + verified"
        if c.execute("select 1 from legislation where id=?", (l["id"],)).fetchone(): continue
        c.execute("INSERT INTO legislation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(l[k] for k in ["id","title","type","jurisdiction","authority","enacted","effective","status","source_url","source_authority","retrieved","verified","version","checksum"])); added+=1
    c.commit(); print(f"imported {added} new legislation records (provisions/requirements: extend following legal_seed.json shape)")
if __name__=="__main__": main()
