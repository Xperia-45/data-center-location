from ..db import conn
def evaluate(site, findings):
    triggers=set(["general_environment"])
    if site["water_mld"]>0: triggers.add("water_extraction"); triggers.add("wastewater"); triggers.add("air_emissions")
    if site["built_up_m2"]>=20000: triggers.add("eia_builtup")
    for f in findings:
        for t in f.get("trigger_keys",[]): triggers.add(t)
        if f["category"]=="Land and ecology" and f["risk"] in ("High","Critical"):
            if "wetland" in f["finding"]: triggers.add("wetland_proximity")
            if "forest" in f["finding"]: triggers.add("forest_proximity")
    c=conn(); out=[]
    for tr in triggers:
        rows=c.execute("SELECT r.*, l.title, l.source_url, l.status as leg_status, l.effective, l.verified, p.section, p.original_text, p.explanation FROM requirements r JOIN legislation l ON l.id=r.legislation_id JOIN provisions p ON p.id=r.provision_id WHERE r.trigger_key=?", (tr,)).fetchall()
        for r in rows:
            d=dict(r)
            if tr=="wetland_proximity": status="HIGH LEGAL EXPOSURE"
            elif tr in ("eia_builtup","water_extraction","wastewater","air_emissions","forest_proximity"): status="APPROVAL REQUIRED"
            else: status="UNKNOWN"
            out.append({"requirement_id":d["id"],"law":d["title"],"section":d["section"],"original_text":d["original_text"],"plain_explanation":d["explanation"],"why_applies":d["trigger_description"],"required_action":d["approval"],"authority":d["authority"],"compliance_status":status,"consequence_type":d["consequence_type"],"potential_consequences":d["consequence"],"source_url":d["source_url"],"effective_date":d["effective"],"amendment_status":d["leg_status"],"last_verified":d["verified"],"trigger":tr})
    c.close(); return out
