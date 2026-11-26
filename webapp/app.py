import os,sys,json,re,io,hmac
from datetime import datetime,timezone,timedelta,time
from functools import wraps
from zoneinfo import ZoneInfo

import pandas as pd
from psycopg.types.json import Jsonb
import redis
from werkzeug.security import generate_password_hash,check_password_hash
from flask import Flask,request,jsonify,render_template,redirect,url_for,session,abort,send_file

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from shared.db import connect,initialize_database
from shared.auth import ensure_admin_user
from shared.metrics import METRICS,ROLES,eligible,calculate
from shared.engine import neural_raw_outputs,validate_neural_export

app=Flask(__name__)
app.secret_key=os.environ["FLASK_SECRET_KEY"]
ERP_API_KEY=os.environ["ERP_API_KEY"]
LOCAL_TZ=ZoneInfo(os.environ.get("LOCAL_TIMEZONE","America/Indiana/Indianapolis"))
queue=redis.from_url(os.environ["REDIS_URL"],decode_responses=True)
initialize_database();ensure_admin_user()


def require_login(fn):
    @wraps(fn)
    def wrapped(*a,**kw):
        if not session.get("logged_in"): return redirect(url_for("login",next=request.path))
        return fn(*a,**kw)
    return wrapped


def require_admin(fn):
    @wraps(fn)
    def wrapped(*a,**kw):
        if not session.get("logged_in"): return redirect(url_for("login",next=request.path))
        if session.get("role")!="admin": abort(403)
        return fn(*a,**kw)
    return wrapped


def parse_monnit_time(value):
    return datetime.strptime(value,"%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def get_machine(mid):
    with connect() as conn,conn.cursor() as cur:
        cur.execute("""SELECT machine_id,machine_name,machine_identifier,machine_group_number,machine_group_name,
                      shift_start,shift_end,current_config_version,updated_at FROM config.machines WHERE machine_id=%s""",(mid,));r=cur.fetchone()
    if not r:return None
    return {"machine_id":r[0],"machine_name":r[1],"identifier":r[2],"group_number":r[3] or "","group_name":r[4] or "",
            "shift_start":str(r[5]),"shift_end":str(r[6]),"config_version":r[7],"updated_at":r[8]}


def list_machines():
    with connect() as conn,conn.cursor() as cur:
        cur.execute("""
        SELECT m.machine_id,m.machine_name,m.machine_identifier,m.machine_group_number,m.machine_group_name,m.current_config_version,
               i.state_name,i.metric_role,i.started_at_local,mm.method
        FROM config.machines m
        LEFT JOIN LATERAL (SELECT state_name,metric_role,started_at_local FROM live.state_intervals x
          WHERE x.machine_id=m.machine_id AND x.ended_at_utc IS NULL ORDER BY x.started_at_utc DESC LIMIT 1) i ON TRUE
        LEFT JOIN config.machine_model mm ON mm.machine_id=m.machine_id
        WHERE m.active=TRUE ORDER BY m.machine_name""");rows=cur.fetchall()
    out=[]
    for r in rows:
        state=r[6] or ("Neural time distribution" if r[9]=="neural_network" else "No state yet")
        out.append({"machine_id":r[0],"machine_name":r[1],"identifier":r[2],"group_number":r[3],"group_name":r[4],
                    "config_version":r[5],"state_name":state,"metric_role":r[7],"state_since":r[8],"method":r[9]})
    return out


def available_measurements():
    with connect() as conn,conn.cursor() as cur:
        cur.execute("""SELECT s.sensor_id,s.sensor_name,m.label,m.label_index,m.unit,s.last_seen
                       FROM config.sensor_measurements m JOIN config.sensor_registry s ON s.sensor_id=m.sensor_id
                       WHERE s.active=TRUE ORDER BY s.sensor_name,m.label_index""");rows=cur.fetchall()
    return [{"sensor_id":r[0],"sensor_name":r[1],"label":r[2],"label_index":r[3],"unit":r[4],"last_seen":r[5]} for r in rows]


def snapshot(mid):
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT machine_name,machine_identifier,machine_group_number,machine_group_name,shift_start,shift_end,current_config_version FROM config.machines WHERE machine_id=%s",(mid,));m=cur.fetchone()
        cur.execute("SELECT feature_name,sensor_id,measurement_label,measurement_index,display_name FROM config.machine_sensors WHERE machine_id=%s ORDER BY NULLIF(regexp_replace(feature_name,'\\D','','g'),'')::int NULLS LAST,feature_name",(mid,));sensors=cur.fetchall()
        cur.execute("SELECT state_key,state_name,metric_role,display_order FROM config.machine_states WHERE machine_id=%s ORDER BY display_order",(mid,));states=cur.fetchall()
        cur.execute("SELECT method,model_json FROM config.machine_model WHERE machine_id=%s",(mid,));model=cur.fetchone()
        cur.execute("SELECT metric_key,selected,parameters_json FROM config.metric_settings WHERE machine_id=%s",(mid,));metrics=cur.fetchall()
    return {"machine":{"name":m[0],"identifier":m[1],"group_number":m[2] or "","group_name":m[3] or "","shift_start":str(m[4]),"shift_end":str(m[5]),"version":m[6]} if m else {},
      "sensors":[{"feature_name":r[0],"sensor_id":r[1],"measurement_label":r[2],"measurement_index":r[3],"display_name":r[4]} for r in sensors],
      "states":[{"state_key":r[0],"state_name":r[1],"metric_role":r[2],"display_order":r[3]} for r in states],
      "model":{"method":model[0],"model_json":model[1]} if model else None,
      "metrics":[{"metric_key":r[0],"selected":r[1],"parameters":r[2]} for r in metrics]}


def save_version(mid,user):
    snap=snapshot(mid)
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT current_config_version FROM config.machines WHERE machine_id=%s",(mid,));v=cur.fetchone()[0]+1
        cur.execute("UPDATE config.machines SET current_config_version=%s,updated_at=NOW() WHERE machine_id=%s",(v,mid))
        cur.execute("INSERT INTO config.configuration_versions(machine_id,version,snapshot_json,changed_by) VALUES(%s,%s,%s,%s)",(mid,v,Jsonb(snap),user));conn.commit()


def metric_data(mid):
    snap=snapshot(mid);roles={s["metric_role"] for s in snap["states"]};existing={m["metric_key"]:m for m in snap["metrics"]}
    method=snap.get("model",{}).get("method") if snap.get("model") else None
    out=[]
    for d in METRICS:
        params=existing.get(d["key"],{}).get("parameters",{});ok,missing=eligible(d,roles,params,method)
        out.append({**d,"eligible":ok,"missing":missing,"selected":existing.get(d["key"],{}).get("selected",False),"parameters":params})
    return out


def daily_segments(mid,day):
    sl=datetime.combine(day,time(0,0),tzinfo=LOCAL_TZ);el=sl+timedelta(days=1);su=sl.astimezone(timezone.utc);eu=el.astimezone(timezone.utc);now=datetime.now(timezone.utc)
    out=[]
    with connect() as conn,conn.cursor() as cur:
        cur.execute("""SELECT state_key,state_name,metric_role,started_at_utc,COALESCE(ended_at_utc,%s)
                       FROM live.state_intervals WHERE machine_id=%s AND started_at_utc<%s AND COALESCE(ended_at_utc,%s)>%s ORDER BY started_at_utc""",
                    (now,mid,eu,now,su))
        for key,name,role,s,e in cur.fetchall():
            s=max(s,su);e=min(e,eu,now)
            if e>s:out.append({"state_key":key,"state_name":name,"metric_role":role,"start_utc":s,"end_utc":e,"seconds":(e-s).total_seconds(),"source":"interval"})
        cur.execute("""SELECT window_end_utc,interval_seconds,state_seconds_json FROM live.neural_windows
                       WHERE machine_id=%s AND window_end_utc>%s AND window_end_utc<=%s ORDER BY window_end_utc""",(mid,su,eu))
        neural=cur.fetchall()
        cur.execute("SELECT state_key,state_name,metric_role FROM config.machine_states WHERE machine_id=%s",(mid,));state_map={r[0]:(r[1],r[2]) for r in cur.fetchall()}
    for end,interval,state_seconds in neural:
        start=end-timedelta(seconds=float(interval))
        for key,sec in (state_seconds or {}).items():
            if key in state_map and float(sec)>0:
                name,role=state_map[key];out.append({"state_key":key,"state_name":name,"metric_role":role,"start_utc":start,"end_utc":end,
                                                     "seconds":float(sec),"interval_seconds":float(interval),"source":"neural"})
    return out


def hourly_profile(segments):
    b={h:{"hour_of_day":h,"operational_minutes":0.0,"monitored_minutes":0.0,"utilization_pct":None} for h in range(24)}
    # interval source: exact placement. neural source: distribute each state's seconds proportionally over its inference window.
    grouped_neural={}
    for x in segments:
        if x.get("source")=="neural":
            k=(x["start_utc"],x["end_utc"],x.get("interval_seconds",600));grouped_neural.setdefault(k,[]).append(x);continue
        cur=x["start_utc"].astimezone(LOCAL_TZ);end=x["end_utc"].astimezone(LOCAL_TZ)
        while cur<end:
            stop=min(cur.replace(minute=0,second=0,microsecond=0)+timedelta(hours=1),end);mins=(stop-cur).total_seconds()/60;row=b[cur.hour];row["monitored_minutes"]+=mins
            if x["metric_role"]=="operational":row["operational_minutes"]+=mins
            cur=stop
    for (start,end,interval),items in grouped_neural.items():
        cur=start.astimezone(LOCAL_TZ);end_local=end.astimezone(LOCAL_TZ);opsec=sum(x["seconds"] for x in items if x["metric_role"]=="operational");frac=opsec/float(interval) if interval else 0
        while cur<end_local:
            stop=min(cur.replace(minute=0,second=0,microsecond=0)+timedelta(hours=1),end_local);mins=(stop-cur).total_seconds()/60;row=b[cur.hour];row["monitored_minutes"]+=mins;row["operational_minutes"]+=mins*frac;cur=stop
    for r in b.values():
        if r["monitored_minutes"]>0:r["utilization_pct"]=100*r["operational_minutes"]/r["monitored_minutes"]
    return list(b.values())


def refresh_live(mid,day):
    seg=daily_segments(mid,day);hourly=hourly_profile(seg);metrics=calculate(seg,hourly);selected={m["key"] for m in metric_data(mid) if m["selected"] and m["eligible"]}
    with connect() as conn,conn.cursor() as cur:
        for key in selected:
            val=metrics.get(key);text=f"{int(val):02d}:00–{(int(val)+1)%24:02d}:00" if key=="peak_usage_hour" and val is not None else None
            cur.execute("""INSERT INTO live.daily_metrics_cache(machine_id,local_date,metric_key,metric_value,text_value)
                           VALUES(%s,%s,%s,%s,%s) ON CONFLICT(machine_id,local_date,metric_key) DO UPDATE SET metric_value=EXCLUDED.metric_value,text_value=EXCLUDED.text_value,updated_at=NOW()""",
                        (mid,day,key,val if isinstance(val,(int,float)) else None,text))
        conn.commit()
    return metrics,hourly


@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        username=request.form.get("username","");password=request.form.get("password","")
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT password_hash,role,active FROM config.users WHERE username=%s",(username,));r=cur.fetchone()
        if r and r[2] and check_password_hash(r[0],password):
            session.update(logged_in=True,username=username,role=r[1]);return redirect(request.args.get("next") or url_for("home"))
        return render_template("login.html",error="Invalid username or password.")
    return render_template("login.html",error=None)

@app.get("/logout")
def logout():session.clear();return redirect(url_for("login"))

@app.get("/")
@require_login
def home():return render_template("home.html",machines=list_machines(),user=session.get("username"),role=session.get("role"))

@app.route("/admin/users",methods=["GET","POST"])
@require_admin
def users():
    message=None
    if request.method=="POST":
        action=request.form.get("action")
        if action=="add":
            u=request.form.get("username","").strip();p=request.form.get("password","")
            if u and p:
                with connect() as conn,conn.cursor() as cur:
                    cur.execute("""INSERT INTO config.users(username,password_hash,role,active) VALUES(%s,%s,'user',TRUE)
                                   ON CONFLICT(username) DO UPDATE SET password_hash=EXCLUDED.password_hash,role='user',active=TRUE,updated_at=NOW()""",
                                (u,generate_password_hash(p)));conn.commit();message=f"User {u} saved."
        elif action=="remove":
            u=request.form.get("username","")
            if u!=session.get("username"):
                with connect() as conn,conn.cursor() as cur:cur.execute("DELETE FROM config.users WHERE username=%s AND role<>'admin'",(u,));conn.commit();message=f"User {u} removed."
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT username,role,active,created_at,updated_at FROM config.users ORDER BY role,username");rows=cur.fetchall()
    return render_template("users.html",users=rows,message=message,user=session.get("username"),role=session.get("role"))

@app.get("/sensors/battery")
@require_login
def battery_health():
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT sensor_id,sensor_name,battery_level,signal_strength,voltage,last_seen,gateway_name FROM config.sensor_registry WHERE active=TRUE ORDER BY sensor_name")
        rows=cur.fetchall()
    sensors=[]
    for r in rows:
        level=r[2]
        if level is None: status="Unknown"
        elif level>=100: status="Full/External"
        elif level>=70: status="Good"
        elif level>=30: status="Watch"
        else: status="Low"
        sensors.append({"sensor_id":r[0],"sensor_name":r[1],"battery":level,"signal":r[3],"voltage":r[4],"last_seen":r[5],"gateway":r[6],"status":status})
    return render_template("battery.html",sensors=sensors,user=session.get("username"),role=session.get("role"))

@app.route("/machines/new",methods=["GET","POST"])
@require_login
def add_machine():
    if request.method=="POST":
        mid=re.sub(r"[^a-z0-9-]","-",request.form["machine_id"].lower()).strip("-")
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""INSERT INTO config.machines(machine_id,machine_name,machine_identifier,machine_group_number,machine_group_name,shift_start,shift_end)
                           VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(machine_id) DO UPDATE SET machine_name=EXCLUDED.machine_name,machine_identifier=EXCLUDED.machine_identifier,
                           machine_group_number=EXCLUDED.machine_group_number,machine_group_name=EXCLUDED.machine_group_name,shift_start=EXCLUDED.shift_start,shift_end=EXCLUDED.shift_end,updated_at=NOW()""",
                        (mid,request.form["machine_name"],request.form["machine_identifier"],request.form.get("machine_group_number"),request.form.get("machine_group_name"),request.form.get("shift_start","07:00"),request.form.get("shift_end","23:59")));conn.commit()
        return redirect(url_for("setup1",machine_id=mid))
    return render_template("new_machine.html",user=session.get("username"),role=session.get("role"))

@app.route("/machines/<machine_id>/setup1",methods=["GET","POST"])
@require_login
def setup1(machine_id):
    m=get_machine(machine_id)
    if not m:abort(404)
    if request.method=="POST":
        p=json.loads(request.form["config_json"]);sensors=p.get("sensors",[]);states=p.get("states",[]);method=p.get("method","rules");model=p.get("model_json",{})
        if method=="neural_network": validate_neural_export(model.get("neural_model") or {})
        with connect() as conn,conn.cursor() as cur:
            cur.execute("UPDATE config.machines SET machine_group_number=%s,machine_group_name=%s,updated_at=NOW() WHERE machine_id=%s",(p.get("machine_group_number"),p.get("machine_group_name"),machine_id))
            cur.execute("DELETE FROM config.machine_sensors WHERE machine_id=%s",(machine_id,))
            for x in sensors:cur.execute("INSERT INTO config.machine_sensors(machine_id,feature_name,sensor_id,measurement_label,measurement_index,display_name) VALUES(%s,%s,%s,%s,%s,%s)",(machine_id,x["feature_name"],x["sensor_id"],x["measurement_label"],int(x["measurement_index"]),x.get("display_name")))
            cur.execute("DELETE FROM config.machine_states WHERE machine_id=%s",(machine_id,))
            for i,x in enumerate(states,1):cur.execute("INSERT INTO config.machine_states(machine_id,state_key,state_name,metric_role,display_order) VALUES(%s,%s,%s,%s,%s)",(machine_id,x["state_key"],x["state_name"],x["metric_role"],i))
            cur.execute("""INSERT INTO config.machine_model(machine_id,method,model_json,updated_at) VALUES(%s,%s,%s,NOW())
                           ON CONFLICT(machine_id) DO UPDATE SET method=EXCLUDED.method,model_json=EXCLUDED.model_json,updated_at=NOW()""",(machine_id,method,Jsonb(model)));conn.commit()
        save_version(machine_id,session.get("username"));return redirect(url_for("setup2",machine_id=machine_id))
    return render_template("setup1.html",machine=m,available=available_measurements(),current=snapshot(machine_id),roles=ROLES,user=session.get("username"),role=session.get("role"))

@app.post("/machines/<machine_id>/neural-test")
@require_login
def neural_test(machine_id):
    payload=request.get_json(force=True);model=payload.get("model") or {};mapping=payload.get("mapping") or {};inputs=payload.get("inputs") or {}
    try:
        raw=neural_raw_outputs(inputs,model);mapped={}
        for y,seconds in raw.items():
            if mapping.get(y):mapped[mapping[y]]=mapped.get(mapping[y],0)+seconds
        return jsonify({"ok":True,"outputs_seconds":raw,"mapped_state_seconds":mapped,"sum_seconds":sum(raw.values())})
    except Exception as exc:return jsonify({"ok":False,"error":str(exc)}),400

@app.route("/machines/<machine_id>/setup2",methods=["GET","POST"])
@require_login
def setup2(machine_id):
    m=get_machine(machine_id)
    if not m:abort(404)
    if request.method=="POST":
        selected=set(request.form.getlist("metric_key"))
        with connect() as conn,conn.cursor() as cur:
            for d in METRICS:
                params={}
                if d["key"]=="oee_pct":params={"ideal_cycle_time_sec":request.form.get("oee_ideal_cycle_time_sec",""),"total_count_feature":request.form.get("oee_total_count_feature",""),"good_count_feature":request.form.get("oee_good_count_feature","")}
                cur.execute("""INSERT INTO config.metric_settings(machine_id,metric_key,selected,parameters_json,updated_at) VALUES(%s,%s,%s,%s,NOW())
                               ON CONFLICT(machine_id,metric_key) DO UPDATE SET selected=EXCLUDED.selected,parameters_json=EXCLUDED.parameters_json,updated_at=NOW()""",
                            (machine_id,d["key"],d["key"] in selected,Jsonb(params)));conn.commit()
        save_version(machine_id,session.get("username"));return redirect(url_for("machine_page",machine_id=machine_id))
    return render_template("setup2.html",machine=m,metrics=metric_data(machine_id),current=snapshot(machine_id),user=session.get("username"),role=session.get("role"))

@app.get("/machines/<machine_id>")
@require_login
def machine_page(machine_id):
    m=get_machine(machine_id)
    if not m:abort(404)
    today=datetime.now(LOCAL_TZ).date();metrics,hourly=refresh_live(machine_id,today)
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT state_name,metric_role,started_at_local FROM live.state_intervals WHERE machine_id=%s AND ended_at_utc IS NULL ORDER BY started_at_utc DESC LIMIT 1",(machine_id,));current_state=cur.fetchone()
        cur.execute("SELECT method FROM config.machine_model WHERE machine_id=%s",(machine_id,));methodrow=cur.fetchone();method=methodrow[0] if methodrow else None
        cur.execute("SELECT metric_key,metric_value,text_value FROM live.daily_metrics_cache WHERE machine_id=%s AND local_date=%s ORDER BY metric_key",(machine_id,today));live_metrics=cur.fetchall()
        cur.execute("""SELECT local_date,availability_pct,operational_utilization_pct,active_efficiency_pct,state_hours_json
                       FROM history.daily_machine_metrics WHERE machine_id=%s ORDER BY local_date DESC LIMIT 30""",(machine_id,));history=list(reversed(cur.fetchall()))
        cur.execute("SELECT window_end_local,state_seconds_json FROM live.neural_windows WHERE machine_id=%s ORDER BY window_end_utc DESC LIMIT 1",(machine_id,));latest_nn=cur.fetchone()
    return render_template("machine.html",machine=m,current_state=current_state,method=method,latest_nn=latest_nn,live_metrics=live_metrics,
                           state_hours=metrics.get("state_hours",{}),hourly=hourly,history=history,user=session.get("username"),role=session.get("role"))

@app.get("/history")
@require_login
def history():
    mid=request.args.get("machine_id");days=max(1,min(int(request.args.get("days","30")),1825))
    with connect() as conn,conn.cursor() as cur:
        if mid:cur.execute("SELECT * FROM history.daily_machine_metrics WHERE machine_id=%s AND local_date>=CURRENT_DATE-%s ORDER BY local_date DESC",(mid,days))
        else:cur.execute("SELECT * FROM history.daily_machine_metrics WHERE local_date>=CURRENT_DATE-%s ORDER BY local_date DESC,machine_id",(days,))
        cols=[d.name for d in cur.description];rows=[dict(zip(cols,r)) for r in cur.fetchall()]
    return render_template("history.html",rows=rows,machines=list_machines(),selected=mid,days=days,user=session.get("username"),role=session.get("role"))

@app.get("/export/history.xlsx")
@require_login
def export_history():
    mid=request.args.get("machine_id")
    with connect() as conn:
        df=pd.read_sql("SELECT * FROM history.daily_machine_metrics WHERE machine_id=%s ORDER BY local_date" if mid else "SELECT * FROM history.daily_machine_metrics ORDER BY local_date,machine_id",conn,params=(mid,) if mid else None)
    buf=io.BytesIO();
    with pd.ExcelWriter(buf,engine="openpyxl") as wr:df.to_excel(wr,index=False,sheet_name="Daily Metrics")
    buf.seek(0);return send_file(buf,as_attachment=True,download_name="machine_history.xlsx",mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.get("/api/erp/daily")
def erp_daily():
    if not hmac.compare_digest(request.headers.get("X-API-Key",""),ERP_API_KEY):return jsonify({"error":"unauthorized"}),401
    mid=request.args.get("machine_id");start=request.args.get("start","2000-01-01");end=request.args.get("end","2100-01-01")
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT * FROM history.daily_machine_metrics WHERE (%s IS NULL OR machine_id=%s) AND local_date BETWEEN %s AND %s ORDER BY local_date,machine_id",(mid,mid,start,end));cols=[d.name for d in cur.description];rows=[dict(zip(cols,r)) for r in cur.fetchall()]
    return jsonify(rows)

@app.get("/api/erp/hourly")
def erp_hourly():
    if not hmac.compare_digest(request.headers.get("X-API-Key",""),ERP_API_KEY):return jsonify({"error":"unauthorized"}),401
    mid=request.args.get("machine_id");start=request.args.get("start","2000-01-01");end=request.args.get("end","2100-01-01")
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT * FROM history.hourly_machine_usage WHERE (%s IS NULL OR machine_id=%s) AND local_date BETWEEN %s AND %s ORDER BY local_date,machine_id,hour_of_day",(mid,mid,start,end));cols=[d.name for d in cur.description];rows=[dict(zip(cols,r)) for r in cur.fetchall()]
    return jsonify(rows)

@app.get("/health")
def health():return {"status":"ok"}

@app.post("/monnit")
def monnit():
    payload=request.get_json(silent=True)
    if not payload:return jsonify({"error":"No JSON"}),400
    gateway=payload.get("gatewayMessage",{});jobs={}
    with connect() as conn:
        for msg in payload.get("sensorMessages",[]):
            sid=str(msg.get("sensorID"));sname=msg.get("sensorName") or sid;tsu=parse_monnit_time(msg["messageDate"]);tsl=tsu.astimezone(LOCAL_TZ)
            labels=(msg.get("plotLabels") or "").split("|");vals=(msg.get("plotValues") or "").split("|");units=(msg.get("dataType") or "").split("|");guid=msg.get("dataMessageGUID")
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO config.sensor_registry(sensor_id,sensor_name,application_id,network_id,gateway_id,gateway_name,last_seen,battery_level,signal_strength,voltage,active)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE) ON CONFLICT(sensor_id) DO UPDATE SET sensor_name=EXCLUDED.sensor_name,application_id=EXCLUDED.application_id,
                               network_id=EXCLUDED.network_id,gateway_id=EXCLUDED.gateway_id,gateway_name=EXCLUDED.gateway_name,last_seen=EXCLUDED.last_seen,battery_level=EXCLUDED.battery_level,
                               signal_strength=EXCLUDED.signal_strength,voltage=EXCLUDED.voltage,active=TRUE""",
                            (sid,sname,str(msg.get("applicationID","")),str(msg.get("networkID","")),str(gateway.get("gatewayID","")),gateway.get("gatewayName"),tsu,
                             float(msg["batteryLevel"]) if msg.get("batteryLevel") not in ("",None) else None,float(msg["signalStrength"]) if msg.get("signalStrength") not in ("",None) else None,
                             float(msg["voltage"]) if msg.get("voltage") not in ("",None) else None))
                for i,raw in enumerate(vals):
                    if raw in ("",None):continue
                    try:value=float(raw)
                    except Exception:continue
                    label=labels[i] if i<len(labels) and labels[i] else f"Field_{i}";unit=units[i] if i<len(units) and units[i] else None
                    cur.execute("""INSERT INTO config.sensor_measurements(sensor_id,label,label_index,unit,last_seen) VALUES(%s,%s,%s,%s,%s)
                                   ON CONFLICT(sensor_id,label,label_index) DO UPDATE SET unit=EXCLUDED.unit,last_seen=EXCLUDED.last_seen""",(sid,label,i,unit,tsu))
                    cur.execute("""INSERT INTO history.sensor_readings(timestamp_utc,timestamp_local,sensor_id,sensor_name,measurement_label,measurement_index,value,unit,message_guid,raw_message)
                                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",(tsu,tsl,sid,sname,label,i,value,unit,guid,Jsonb(msg)))
                cur.execute("SELECT DISTINCT machine_id FROM config.machine_sensors WHERE sensor_id=%s",(sid,));mids=[r[0] for r in cur.fetchall()]
            conn.commit()
            for mid in mids:jobs[(mid,tsu.isoformat())]={"machine_id":mid,"timestamp_utc":tsu.isoformat(),"sensor_id":sid,"message_guid":guid}
    for job in jobs.values():queue.rpush("machine_jobs",json.dumps(job))
    return jsonify({"status":"received","jobs_enqueued":len(jobs)}),200
