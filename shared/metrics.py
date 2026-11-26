import math

METRICS=[
 {"key":"availability_pct","name":"Availability","formula":"Non-off classified time / total classified time × 100","required_all":["off"],"required_any":["operational","idle","setup","processing"]},
 {"key":"operational_utilization_pct","name":"Operational Utilization","formula":"Operational time / total classified time × 100","required_all":["operational"]},
 {"key":"active_efficiency_pct","name":"Active Efficiency","formula":"Operational time / available time × 100","required_all":["operational"],"required_any":["idle","setup","processing"]},
 {"key":"off_time_share_pct","name":"Off-Time Share","formula":"Off time / total classified time × 100","required_all":["off"]},
 {"key":"operational_time_share_pct","name":"Operational-Time Share","formula":"Operational time / total classified time × 100","required_all":["operational"]},
 {"key":"idle_time_share_pct","name":"Idle-Time Share","formula":"Idle time / total classified time × 100","required_all":["idle"]},
 {"key":"setup_time_share_pct","name":"Setup-Time Share","formula":"Setup time / total classified time × 100","required_all":["setup"]},
 {"key":"processing_time_share_pct","name":"Processing-Time Share","formula":"Processing time / total classified time × 100","required_all":["processing"]},
 {"key":"operation_session_count","name":"Operational Session Count","formula":"Number of continuous operational periods","required_all":["operational"],"sequence_required":True},
 {"key":"avg_operation_session_min","name":"Average Operational Session","formula":"Mean duration of continuous operational periods","required_all":["operational"],"sequence_required":True},
 {"key":"std_operation_session_min","name":"Operational Session Standard Deviation","formula":"Sample standard deviation of operational-session duration","required_all":["operational"],"sequence_required":True},
 {"key":"peak_usage_hour","name":"Peak Usage Hour","formula":"Hour with maximum operational utilization","required_all":["operational"]},
 {"key":"peak_hour_utilization_pct","name":"Peak-Hour Utilization","formula":"Maximum hourly operational utilization","required_all":["operational"]},
 {"key":"oee_pct","name":"OEE","formula":"Availability × Performance × Quality × 100","required_all":["operational","off"],"extra":["ideal_cycle_time_sec","total_count_feature","good_count_feature"]}
]
ROLES={"operational":"Operational","idle":"Idle","off":"Off","setup":"Setup","processing":"Processing","failure":"Failure","repair":"Repair","other":"Other"}


def eligible(metric,roles,params=None,model_method=None):
    params=params or {};missing=[]
    for r in metric.get("required_all",[]):
        if r not in roles: missing.append(f"state/time role '{ROLES.get(r,r)}'")
    anyr=metric.get("required_any",[])
    if anyr and not any(r in roles for r in anyr): missing.append("one of: "+", ".join(ROLES.get(r,r) for r in anyr))
    for x in metric.get("extra",[]):
        if not params.get(x): missing.append(x.replace('_',' '))
    if metric.get("sequence_required") and model_method=="neural_network":
        missing.append("ordered state transitions (neural time-distribution output does not identify within-window sequence)")
    return not missing,missing


def calculate(segments,hourly=None):
    role={};state={};sessions=[];has_neural=False
    for x in segments:
        sec=max(0,float(x.get("seconds",0)))
        role[x["metric_role"]]=role.get(x["metric_role"],0)+sec
        state[x["state_name"]]=state.get(x["state_name"],0)+sec
        if x.get("source")=="neural": has_neural=True
        if x["metric_role"]=="operational" and x.get("source")!="neural": sessions.append(sec)
    total=sum(role.values());op=role.get("operational",0);off=role.get("off",0);available=total-off
    out={
      "availability_pct":100*available/total if total else None,
      "operational_utilization_pct":100*op/total if total else None,
      "active_efficiency_pct":100*op/available if available else None,
      "off_time_share_pct":100*off/total if total else None,
      "operational_time_share_pct":100*op/total if total else None,
      "idle_time_share_pct":100*role.get("idle",0)/total if total else None,
      "setup_time_share_pct":100*role.get("setup",0)/total if total else None,
      "processing_time_share_pct":100*role.get("processing",0)/total if total else None,
      "operation_session_count":None if has_neural else len(sessions),
      "avg_operation_session_min":None if has_neural or not sessions else sum(sessions)/len(sessions)/60,
      "std_operation_session_min":None,
      "peak_usage_hour":None,"peak_hour_utilization_pct":None,"oee_pct":None,
      "state_hours":{k:v/3600 for k,v in state.items()}
    }
    if not has_neural:
        if len(sessions)==1: out["std_operation_session_min"]=0.0
        elif len(sessions)>1:
            m=sum(sessions)/len(sessions);out["std_operation_session_min"]=math.sqrt(sum((x-m)**2 for x in sessions)/(len(sessions)-1))/60
    valid=[r for r in (hourly or []) if r.get("utilization_pct") is not None]
    if valid:
        best=max(valid,key=lambda r:r["utilization_pct"]);out["peak_usage_hour"]=best["hour_of_day"];out["peak_hour_utilization_pct"]=best["utilization_pct"]
    return out
