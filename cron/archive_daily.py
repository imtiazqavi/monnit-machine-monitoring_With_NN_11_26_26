import os,sys
from datetime import datetime,timedelta,time,timezone
from zoneinfo import ZoneInfo
from psycopg.types.json import Jsonb

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from shared.db import connect,initialize_database
from shared.metrics import calculate

LOCAL_TZ=ZoneInfo(os.environ.get("LOCAL_TIMEZONE","America/Indiana/Indianapolis"));RAW_RETENTION=int(os.environ.get("RAW_SENSOR_RETENTION_DAYS","90"));HIST_RETENTION=int(os.environ.get("HISTORY_RETENTION_DAYS","1825"));initialize_database()


def segments(mid,day):
    sl=datetime.combine(day,time(0,0),tzinfo=LOCAL_TZ);el=sl+timedelta(days=1);su=sl.astimezone(timezone.utc);eu=el.astimezone(timezone.utc);out=[]
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT id,state_key,state_name,metric_role,started_at_utc,started_at_local,config_version,model_method,input_json,probabilities_json FROM live.state_intervals WHERE machine_id=%s AND ended_at_utc IS NULL AND started_at_utc<%s FOR UPDATE",(mid,eu))
        for r in cur.fetchall():
            if r[4]<eu:
                cur.execute("UPDATE live.state_intervals SET ended_at_utc=%s,ended_at_local=%s WHERE id=%s",(eu,el,r[0]));cur.execute("""INSERT INTO live.state_intervals(machine_id,state_key,state_name,metric_role,started_at_utc,started_at_local,config_version,model_method,input_json,probabilities_json)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",(mid,r[1],r[2],r[3],eu,el,r[6],r[7],Jsonb(r[8]),Jsonb(r[9]) if r[9] else None))
        conn.commit()
    with connect() as conn,conn.cursor() as cur:
        cur.execute("""SELECT state_key,state_name,metric_role,started_at_utc,started_at_local,ended_at_utc,ended_at_local,config_version,model_method,input_json,probabilities_json
                       FROM live.state_intervals WHERE machine_id=%s AND ended_at_utc IS NOT NULL AND started_at_utc<%s AND ended_at_utc>%s ORDER BY started_at_utc""",(mid,eu,su));interval_rows=cur.fetchall()
        cur.execute("SELECT window_end_utc,window_end_local,interval_seconds,config_version,input_json,output_json,state_seconds_json FROM live.neural_windows WHERE machine_id=%s AND window_end_utc>%s AND window_end_utc<=%s ORDER BY window_end_utc",(mid,su,eu));nn_rows=cur.fetchall()
        cur.execute("SELECT state_key,state_name,metric_role FROM config.machine_states WHERE machine_id=%s",(mid,));sm={r[0]:(r[1],r[2]) for r in cur.fetchall()}
    for r in interval_rows:
        s=max(r[3],su);e=min(r[5],eu)
        if e>s:out.append({"state_key":r[0],"state_name":r[1],"metric_role":r[2],"start_utc":s,"end_utc":e,"seconds":(e-s).total_seconds(),"source":"interval","config_version":r[7],"model_method":r[8],"input_json":r[9],"probabilities_json":r[10]})
    for end,end_local,interval,ver,inputs,outputs,state_seconds in nn_rows:
        start=end-timedelta(seconds=float(interval))
        for key,sec in (state_seconds or {}).items():
            if key in sm and float(sec)>0:
                name,role=sm[key];out.append({"state_key":key,"state_name":name,"metric_role":role,"start_utc":start,"end_utc":end,"seconds":float(sec),"source":"neural","interval_seconds":float(interval),"config_version":ver,"model_method":"neural_network","input_json":inputs,"output_json":outputs})
    return out,nn_rows


def hourly(seg):
    b={h:{"hour_of_day":h,"operational_minutes":0.0,"monitored_minutes":0.0,"utilization_pct":None} for h in range(24)};groups={}
    for x in seg:
        if x["source"]=="neural":groups.setdefault((x["start_utc"],x["end_utc"],x["interval_seconds"]),[]).append(x);continue
        cur=x["start_utc"].astimezone(LOCAL_TZ);end=x["end_utc"].astimezone(LOCAL_TZ)
        while cur<end:
            stop=min(cur.replace(minute=0,second=0,microsecond=0)+timedelta(hours=1),end);mins=(stop-cur).total_seconds()/60;r=b[cur.hour];r["monitored_minutes"]+=mins
            if x["metric_role"]=="operational":r["operational_minutes"]+=mins
            cur=stop
    for (start,end,interval),items in groups.items():
        frac=sum(x["seconds"] for x in items if x["metric_role"]=="operational")/interval if interval else 0;cur=start.astimezone(LOCAL_TZ);el=end.astimezone(LOCAL_TZ)
        while cur<el:
            stop=min(cur.replace(minute=0,second=0,microsecond=0)+timedelta(hours=1),el);mins=(stop-cur).total_seconds()/60;r=b[cur.hour];r["monitored_minutes"]+=mins;r["operational_minutes"]+=mins*frac;cur=stop
    for r in b.values():
        if r["monitored_minutes"]>0:r["utilization_pct"]=100*r["operational_minutes"]/r["monitored_minutes"]
    return list(b.values())


def archive_machine(m,day):
    seg,nn_rows=segments(m["machine_id"],day)
    if not seg:return
    hr=hourly(seg);met=calculate(seg,hr);ops=[x for x in seg if x["metric_role"]=="operational" and x["source"]!="neural"]
    first=min((x["start_utc"].astimezone(LOCAL_TZ).time() for x in ops),default=None);last=max((x["end_utc"].astimezone(LOCAL_TZ).time() for x in ops),default=None);span=(max(x["end_utc"] for x in ops)-min(x["start_utc"] for x in ops)).total_seconds()/3600 if ops else None
    with connect() as conn,conn.cursor() as cur:
        for x in [x for x in seg if x["source"]=="interval"]:
            cur.execute("""INSERT INTO history.state_intervals(machine_id,state_key,state_name,metric_role,started_at_utc,started_at_local,ended_at_utc,ended_at_local,duration_seconds,config_version,model_method,input_json,probabilities_json)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",(m["machine_id"],x["state_key"],x["state_name"],x["metric_role"],x["start_utc"],x["start_utc"].astimezone(LOCAL_TZ),x["end_utc"],x["end_utc"].astimezone(LOCAL_TZ),x["seconds"],x["config_version"],x["model_method"],Jsonb(x["input_json"]),Jsonb(x["probabilities_json"]) if x.get("probabilities_json") else None))
        for end,end_local,interval,ver,inputs,outputs,state_seconds in nn_rows:
            cur.execute("""INSERT INTO history.neural_windows(machine_id,window_end_utc,window_end_local,interval_seconds,config_version,input_json,output_json,state_seconds_json)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(machine_id,window_end_utc) DO NOTHING""",(m["machine_id"],end,end_local,interval,ver,Jsonb(inputs),Jsonb(outputs),Jsonb(state_seconds)))
        for r in hr:cur.execute("""INSERT INTO history.hourly_machine_usage(machine_id,local_date,hour_of_day,operational_minutes,monitored_minutes,utilization_pct) VALUES(%s,%s,%s,%s,%s,%s)
                                 ON CONFLICT(machine_id,local_date,hour_of_day) DO UPDATE SET operational_minutes=EXCLUDED.operational_minutes,monitored_minutes=EXCLUDED.monitored_minutes,utilization_pct=EXCLUDED.utilization_pct""",(m["machine_id"],day,r["hour_of_day"],r["operational_minutes"],r["monitored_minutes"],r["utilization_pct"]))
        cur.execute("SELECT COUNT(*) FROM config.machine_sensors WHERE machine_id=%s",(m["machine_id"],));sensor_count=cur.fetchone()[0]
        cur.execute("SELECT method FROM config.machine_model WHERE machine_id=%s",(m["machine_id"],));mr=cur.fetchone();method=mr[0] if mr else None
        cur.execute("SELECT changed_by,changed_at FROM config.configuration_versions WHERE machine_id=%s ORDER BY version DESC LIMIT 1",(m["machine_id"],));cr=cur.fetchone();changed_by=cr[0] if cr else None;changed_at=cr[1] if cr else m["updated_at"]
        cur.execute("""INSERT INTO history.daily_machine_metrics(machine_id,local_date,machine_name,machine_identifier,machine_group_number,machine_group_name,last_setting_update,last_setting_user,config_version,estimation_model,sensor_count,state_hours_json,
                       availability_pct,operational_utilization_pct,active_efficiency_pct,off_time_share_pct,operational_time_share_pct,idle_time_share_pct,setup_time_share_pct,processing_time_share_pct,oee_pct,operation_session_count,
                       avg_operation_session_min,std_operation_session_min,first_operation_time,last_operation_time,operating_span_hours,peak_usage_hour,peak_hour_utilization_pct,metrics_json)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(machine_id,local_date) DO UPDATE SET machine_group_number=EXCLUDED.machine_group_number,machine_group_name=EXCLUDED.machine_group_name,last_setting_update=EXCLUDED.last_setting_update,last_setting_user=EXCLUDED.last_setting_user,
                       config_version=EXCLUDED.config_version,estimation_model=EXCLUDED.estimation_model,sensor_count=EXCLUDED.sensor_count,state_hours_json=EXCLUDED.state_hours_json,availability_pct=EXCLUDED.availability_pct,
                       operational_utilization_pct=EXCLUDED.operational_utilization_pct,active_efficiency_pct=EXCLUDED.active_efficiency_pct,off_time_share_pct=EXCLUDED.off_time_share_pct,operational_time_share_pct=EXCLUDED.operational_time_share_pct,
                       idle_time_share_pct=EXCLUDED.idle_time_share_pct,setup_time_share_pct=EXCLUDED.setup_time_share_pct,processing_time_share_pct=EXCLUDED.processing_time_share_pct,operation_session_count=EXCLUDED.operation_session_count,
                       avg_operation_session_min=EXCLUDED.avg_operation_session_min,std_operation_session_min=EXCLUDED.std_operation_session_min,first_operation_time=EXCLUDED.first_operation_time,last_operation_time=EXCLUDED.last_operation_time,
                       operating_span_hours=EXCLUDED.operating_span_hours,peak_usage_hour=EXCLUDED.peak_usage_hour,peak_hour_utilization_pct=EXCLUDED.peak_hour_utilization_pct,metrics_json=EXCLUDED.metrics_json,archived_at=NOW()""",
                    (m["machine_id"],day,m["machine_name"],m["machine_identifier"],m["group_number"],m["group_name"],changed_at,changed_by,m["version"],method,sensor_count,Jsonb(met["state_hours"]),met["availability_pct"],met["operational_utilization_pct"],met["active_efficiency_pct"],met["off_time_share_pct"],met["operational_time_share_pct"],met["idle_time_share_pct"],met["setup_time_share_pct"],met["processing_time_share_pct"],met["oee_pct"],met["operation_session_count"],met["avg_operation_session_min"],met["std_operation_session_min"],first,last,span,met["peak_usage_hour"],met["peak_hour_utilization_pct"],Jsonb(met)))
        cur.execute("DELETE FROM live.state_intervals WHERE machine_id=%s AND ended_at_utc IS NOT NULL AND ended_at_local::date<=%s",(m["machine_id"],day));cur.execute("DELETE FROM live.neural_windows WHERE machine_id=%s AND window_end_local::date<=%s",(m["machine_id"],day));cur.execute("DELETE FROM live.daily_metrics_cache WHERE machine_id=%s AND local_date<=%s",(m["machine_id"],day));cur.execute("DELETE FROM live.hourly_usage_cache WHERE machine_id=%s AND local_date<=%s",(m["machine_id"],day));conn.commit()


def main():
    day=datetime.now(LOCAL_TZ).date()-timedelta(days=1)
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT machine_id,machine_name,machine_identifier,machine_group_number,machine_group_name,current_config_version,updated_at FROM config.machines WHERE active=TRUE");machines=[{"machine_id":r[0],"machine_name":r[1],"machine_identifier":r[2],"group_number":r[3],"group_name":r[4],"version":r[5],"updated_at":r[6]} for r in cur.fetchall()]
    for m in machines:archive_machine(m,day)
    with connect() as conn,conn.cursor() as cur:
        cur.execute("DELETE FROM history.sensor_readings WHERE timestamp_utc<NOW()-(%s || ' days')::interval",(RAW_RETENTION,));cur.execute("DELETE FROM history.daily_machine_metrics WHERE local_date<CURRENT_DATE-%s",(HIST_RETENTION,));cur.execute("DELETE FROM history.hourly_machine_usage WHERE local_date<CURRENT_DATE-%s",(HIST_RETENTION,));cur.execute("DELETE FROM history.state_intervals WHERE started_at_local::date<CURRENT_DATE-%s",(HIST_RETENTION,));cur.execute("DELETE FROM history.neural_windows WHERE window_end_local::date<CURRENT_DATE-%s",(HIST_RETENTION,));conn.commit()
if __name__=="__main__":main()
