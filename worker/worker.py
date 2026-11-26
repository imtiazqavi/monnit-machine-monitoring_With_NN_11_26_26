import os,sys,json,time
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
import redis
from psycopg.types.json import Jsonb

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from shared.db import connect,initialize_database
from shared.engine import predict,neural_state_seconds

LOCAL_TZ=ZoneInfo(os.environ.get("LOCAL_TIMEZONE","America/Indiana/Indianapolis"));queue=redis.from_url(os.environ["REDIS_URL"],decode_responses=True);initialize_database()


def config(mid):
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT feature_name,sensor_id,measurement_label FROM config.machine_sensors WHERE machine_id=%s ORDER BY feature_name",(mid,));s=[{"feature_name":r[0],"sensor_id":r[1],"measurement_label":r[2]} for r in cur.fetchall()]
        cur.execute("SELECT state_key,state_name,metric_role,display_order FROM config.machine_states WHERE machine_id=%s ORDER BY display_order",(mid,));states=[{"state_key":r[0],"state_name":r[1],"metric_role":r[2],"display_order":r[3]} for r in cur.fetchall()]
        cur.execute("SELECT method,model_json FROM config.machine_model WHERE machine_id=%s",(mid,));m=cur.fetchone();cur.execute("SELECT current_config_version FROM config.machines WHERE machine_id=%s",(mid,));v=cur.fetchone()
    return None if not m or not states else {"sensors":s,"states":states,"method":m[0],"model_json":m[1],"version":v[0] if v else 0}


def values(cfg,event_time):
    out={}
    with connect() as conn,conn.cursor() as cur:
        for f in cfg["sensors"]:
            cur.execute("SELECT value FROM history.sensor_readings WHERE sensor_id=%s AND measurement_label=%s AND timestamp_utc<=%s ORDER BY timestamp_utc DESC LIMIT 1",(f["sensor_id"],f["measurement_label"],event_time));r=cur.fetchone();out[f["feature_name"]]=r[0] if r else None
    return out


def close_open_interval(mid,event_time):
    with connect() as conn,conn.cursor() as cur:
        cur.execute("UPDATE live.state_intervals SET ended_at_utc=%s,ended_at_local=%s WHERE machine_id=%s AND ended_at_utc IS NULL",(event_time,event_time.astimezone(LOCAL_TZ),mid));conn.commit()


def update_interval(mid,event_time,state,cfg,vals,probs,guid):
    sm={s["state_key"]:s for s in cfg["states"]};info=sm.get(state)
    if not info:return
    local=event_time.astimezone(LOCAL_TZ)
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT id,state_key FROM live.state_intervals WHERE machine_id=%s AND ended_at_utc IS NULL ORDER BY started_at_utc DESC LIMIT 1 FOR UPDATE",(mid,));openrow=cur.fetchone()
        if openrow and openrow[1]==state:return
        if openrow:cur.execute("UPDATE live.state_intervals SET ended_at_utc=%s,ended_at_local=%s WHERE id=%s",(event_time,local,openrow[0]))
        cur.execute("""INSERT INTO live.state_intervals(machine_id,state_key,state_name,metric_role,started_at_utc,started_at_local,config_version,model_method,input_json,probabilities_json,source_message_guid)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",(mid,state,info["state_name"],info["metric_role"],event_time,local,cfg["version"],cfg["method"],Jsonb(vals),Jsonb(probs) if probs else None,guid));conn.commit()


def update_neural(mid,event_time,cfg,vals,guid):
    raw,state_seconds=neural_state_seconds(vals,cfg["model_json"]);interval=float((cfg["model_json"].get("neural_model") or {}).get("interval_seconds",600));close_open_interval(mid,event_time)
    with connect() as conn,conn.cursor() as cur:
        cur.execute("""INSERT INTO live.neural_windows(machine_id,window_end_utc,window_end_local,interval_seconds,config_version,input_json,output_json,state_seconds_json,source_message_guid)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(machine_id,window_end_utc) DO UPDATE SET
                       interval_seconds=EXCLUDED.interval_seconds,config_version=EXCLUDED.config_version,input_json=EXCLUDED.input_json,output_json=EXCLUDED.output_json,
                       state_seconds_json=EXCLUDED.state_seconds_json,source_message_guid=EXCLUDED.source_message_guid""",
                    (mid,event_time,event_time.astimezone(LOCAL_TZ),interval,cfg["version"],Jsonb(vals),Jsonb(raw),Jsonb(state_seconds),guid));conn.commit()


def handle(job):
    mid=job["machine_id"];cfg=config(mid)
    if not cfg:return
    t=datetime.fromisoformat(job["timestamp_utc"]);t=t if t.tzinfo else t.replace(tzinfo=timezone.utc);vals=values(cfg,t)
    if cfg["method"]=="neural_network":update_neural(mid,t,cfg,vals,job.get("message_guid"));return
    state,probs=predict(vals,cfg["states"],cfg["method"],cfg["model_json"])
    if state:update_interval(mid,t,state,cfg,vals,probs,job.get("message_guid"))

print("Generic machine-state worker started",flush=True)
while True:
    try:
        item=queue.blpop("machine_jobs",timeout=30)
        if not item:continue
        _,raw=item
        try:handle(json.loads(raw))
        except Exception as exc:print("Job failed:",repr(exc),flush=True);queue.rpush("failed_machine_jobs",raw)
    except Exception as exc:print("Queue error:",repr(exc),flush=True);time.sleep(5)
