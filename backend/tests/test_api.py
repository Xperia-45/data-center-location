from fastapi.testclient import TestClient
from app.main import app
c=TestClient(app)
def test_health(): assert c.get("/api/v1/health").status_code==200
def test_site_validation(): assert c.post("/api/v1/sites", json={"name":"x","lat":999,"lon":0}).status_code==422
def test_legal_seed():
    laws=c.get("/api/v1/legal/legislation").json(); assert len(laws)>=5
    one=c.get(f"/api/v1/legal/legislation/{laws[0]['id']}").json(); assert "provisions" in one
    assert c.post("/api/v1/legal/search", json={"query":"consent"}).status_code==200
def test_flow():
    s=c.post("/api/v1/sites", json={"name":"Test Ludhiana","lat":30.7,"lon":76.78,"built_up_m2":30000,"water_mld":1}).json()
    assert s["id"]
    legal=c.get(f"/api/v1/sites/{s['id']}/legal").json()
    assert any(r["compliance_status"]=="APPROVAL REQUIRED" for r in legal["results"])
    sc=c.post(f"/api/v1/sites/{s['id']}/scenarios", json={"preset":"mitigation","mitigation":["recycled water"]}).json()
    assert sc["label"].startswith("Modelled")
