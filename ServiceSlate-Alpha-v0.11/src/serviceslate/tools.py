from __future__ import annotations

import csv
import io
import math
import html
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .db import connect, new_id, utcnow

router = APIRouter(prefix="/api/tools")


def _user(request: Request) -> dict[str, Any]:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Sign in required")
    with connect() as conn:
        row = conn.execute(
            """SELECT u.*,o.profile organization_profile,o.name organization_name
               FROM users u JOIN organizations o ON o.id=u.organization_id
               WHERE u.id=? AND u.active=1""",
            (uid,),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Sign in required")
    return dict(row)


Field = dict[str, Any]


def f(key: str, label: str, type_: str = "number", **kw: Any) -> Field:
    return {"key": key, "label": label, "type": type_, **kw}


TOOLS: list[dict[str, Any]] = [
    {"id":"inch_fraction","name":"Decimal ↔ Fractional Inch","category":"Field Math","description":"Convert shop measurements between decimal inches and practical fractions.","profiles":["automotive_equipment"],"fields":[f("value","Value"),f("mode","Convert","select",options=["decimal_to_fraction","fraction_to_decimal"],default="decimal_to_fraction"),f("denominator","Nearest denominator","select",options=[8,16,32,64],default=64)]},
    {"id":"unit_length","name":"Length Converter","category":"Field Math","description":"Inches, feet, millimeters, centimeters and meters.","fields":[f("value","Value"),f("from_unit","From","select",options=["in","ft","mm","cm","m"],default="in"),f("to_unit","To","select",options=["in","ft","mm","cm","m"],default="mm")]},
    {"id":"unit_pressure","name":"Pressure Converter","category":"Field Math","description":"PSI, bar, kPa and MPa without a web lookup.","profiles":["automotive_equipment"],"fields":[f("value","Value"),f("from_unit","From","select",options=["psi","bar","kPa","MPa"],default="psi"),f("to_unit","To","select",options=["psi","bar","kPa","MPa"],default="bar")]},
    {"id":"unit_torque","name":"Torque Converter","category":"Field Math","description":"ft-lb, in-lb and N·m reference conversion.","profiles":["automotive_equipment"],"fields":[f("value","Value"),f("from_unit","From","select",options=["ft-lb","in-lb","N-m"],default="ft-lb"),f("to_unit","To","select",options=["ft-lb","in-lb","N-m"],default="N-m")]},
    {"id":"unit_temperature","name":"Temperature Converter","category":"Field Math","description":"Fahrenheit, Celsius and Kelvin.","fields":[f("value","Value"),f("from_unit","From","select",options=["F","C","K"],default="F"),f("to_unit","To","select",options=["F","C","K"],default="C")]},
    {"id":"unit_mass","name":"Weight / Mass Converter","category":"Field Math","description":"Pounds, ounces, kilograms and grams.","fields":[f("value","Value"),f("from_unit","From","select",options=["lb","oz","kg","g"],default="lb"),f("to_unit","To","select",options=["lb","oz","kg","g"],default="kg")]},
    {"id":"unit_volume","name":"Volume Converter","category":"Field Math","description":"Gallons, quarts, liters and milliliters.","fields":[f("value","Value"),f("from_unit","From","select",options=["gal","qt","L","mL"],default="gal"),f("to_unit","To","select",options=["gal","qt","L","mL"],default="L")]},
    {"id":"ohms_law","name":"Ohm's Law","category":"Field Math","description":"Solve voltage, current, resistance and electrical power from any two known values.","profiles":["automotive_equipment"],"fields":[f("volts","Volts (V)",required=False),f("amps","Current (A)",required=False),f("ohms","Resistance (Ω)",required=False),f("watts","Power (W)",required=False)]},
    {"id":"hydraulic_force","name":"Hydraulic Cylinder Force","category":"Field Math","description":"Reference cylinder force from pressure and bore; retract force can include rod diameter.","profiles":["automotive_equipment"],"fields":[f("pressure_psi","Pressure (PSI)"),f("bore_in","Bore diameter (in)"),f("rod_in","Rod diameter (in)",required=False,default=0)]},
    {"id":"crew_labor","name":"Crew Labor Calculator","category":"Planning","description":"Compare elapsed job time with total labor-hours for one or more technicians.","profiles":["automotive_equipment"],"fields":[f("elapsed_minutes","Elapsed minutes"),f("crew_count","Crew count",default=1),f("shared_minutes","Minutes all crew work together",required=False,default=0)]},
    {"id":"markup_margin","name":"Markup / Margin","category":"Office Math","description":"Translate cost, sell price, markup and gross margin without spreadsheet formulas.","fields":[f("cost","Cost ($)"),f("sell","Sell price ($)",required=False),f("markup_percent","Markup %",required=False),f("margin_percent","Margin %",required=False)]},
    {"id":"estimate_math","name":"Estimate Math","category":"Office Math","description":"Subtotal, discount and tax reference math. Does not create an estimate until you choose to.","fields":[f("subtotal","Subtotal ($)"),f("discount_percent","Discount %",required=False,default=0),f("tax_percent","Tax %",required=False,default=0)]},
    {"id":"due_date","name":"Due-Date Calculator","category":"Planning","description":"Add calendar days, business days, weeks or months to a starting date.","fields":[f("start_date","Start date","date",default="today"),f("amount","Amount",default=1),f("unit","Unit","select",options=["calendar_days","business_days","weeks","months"],default="calendar_days")]},
    {"id":"coordinate_distance","name":"Planning Distance","category":"Planning","description":"Straight-line distance between two supplied planning coordinates. Never reads device or vehicle GPS.","profiles":["automotive_equipment"],"fields":[f("lat1","Point A latitude"),f("lon1","Point A longitude"),f("lat2","Point B latitude"),f("lon2","Point B longitude")]},
    {"id":"map_link","name":"Open Address in Maps","category":"Handoffs","description":"Create a normal map search link without a paid map API or tracking integration.","fields":[f("address","Address","text"),f("provider","Map provider","select",options=["Google Maps","Bing Maps","OpenStreetMap"],default="Google Maps")]},
    {"id":"email_link","name":"Draft in Email App","category":"Handoffs","description":"Open the device's normal email composer. ServiceSlate does not claim the message was sent.","fields":[f("to","To","text",required=False),f("subject","Subject","text",required=False),f("body","Message","textarea",required=False)]},
    {"id":"sms_link","name":"Draft Text Message","category":"Handoffs","description":"Open the device's SMS composer. ServiceSlate does not claim delivery.","fields":[f("phone","Phone","text"),f("body","Message","textarea",required=False)]},
    {"id":"phone_link","name":"Call Phone","category":"Handoffs","description":"Hand a phone number to the device dialer; no call recording or tracking.","fields":[f("phone","Phone","text")]},
    {"id":"calendar_link","name":"Calendar Event File","category":"Handoffs","description":"Create a standard .ics calendar file for Outlook, Apple Calendar, Google Calendar and others.","fields":[f("title","Title","text"),f("start_at","Start","datetime-local"),f("end_at","End","datetime-local"),f("location","Location","text",required=False),f("description","Description","textarea",required=False)]},
    {"id":"vcard","name":"Contact Card File","category":"Handoffs","description":"Create a standard .vcf contact file for phone/address-book import.","fields":[f("name","Name","text"),f("company","Company","text",required=False),f("phone","Phone","text",required=False),f("email","Email","text",required=False),f("address","Address","text",required=False)]},
    {"id":"voice_note","name":"Voice Note Pad","category":"Device Tools","description":"Use browser speech recognition when available, then copy the note into any ServiceSlate field. Nothing is sent to ServiceSlate until you choose to save it.","client_tool":True,"fields":[]},
    {"id":"barcode_scan","name":"Barcode / QR Reader","category":"Device Tools","description":"Read supported barcodes from a camera photo using the browser when available. No GPS or continuous camera tracking.","client_tool":True,"profiles":["automotive_equipment"],"fields":[]},
    {"id":"photo_optimizer","name":"Photo Optimizer","category":"Device Tools","description":"Resize a large field photo locally in the browser before attaching or sharing it. The image stays on the device unless you upload it.","client_tool":True,"fields":[]},
    {"id":"share_text","name":"Device Share Sheet","category":"Device Tools","description":"Hand text to the phone or computer share sheet when the browser supports it.","client_tool":True,"fields":[]},
    {"id":"customer_csv_template","name":"Customer Import Template","category":"Data & Recovery","description":"Download a clean CSV template for bringing customer data into ServiceSlate.","roles":["ADMIN","MANAGER","COORDINATOR","RECEPTION"],"fields":[]},
    {"id":"history_csv_template","name":"Legacy Work-History Template","category":"Data & Recovery","description":"Download the recommended FastField/NoteWise history-import columns.","profiles":["automotive_equipment"],"roles":["ADMIN","MANAGER","COORDINATOR"],"fields":[]},
    {"id":"quickbooks_handoff","name":"QuickBooks Handoff CSV","category":"Data & Recovery","description":"Export billing-ready operational work as a neutral CSV. It does not write to QuickBooks.","profiles":["automotive_equipment"],"roles":["ADMIN","MANAGER","COORDINATOR","BILLING"],"fields":[]},
    {"id":"work_order_print","name":"Printable Work Order","category":"Documents","description":"Open a clean printable work-order/history page from a work-order number.","profiles":["automotive_equipment"],"fields":[f("job_number","Work-order number","text")]},
    {"id":"equipment_print","name":"Printable Equipment Record","category":"Documents","description":"Open a clean printable equipment history from a serial number.","profiles":["automotive_equipment"],"fields":[f("serial","Serial number","text")]},
]


class RunTool(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


def _visible(tool: dict[str, Any], user: dict[str, Any]) -> bool:
    if tool.get("profiles") and user["organization_profile"] not in tool["profiles"]:
        return False
    if tool.get("roles") and user["role"] not in tool["roles"]:
        return False
    return True


@router.get("")
def list_tools(request: Request):
    user = _user(request)
    return [{**t, "availability":"READY", "local_first":True} for t in TOOLS if _visible(t, user)]


def _num(inp: dict[str, Any], key: str, *, required: bool = True) -> float | None:
    value = inp.get(key)
    if value in (None, ""):
        if required:
            raise HTTPException(400, f"Enter {key.replace('_',' ')}")
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{key.replace('_',' ').title()} must be a number") from None


def _round(value: float, digits: int = 4) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1." + "0" * digits), rounding=ROUND_HALF_UP))


def _conversion(inp: dict[str, Any], factors: dict[str, float], label: str) -> dict[str, Any]:
    value = _num(inp, "value")
    a, b = str(inp.get("from_unit")), str(inp.get("to_unit"))
    if a not in factors or b not in factors:
        raise HTTPException(400, "Choose supported units")
    result = value * factors[a] / factors[b]
    return {"headline": f"{_round(result, 6):g} {b}", "details": [f"{value:g} {a} = {_round(result, 6):g} {b}"], "copy_value": f"{_round(result, 6):g}"}


def _month_add(d: date, months: int) -> date:
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(d.day, days[month - 1]))


def _tool_result(tool_id: str, inp: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if tool_id == "inch_fraction":
        mode = inp.get("mode", "decimal_to_fraction")
        if mode == "fraction_to_decimal":
            raw = str(inp.get("value", "")).strip().replace("-", " ")
            try:
                parts = raw.split()
                if "/" in raw and len(parts) > 1:
                    whole = float(parts[0])
                    fraction = float(Fraction(parts[1]))
                    value = whole - fraction if whole < 0 else whole + fraction
                else:
                    value = float(Fraction(raw)) if "/" in raw else float(raw)
            except Exception:
                raise HTTPException(400, "Use a value like 8 3/8 or 8.375") from None
            return {"headline": f"{value:g} in", "details":[f"{raw} in = {value:g} decimal inches"],"copy_value":f"{value:g}"}
        value = _num(inp, "value"); denominator = int(inp.get("denominator") or 64)
        whole = math.floor(value) if value >= 0 else math.ceil(value)
        frac = Fraction(abs(value - whole)).limit_denominator(denominator)
        if frac.numerator == 0: text = f"{whole} in"
        elif whole == 0: text = f"{'-' if value < 0 else ''}{frac.numerator}/{frac.denominator} in"
        else: text = f"{whole} {frac.numerator}/{frac.denominator} in"
        return {"headline":text,"details":[f"{value:g} decimal inches ≈ {text}", f"Reduced to denominator ≤ {denominator}."],"copy_value":text.replace(" in","")}
    if tool_id == "unit_length": return _conversion(inp,{"in":1,"ft":12,"mm":1/25.4,"cm":10/25.4,"m":1000/25.4},"length")
    if tool_id == "unit_pressure": return _conversion(inp,{"psi":1,"bar":14.5037738,"kPa":0.145037738,"MPa":145.037738},"pressure")
    if tool_id == "unit_torque": return _conversion(inp,{"ft-lb":1,"in-lb":1/12,"N-m":0.737562149},"torque")
    if tool_id == "unit_mass": return _conversion(inp,{"lb":1,"oz":1/16,"kg":2.2046226218,"g":0.0022046226},"mass")
    if tool_id == "unit_volume": return _conversion(inp,{"gal":1,"qt":0.25,"L":0.2641720524,"mL":0.0002641720524},"volume")
    if tool_id == "unit_temperature":
        v=_num(inp,"value"); a,b=inp.get("from_unit"),inp.get("to_unit")
        if a not in ("F","C","K") or b not in ("F","C","K"): raise HTTPException(400,"Choose supported units")
        c=(v-32)*5/9 if a=="F" else v-273.15 if a=="K" else v
        result=c*9/5+32 if b=="F" else c+273.15 if b=="K" else c
        return {"headline":f"{_round(result,3):g} °{b}","details":[f"{v:g} °{a} = {_round(result,3):g} °{b}"],"copy_value":f"{_round(result,3):g}"}
    if tool_id == "ohms_law":
        vals={k:_num(inp,k,required=False) for k in ("volts","amps","ohms","watts")}; known={k:v for k,v in vals.items() if v is not None}
        if len(known)<2: raise HTTPException(400,"Enter at least two known electrical values")
        V,I,R,P=vals["volts"],vals["amps"],vals["ohms"],vals["watts"]
        try:
            if V is not None and I is not None: R=R if R is not None else V/I; P=P if P is not None else V*I
            elif V is not None and R is not None: I=I if I is not None else V/R; P=P if P is not None else V*V/R
            elif I is not None and R is not None: V=V if V is not None else I*R; P=P if P is not None else I*I*R
            elif P is not None and V is not None: I=I if I is not None else P/V; R=R if R is not None else V*V/P
            elif P is not None and I is not None: V=V if V is not None else P/I; R=R if R is not None else P/(I*I)
            elif P is not None and R is not None: I=I if I is not None else math.sqrt(P/R); V=V if V is not None else math.sqrt(P*R)
        except (ZeroDivisionError, ValueError): raise HTTPException(400,"Values must produce a valid non-zero calculation") from None
        return {"headline":f"{_round(V,3):g} V · {_round(I,3):g} A","details":[f"Resistance: {_round(R,4):g} Ω",f"Power: {_round(P,3):g} W","Reference math only — verify equipment procedure and ratings."]}
    if tool_id == "hydraulic_force":
        psi=_num(inp,"pressure_psi"); bore=_num(inp,"bore_in"); rod=_num(inp,"rod_in",required=False) or 0
        if psi<0 or bore<=0 or rod<0 or rod>=bore: raise HTTPException(400,"Use a positive bore and a rod diameter smaller than the bore")
        area=math.pi*(bore/2)**2; extend=psi*area; retract_area=area-math.pi*(rod/2)**2; retract=psi*retract_area
        return {"headline":f"{extend:,.0f} lbf extend","details":[f"Piston area: {_round(area,4):g} in²",f"Retract force: {retract:,.0f} lbf" if rod else "Enter rod diameter to calculate retract force.","Reference math only — use manufacturer-rated pressures and procedures."]}
    if tool_id == "crew_labor":
        elapsed=_num(inp,"elapsed_minutes"); crew=int(_num(inp,"crew_count")); shared=_num(inp,"shared_minutes",required=False) or 0
        if crew<1 or shared<0 or shared>elapsed: raise HTTPException(400,"Crew must be at least 1 and shared minutes cannot exceed elapsed time")
        labor=shared*crew+(elapsed-shared)
        return {"headline":f"{labor/60:.2f} labor-hours","details":[f"Elapsed job time: {elapsed/60:.2f} hours",f"Shared crew portion: {shared:g} min × {crew} techs",f"Solo remainder: {elapsed-shared:g} min"]}
    if tool_id == "markup_margin":
        cost=_num(inp,"cost"); sell=_num(inp,"sell",required=False); markup=_num(inp,"markup_percent",required=False); margin=_num(inp,"margin_percent",required=False)
        if cost<0: raise HTTPException(400,"Cost cannot be negative")
        if sell is None:
            if markup is not None: sell=cost*(1+markup/100)
            elif margin is not None:
                if margin>=100: raise HTTPException(400,"Margin must be below 100%")
                sell=cost/(1-margin/100)
            else: raise HTTPException(400,"Enter sell price, markup %, or margin %")
        if sell<=0: raise HTTPException(400,"Sell price must be positive")
        markup_calc=(sell-cost)/cost*100 if cost else 0; margin_calc=(sell-cost)/sell*100
        return {"headline":f"Sell ${sell:,.2f}","details":[f"Gross profit: ${sell-cost:,.2f}",f"Markup: {markup_calc:.2f}%",f"Margin: {margin_calc:.2f}%"]}
    if tool_id == "estimate_math":
        subtotal=_num(inp,"subtotal"); discount=_num(inp,"discount_percent",required=False) or 0; tax=_num(inp,"tax_percent",required=False) or 0
        discounted=subtotal*(1-discount/100); tax_amt=discounted*tax/100; total=discounted+tax_amt
        return {"headline":f"${total:,.2f} total","details":[f"After discount: ${discounted:,.2f}",f"Tax reference: ${tax_amt:,.2f}","Reference math only; actual tax/accounting rules remain external."]}
    if tool_id == "due_date":
        raw=inp.get("start_date") or "today"; d=date.today() if raw=="today" else date.fromisoformat(str(raw)[:10]); amount=int(_num(inp,"amount")); unit=inp.get("unit")
        if unit=="calendar_days": result=d+timedelta(days=amount)
        elif unit=="weeks": result=d+timedelta(weeks=amount)
        elif unit=="months": result=_month_add(d,amount)
        elif unit=="business_days":
            result=d; step=1 if amount>=0 else -1
            for _ in range(abs(amount)):
                result+=timedelta(days=step)
                while result.weekday()>=5: result+=timedelta(days=step)
        else: raise HTTPException(400,"Choose a supported date unit")
        return {"headline":f"{result.strftime('%b')} {result.day}, {result.year}","details":[f"Start: {d.isoformat()}",f"{amount} {str(unit).replace('_',' ')} → {result.isoformat()}"],"copy_value":result.isoformat()}
    if tool_id == "coordinate_distance":
        lat1,lon1,lat2,lon2=(_num(inp,k) for k in ("lat1","lon1","lat2","lon2"))
        if not all((-90<=x<=90) for x in (lat1,lat2)) or not all((-180<=x<=180) for x in (lon1,lon2)): raise HTTPException(400,"Coordinates are outside valid latitude/longitude ranges")
        r=3958.7613; p1,p2=math.radians(lat1),math.radians(lat2); dp=math.radians(lat2-lat1); dl=math.radians(lon2-lon1)
        a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2; miles=r*2*math.atan2(math.sqrt(a),math.sqrt(1-a))
        return {"headline":f"{miles:.1f} mi straight-line","details":[f"{miles*1.609344:.1f} km", "Planning estimate only — not route distance and not GPS tracking."]}
    if tool_id == "map_link":
        address=str(inp.get("address") or "").strip(); provider=inp.get("provider","Google Maps")
        if not address: raise HTTPException(400,"Enter an address")
        urls={"Google Maps":f"https://www.google.com/maps/search/?api=1&query={quote(address)}","Bing Maps":f"https://www.bing.com/maps?q={quote(address)}","OpenStreetMap":f"https://www.openstreetmap.org/search?query={quote(address)}"}
        return {"headline":"Map link ready","details":[address],"action":{"label":f"Open {provider}","url":urls.get(provider,urls["Google Maps"])}}
    if tool_id == "email_link":
        params={k:str(inp.get(k) or "") for k in ("subject","body")}; url=f"mailto:{quote(str(inp.get('to') or ''))}?{urlencode(params)}"
        return {"headline":"Email draft ready","details":["Opening the email app does not mark this message sent in ServiceSlate."],"action":{"label":"Open Email App","url":url}}
    if tool_id == "sms_link":
        phone=str(inp.get("phone") or "").strip(); body=str(inp.get("body") or "");
        if not phone: raise HTTPException(400,"Enter a phone number")
        return {"headline":"Text draft ready","details":["Delivery remains outside ServiceSlate until a messaging provider is connected."],"action":{"label":"Open Messages","url":f"sms:{quote(phone)}?body={quote(body)}"}}
    if tool_id == "phone_link":
        phone=str(inp.get("phone") or "").strip();
        if not phone: raise HTTPException(400,"Enter a phone number")
        return {"headline":phone,"details":["ServiceSlate does not record or monitor the call."],"action":{"label":"Open Dialer","url":f"tel:{quote(phone)}"}}
    if tool_id in {"calendar_link","vcard","customer_csv_template","history_csv_template","quickbooks_handoff","work_order_print","equipment_print"}:
        return {"download":True}
    raise HTTPException(404,"Tool not found")


@router.post("/{tool_id}/run")
def run_tool(tool_id: str, payload: RunTool, request: Request):
    user=_user(request); tool=next((t for t in TOOLS if t["id"]==tool_id),None)
    if not tool or not _visible(tool,user): raise HTTPException(404,"Tool not available")
    if tool.get("client_tool"):
        raise HTTPException(400, "This tool runs directly on the device")
    result=_tool_result(tool_id,payload.inputs,user)
    if result.get("download"):
        query=urlencode({k:str(v) for k,v in payload.inputs.items() if v not in (None,"")})
        result={"headline":"Ready","details":["Open or download the generated file."],"action":{"label":"Open / Download","url":f"/api/tools/{tool_id}/download"+(f"?{query}" if query else "")}}
    with connect() as conn:
        conn.execute("INSERT INTO tool_usage(id,organization_id,user_id,tool_id,created_at) VALUES(?,?,?,?,?)",(new_id("tooluse"),user["organization_id"],user["id"],tool_id,utcnow()))
    return result


def _escape_ics(v: str) -> str:
    return v.replace("\\","\\\\").replace("\n","\\n").replace(",","\\,").replace(";","\\;")


def _dt_ics(raw: str) -> str:
    try: return datetime.fromisoformat(raw).strftime("%Y%m%dT%H%M%S")
    except ValueError: raise HTTPException(400,"Enter a valid start and end time") from None


@router.get("/{tool_id}/download")
def download_tool(tool_id: str, request: Request):
    user=_user(request); tool=next((t for t in TOOLS if t["id"]==tool_id),None)
    if not tool or not _visible(tool,user): raise HTTPException(404,"Tool not available")
    q=request.query_params
    if tool_id=="calendar_link":
        title=q.get("title") or "ServiceSlate Event"; start=q.get("start_at"); end=q.get("end_at")
        if not start or not end: raise HTTPException(400,"Start and end are required")
        ics="\r\n".join(["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//ServiceSlate//EN","BEGIN:VEVENT",f"UID:{new_id('event')}@serviceslate.local",f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",f"DTSTART:{_dt_ics(start)}",f"DTEND:{_dt_ics(end)}",f"SUMMARY:{_escape_ics(title)}",f"LOCATION:{_escape_ics(q.get('location') or '')}",f"DESCRIPTION:{_escape_ics(q.get('description') or '')}","END:VEVENT","END:VCALENDAR",""])
        return PlainTextResponse(ics,media_type="text/calendar",headers={"Content-Disposition":"attachment; filename=ServiceSlate-Event.ics"})
    if tool_id=="vcard":
        name=q.get("name") or "ServiceSlate Contact"; lines=["BEGIN:VCARD","VERSION:3.0",f"FN:{_vcard_safe(name)}"]
        if q.get("company"): lines.append(f"ORG:{_vcard_safe(q['company'])}")
        if q.get("phone"): lines.append(f"TEL:{_vcard_safe(q['phone'])}")
        if q.get("email"): lines.append(f"EMAIL:{_vcard_safe(q['email'])}")
        if q.get("address"): lines.append(f"ADR:;;{_vcard_safe(q['address'])};;;;")
        lines.extend(["END:VCARD",""])
        return PlainTextResponse("\r\n".join(lines),media_type="text/vcard",headers={"Content-Disposition":"attachment; filename=ServiceSlate-Contact.vcf"})
    if tool_id in {"customer_csv_template","history_csv_template"}:
        out=io.StringIO(); w=csv.writer(out)
        if tool_id=="customer_csv_template": w.writerow(["customer_name","phone","email","address1","city","state","postal_code","notes"])
        else: w.writerow(["customer","work_order","date","serial","technician","complaint","finding","cause","correction","verification"])
        return StreamingResponse(iter([out.getvalue()]),media_type="text/csv",headers={"Content-Disposition":f"attachment; filename={tool_id}.csv"})
    if tool_id=="quickbooks_handoff":
        out=io.StringIO(); w=csv.writer(out); w.writerow(["work_order","customer","location","completed_date","purchase_order","estimate_total","status"])
        with connect() as conn:
            rows=conn.execute("""SELECT j.job_number,c.name customer_name,l.name location_name,j.purchase_order,j.status,
                    MAX(ws.performed_at) completed_date,COALESCE(MAX(e.total_cents),0) total_cents
                    FROM jobs j JOIN customers c ON c.id=j.customer_id JOIN locations l ON l.id=j.location_id
                    LEFT JOIN work_submissions ws ON ws.job_id=j.id AND ws.state IN ('APPROVED','LOCKED')
                    LEFT JOIN estimates e ON e.job_id=j.id AND e.status='APPROVED'
                    WHERE j.organization_id=? AND j.status IN ('READY_TO_INVOICE','WORK_COMPLETE','INVOICED','CLOSED')
                    GROUP BY j.id ORDER BY j.job_number""",(user["organization_id"],)).fetchall()
        for r in rows: w.writerow([_csv_safe(r["job_number"]),_csv_safe(r["customer_name"]),_csv_safe(r["location_name"]),_csv_safe(r["completed_date"]),_csv_safe(r["purchase_order"]),f"{r['total_cents']/100:.2f}",_csv_safe(r["status"])])
        return StreamingResponse(iter([out.getvalue()]),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=ServiceSlate-QuickBooks-Handoff.csv"})
    if tool_id=="work_order_print":
        number=(q.get("job_number") or "").strip()
        with connect() as conn:
            row=conn.execute("""SELECT j.*,c.name customer_name,l.name location_name,l.address1,l.city,l.state,l.postal_code,
                e.manufacturer,e.model,e.serial_number FROM jobs j JOIN customers c ON c.id=j.customer_id JOIN locations l ON l.id=j.location_id
                LEFT JOIN equipment e ON e.id=j.equipment_id WHERE j.organization_id=? AND lower(j.job_number)=lower(?)""",(user["organization_id"],number)).fetchone()
            if not row: raise HTTPException(404,"Work order not found")
            hist=conn.execute("SELECT * FROM work_submissions WHERE organization_id=? AND job_id=? AND state IN ('APPROVED','LOCKED') ORDER BY COALESCE(performed_at,submitted_at,created_at)",(user["organization_id"],row["id"])).fetchall()
        h="".join(f"<section><h3>{_html(x['performed_at'] or x['submitted_at'] or '')}</h3><p><b>Finding:</b> {_html(x['finding'] or '—')}<br><b>Correction:</b> {_html(x['correction'] or '—')}<br><b>Verification:</b> {_html(x['verification'] or '—')}</p></section>" for x in hist)
        equipment_text=" ".join(str(x) for x in [row["manufacturer"],row["model"],row["serial_number"]] if x) or "—"
        return HTMLResponse(_print_page(f"Work Order {row['job_number']}",f"<h1>{_html(row['job_number'])}</h1><h2>{_html(row['customer_name'])} · {_html(row['location_name'])}</h2><p>{_html(row['description'])}</p><p><b>Equipment:</b> {_html(equipment_text)}</p>{h}"))
    if tool_id=="equipment_print":
        serial=(q.get("serial") or "").strip()
        with connect() as conn:
            e=conn.execute("""SELECT e.*,c.name customer_name,l.name location_name FROM equipment e JOIN customers c ON c.id=e.customer_id JOIN locations l ON l.id=e.location_id
                WHERE e.organization_id=? AND lower(e.serial_number)=lower(?)""",(user["organization_id"],serial)).fetchone()
            if not e: raise HTTPException(404,"Equipment not found")
            hist=conn.execute("""SELECT ws.*,j.job_number,j.description FROM work_submissions ws JOIN jobs j ON j.id=ws.job_id
                WHERE ws.organization_id=? AND j.equipment_id=? AND ws.state IN ('APPROVED','LOCKED') ORDER BY COALESCE(ws.performed_at,ws.submitted_at,ws.created_at) DESC""",(user["organization_id"],e["id"])).fetchall()
        h="".join(f"<section><h3>{_html(x['job_number'])} · {_html(x['description'])}</h3><p><b>Finding:</b> {_html(x['finding'] or '—')}<br><b>Correction:</b> {_html(x['correction'] or '—')}<br><b>Verification:</b> {_html(x['verification'] or '—')}</p></section>" for x in hist)
        return HTMLResponse(_print_page(f"Equipment {serial}",f"<h1>{_html(e['manufacturer'])} {_html(e['model'] or e['category'])}</h1><h2>{_html(serial)}</h2><p>{_html(e['customer_name'])} · {_html(e['location_name'])} · {_html(e['bay'])}</p>{h}"))
    raise HTTPException(404,"Download tool not found")


def _html(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _csv_safe(value: Any) -> str:
    text = str(value or "")
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _vcard_safe(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").replace(";", "\\;").replace(",", "\\,")


def _print_page(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>{_html(title)}</title><style>body{{font:14px/1.45 system-ui;margin:36px;color:#172027}}h1{{margin-bottom:4px}}h2{{font-size:16px;color:#52616d}}section{{border-top:1px solid #ccd3d8;padding:14px 0}}@media print{{button{{display:none}}}}</style></head><body><button onclick='print()'>Print / Save PDF</button>{body}</body></html>"""
