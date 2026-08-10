from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .db import audit, command_once, connect, new_id, utcnow

router = APIRouter(prefix="/api/sales")


def _user(request: Request) -> dict[str, Any]:
    uid=request.session.get("user_id")
    if not uid: raise HTTPException(401,"Sign in required")
    with connect() as conn:
        row=conn.execute("SELECT u.*,o.profile organization_profile FROM users u JOIN organizations o ON o.id=u.organization_id WHERE u.id=? AND u.active=1",(uid,)).fetchone()
    if not row: raise HTTPException(401,"Sign in required")
    data=dict(row)
    if data["organization_profile"]!="automotive_equipment": raise HTTPException(404)
    return data


def _write(user:dict[str,Any]) -> None:
    if user["role"] not in ("ADMIN","MANAGER","COORDINATOR","BILLING"):
        raise HTTPException(403,"Sales workflow permission required")


class OpportunityCreate(BaseModel):
    command_id:str
    customer_id:str
    location_id:str|None=None
    title:str
    expected_value_cents:int|None=None
    probability_pct:int=25
    owner_user_id:str|None=None
    next_action:str|None=None
    target_close_date:str|None=None
    source:str|None=None
    notes:str|None=None


@router.get("/pipeline")
def pipeline(request:Request):
    u=_user(request); org=u["organization_id"]
    with connect() as conn:
        opportunities=[dict(r) for r in conn.execute("""SELECT o.*,c.name customer_name,l.name location_name,u.name owner_name,
            (SELECT COUNT(*) FROM equipment_orders eo WHERE eo.opportunity_id=o.id) order_count,
            (SELECT COUNT(*) FROM installation_projects ip WHERE ip.opportunity_id=o.id) project_count
            FROM sales_opportunities o JOIN customers c ON c.id=o.customer_id LEFT JOIN locations l ON l.id=o.location_id
            LEFT JOIN users u ON u.id=o.owner_user_id WHERE o.organization_id=? ORDER BY o.updated_at DESC""",(org,)).fetchall()]
        orders=[dict(r) for r in conn.execute("""SELECT eo.*,c.name customer_name,so.title opportunity_title
            FROM equipment_orders eo JOIN customers c ON c.id=eo.customer_id LEFT JOIN sales_opportunities so ON so.id=eo.opportunity_id
            WHERE eo.organization_id=? ORDER BY eo.updated_at DESC""",(org,)).fetchall()]
        projects=[dict(r) for r in conn.execute("""SELECT ip.*,c.name customer_name,l.name location_name,u.name manager_name
            FROM installation_projects ip JOIN customers c ON c.id=ip.customer_id JOIN locations l ON l.id=ip.location_id
            LEFT JOIN users u ON u.id=ip.project_manager_user_id WHERE ip.organization_id=? ORDER BY ip.updated_at DESC""",(org,)).fetchall()]
    return {"opportunities":opportunities,"orders":orders,"projects":projects}


@router.post("/opportunities")
def create_opportunity(p:OpportunityCreate,request:Request):
    u=_user(request); _write(u); org=u["organization_id"]
    if not p.title.strip(): raise HTTPException(400,"Opportunity title is required")
    if not 0<=p.probability_pct<=100: raise HTTPException(400,"Probability must be between 0 and 100")
    with connect() as conn:
        def do():
            if not conn.execute("SELECT 1 FROM customers WHERE id=? AND organization_id=?",(p.customer_id,org)).fetchone(): raise HTTPException(404,"Customer not found")
            if p.location_id and not conn.execute("SELECT 1 FROM locations WHERE id=? AND organization_id=? AND customer_id=?",(p.location_id,org,p.customer_id)).fetchone(): raise HTTPException(400,"Location does not belong to customer")
            oid=new_id("opp"); now=utcnow()
            conn.execute("""INSERT INTO sales_opportunities(id,organization_id,customer_id,location_id,title,stage,expected_value_cents,probability_pct,owner_user_id,next_action,target_close_date,source,notes,created_at,updated_at)
                VALUES(?,?,?,?,?,'DISCOVERY',?,?,?,?,?,?,?,?,?)""",(oid,org,p.customer_id,p.location_id,p.title.strip(),p.expected_value_cents,p.probability_pct,p.owner_user_id or u["id"],p.next_action,p.target_close_date,p.source,p.notes,now,now))
            audit(conn,org,u["id"],"sales_opportunity",oid,"CREATED",p.title.strip())
            return {"id":oid,"stage":"DISCOVERY"}
        return command_once(conn,org,p.command_id,do)


class OpportunityUpdate(BaseModel):
    version:int
    stage:str|None=None
    expected_value_cents:int|None=None
    probability_pct:int|None=None
    owner_user_id:str|None=None
    next_action:str|None=None
    target_close_date:str|None=None
    notes:str|None=None


@router.patch("/opportunities/{opportunity_id}")
def update_opportunity(opportunity_id:str,p:OpportunityUpdate,request:Request):
    u=_user(request); _write(u); org=u["organization_id"]
    allowed={"DISCOVERY","SITE_SURVEY","QUOTE","CUSTOMER_REVIEW","APPROVED","ORDERED","INSTALLATION","COMMISSIONED","WON","LOST","ON_HOLD"}
    with connect() as conn:
        row=conn.execute("SELECT * FROM sales_opportunities WHERE id=? AND organization_id=?",(opportunity_id,org)).fetchone()
        if not row: raise HTTPException(404,"Opportunity not found")
        if row["version"]!=p.version: raise HTTPException(409,"This opportunity changed while you were working")
        stage=p.stage or row["stage"]
        if stage not in allowed: raise HTTPException(400,"Unknown sales stage")
        values={"stage":stage,"expected_value_cents":p.expected_value_cents if p.expected_value_cents is not None else row["expected_value_cents"],"probability_pct":p.probability_pct if p.probability_pct is not None else row["probability_pct"],"owner_user_id":p.owner_user_id if p.owner_user_id is not None else row["owner_user_id"],"next_action":p.next_action if p.next_action is not None else row["next_action"],"target_close_date":p.target_close_date if p.target_close_date is not None else row["target_close_date"],"notes":p.notes if p.notes is not None else row["notes"]}
        conn.execute("UPDATE sales_opportunities SET stage=?,expected_value_cents=?,probability_pct=?,owner_user_id=?,next_action=?,target_close_date=?,notes=?,version=version+1,updated_at=? WHERE id=?",(*values.values(),utcnow(),opportunity_id))
        audit(conn,org,u["id"],"sales_opportunity",opportunity_id,"UPDATED",f"Sales stage: {stage}",before=dict(row),after=values)
        return {"id":opportunity_id,"stage":stage,"version":p.version+1}


class LinkEstimate(BaseModel):
    command_id:str
    estimate_id:str


@router.post("/opportunities/{opportunity_id}/quote")
def link_quote(opportunity_id:str,p:LinkEstimate,request:Request):
    u=_user(request); _write(u); org=u["organization_id"]
    with connect() as conn:
        def do():
            opp=conn.execute("SELECT * FROM sales_opportunities WHERE id=? AND organization_id=?",(opportunity_id,org)).fetchone()
            est=conn.execute("SELECT * FROM estimates WHERE id=? AND organization_id=?",(p.estimate_id,org)).fetchone()
            if not opp or not est: raise HTTPException(404,"Opportunity or estimate not found")
            if est["customer_id"]!=opp["customer_id"]: raise HTTPException(400,"Estimate belongs to a different customer")
            conn.execute("UPDATE sales_opportunities SET stage='QUOTE',expected_value_cents=?,version=version+1,updated_at=? WHERE id=?",(est["total_cents"],utcnow(),opportunity_id))
            audit(conn,org,u["id"],"sales_opportunity",opportunity_id,"QUOTE_LINKED",f"Linked {est['estimate_number']} revision {est['revision']}")
            return {"opportunity_id":opportunity_id,"estimate_id":p.estimate_id,"stage":"QUOTE"}
        return command_once(conn,org,p.command_id,do)


class OrderCreate(BaseModel):
    command_id:str
    estimate_id:str|None=None
    vendor:str|None=None
    order_number:str|None=None
    expected_at:str|None=None
    total_cents:int|None=None
    notes:str|None=None


@router.post("/opportunities/{opportunity_id}/orders")
def create_order(opportunity_id:str,p:OrderCreate,request:Request):
    u=_user(request); _write(u); org=u["organization_id"]
    with connect() as conn:
        def do():
            opp=conn.execute("SELECT * FROM sales_opportunities WHERE id=? AND organization_id=?",(opportunity_id,org)).fetchone()
            if not opp: raise HTTPException(404,"Opportunity not found")
            oid=new_id("order"); now=utcnow()
            conn.execute("""INSERT INTO equipment_orders(id,organization_id,opportunity_id,estimate_id,customer_id,vendor,order_number,status,ordered_at,expected_at,total_cents,notes,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'ORDERED',?,?,?,?,?,?)""",(oid,org,opportunity_id,p.estimate_id,opp["customer_id"],p.vendor,p.order_number,now,p.expected_at,p.total_cents or opp["expected_value_cents"],p.notes,now,now))
            conn.execute("UPDATE sales_opportunities SET stage='ORDERED',version=version+1,updated_at=? WHERE id=?",(now,opportunity_id))
            audit(conn,org,u["id"],"equipment_order",oid,"CREATED",p.order_number or "Equipment order")
            return {"id":oid,"status":"ORDERED"}
        return command_once(conn,org,p.command_id,do)


class ProjectCreate(BaseModel):
    command_id:str
    location_id:str
    name:str
    planned_start:str|None=None
    planned_finish:str|None=None
    project_manager_user_id:str|None=None
    notes:str|None=None


@router.post("/orders/{order_id}/installation")
def create_installation(order_id:str,p:ProjectCreate,request:Request):
    u=_user(request); _write(u); org=u["organization_id"]
    with connect() as conn:
        def do():
            order=conn.execute("SELECT * FROM equipment_orders WHERE id=? AND organization_id=?",(order_id,org)).fetchone()
            if not order: raise HTTPException(404,"Equipment order not found")
            if not conn.execute("SELECT 1 FROM locations WHERE id=? AND organization_id=? AND customer_id=?",(p.location_id,org,order["customer_id"])).fetchone(): raise HTTPException(400,"Choose a location for this customer")
            pid=new_id("project"); now=utcnow()
            conn.execute("""INSERT INTO installation_projects(id,organization_id,opportunity_id,equipment_order_id,customer_id,location_id,name,status,planned_start,planned_finish,project_manager_user_id,notes,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'PLANNING',?,?,?,?,?,?)""",(pid,org,order["opportunity_id"],order_id,order["customer_id"],p.location_id,p.name,p.planned_start,p.planned_finish,p.project_manager_user_id or u["id"],p.notes,now,now))
            conn.execute("UPDATE equipment_orders SET status='INSTALLATION_PLANNING',version=version+1,updated_at=? WHERE id=?",(now,order_id))
            if order["opportunity_id"]: conn.execute("UPDATE sales_opportunities SET stage='INSTALLATION',version=version+1,updated_at=? WHERE id=?",(now,order["opportunity_id"]))
            audit(conn,org,u["id"],"installation_project",pid,"CREATED",p.name)
            return {"id":pid,"status":"PLANNING"}
        return command_once(conn,org,p.command_id,do)


class CommissionCreate(BaseModel):
    command_id:str
    result:str
    customer_ack_name:str|None=None
    job_id:str|None=None
    equipment_id:str|None=None
    checklist:dict[str,Any]={}


@router.post("/projects/{project_id}/commission")
def commission_project(project_id:str,p:CommissionCreate,request:Request):
    u=_user(request); _write(u); org=u["organization_id"]
    with connect() as conn:
        def do():
            project=conn.execute("SELECT * FROM installation_projects WHERE id=? AND organization_id=?",(project_id,org)).fetchone()
            if not project: raise HTTPException(404,"Installation project not found")
            cid=new_id("commission"); now=utcnow(); result=p.result.upper()
            conn.execute("INSERT INTO commissioning_records(id,organization_id,project_id,equipment_id,job_id,result,checklist_json,customer_ack_name,performed_by_user_id,performed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(cid,org,project_id,p.equipment_id,p.job_id,result,__import__('json').dumps(p.checklist),p.customer_ack_name,u["id"],now,now))
            status='COMMISSIONED' if result in ('PASS','COMMISSIONED','ACCEPTED') else 'NEEDS_ATTENTION'
            conn.execute("UPDATE installation_projects SET status=?,commissioning_status=?,actual_finish=CASE WHEN ?='COMMISSIONED' THEN ? ELSE actual_finish END,version=version+1,updated_at=? WHERE id=?",(status,status,status,now,now,project_id))
            if project["opportunity_id"] and status=='COMMISSIONED': conn.execute("UPDATE sales_opportunities SET stage='WON',probability_pct=100,version=version+1,updated_at=? WHERE id=?",(now,project["opportunity_id"]))
            audit(conn,org,u["id"],"commissioning",cid,"RECORDED",f"Commissioning result: {result}")
            return {"id":cid,"project_status":status,"lifetime_service_ready":bool(p.equipment_id and status=='COMMISSIONED')}
        return command_once(conn,org,p.command_id,do)

class InstalledEquipmentCreate(BaseModel):
    command_id: str
    category: str
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    bay: str | None = None
    install_date: str | None = None
    warranty_until: str | None = None
    notes: str | None = None


@router.post("/projects/{project_id}/installed-equipment")
def create_installed_equipment(project_id: str, p: InstalledEquipmentCreate, request: Request):
    u=_user(request); _write(u); org=u["organization_id"]
    with connect() as conn:
        def do():
            project=conn.execute("SELECT * FROM installation_projects WHERE id=? AND organization_id=?",(project_id,org)).fetchone()
            if not project: raise HTTPException(404,"Installation project not found")
            serial=(p.serial_number or "").strip() or None
            if serial and conn.execute("SELECT 1 FROM equipment WHERE organization_id=? AND lower(serial_number)=lower(?)",(org,serial)).fetchone():
                raise HTTPException(409,"That equipment serial number already exists")
            eid=new_id("equip"); now=utcnow()
            conn.execute("""INSERT INTO equipment(id,organization_id,customer_id,location_id,bay,category,manufacturer,model,serial_number,install_date,warranty_until,operational_status,notes,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,?)""",(eid,org,project["customer_id"],project["location_id"],p.bay,p.category,p.manufacturer,p.model,serial,p.install_date or now[:10],p.warranty_until,p.notes,now,now))
            audit(conn,org,u["id"],"equipment",eid,"INSTALLED_FROM_PROJECT",f"Created installed equipment from {project['name']}")
            return {"id":eid,"project_id":project_id,"lifetime_service_ready":True}
        return command_once(conn,org,p.command_id,do)

class InstallationJobCreate(BaseModel):
    command_id: str
    description: str | None = None
    due_date: str | None = None
    estimated_minutes: int = 480
    crew_min: int = 2
    qualification_required: str | None = "INSTALL"


@router.post("/projects/{project_id}/job")
def create_installation_job(project_id: str, p: InstallationJobCreate, request: Request):
    u=_user(request); _write(u); org=u["organization_id"]
    with connect() as conn:
        def do():
            project=conn.execute("SELECT * FROM installation_projects WHERE id=? AND organization_id=?",(project_id,org)).fetchone()
            if not project: raise HTTPException(404,"Installation project not found")
            count=conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE organization_id=?",(org,)).fetchone()["n"]
            from datetime import date
            job_number=f"WO-{date.today().strftime('%y')}{100+int(count)+1:03d}"
            jid=new_id("job"); now=utcnow()
            conn.execute("""INSERT INTO jobs(id,organization_id,customer_id,location_id,job_number,job_type,description,status,priority,owner_user_id,next_action,due_date,estimated_minutes,crew_min,crew_recommended,simultaneous_crew_minutes,qualification_required,parts_status,commitment_type,created_at,updated_at)
                VALUES(?,?,?,?,?,'Installation',?,'APPROVED_UNSCHEDULED','NORMAL',?,'Schedule installation',?,?,?,?,0,?,'READY','DAY_COMMITMENT',?,?)""",(jid,org,project["customer_id"],project["location_id"],job_number,p.description or project["name"],project["project_manager_user_id"] or u["id"],p.due_date,p.estimated_minutes,max(1,p.crew_min),max(1,p.crew_min),p.qualification_required,now,now))
            conn.execute("UPDATE installation_projects SET status='READY_TO_SCHEDULE',version=version+1,updated_at=? WHERE id=?",(now,project_id))
            audit(conn,org,u["id"],"installation_project",project_id,"INSTALLATION_JOB_CREATED",job_number)
            return {"job_id":jid,"job_number":job_number,"project_status":"READY_TO_SCHEDULE"}
        return command_once(conn,org,p.command_id,do)
