import math
import numpy as np


def compare(value,op,v1=None,v2=None):
    if value is None: return False
    if op==">": return value>v1
    if op==">=": return value>=v1
    if op=="<": return value<v1
    if op=="<=": return value<=v1
    if op=="==": return value==v1
    if op=="!=": return value!=v1
    if op=="between": return v1<=value<=v2
    if op=="outside": return value<v1 or value>v2
    return False


def logistic(values,states,model):
    coeffs=model.get("coefficients",{})
    scores={}
    for s in states:
        c=coeffs.get(s["state_key"],{})
        z=float(c.get("intercept",0))
        for feature,b in c.get("features",{}).items():
            x=values.get(feature)
            if x is not None: z+=float(b)*float(x)
        scores[s["state_key"]]=z
    if not scores: return None,{}
    m=max(scores.values())
    ex={k:math.exp(v-m) for k,v in scores.items()}
    d=sum(ex.values()) or 1
    p={k:v/d for k,v in ex.items()}
    return max(p,key=p.get),p


def rules(values,states,model):
    for rule in sorted(model.get("rules",[]),key=lambda r:int(r.get("priority",9999))):
        result=None
        for i,c in enumerate(rule.get("conditions",[])):
            v1=float(c["value1"]) if c.get("value1") not in ("",None) else None
            v2=float(c["value2"]) if c.get("value2") not in ("",None) else None
            cur=compare(values.get(c.get("feature")),c.get("operator"),v1,v2)
            result=cur if i==0 else ((result and cur) if c.get("join","AND").upper()=="AND" else (result or cur))
        if result: return rule.get("state_key"),{}
    return model.get("default_state_key"),{}


def _activation(x,name):
    name=(name or "linear").lower()
    if name=="relu": return np.maximum(x,0)
    if name=="tanh": return np.tanh(x)
    if name=="sigmoid": return 1/(1+np.exp(-np.clip(x,-60,60)))
    if name=="leaky_relu": return np.where(x>=0,x,0.01*x)
    if name=="elu": return np.where(x>=0,x,np.expm1(x))
    if name=="gelu": return 0.5*x*(1+np.tanh(np.sqrt(2/np.pi)*(x+0.044715*np.power(x,3))))
    if name=="softplus": return np.log1p(np.exp(-np.abs(x)))+np.maximum(x,0)
    return x


def validate_neural_export(model):
    required=["format","input_names","output_names","weights","biases","activations","x_mean","x_scale","interval_seconds","output_mode"]
    missing=[k for k in required if k not in model]
    if missing: raise ValueError("Neural model JSON missing: "+", ".join(missing))
    if model.get("format")!="monnit_time_distribution_nn_v1":
        raise ValueError("Unsupported neural model format")
    if len(model["weights"])!=len(model["biases"]):
        raise ValueError("Weights/biases layer count mismatch")
    return True


def neural_raw_outputs(values,model):
    validate_neural_export(model)
    xs=[]
    for name in model["input_names"]:
        v=values.get(name)
        if v is None: v=values.get(name.lower())
        if v is None: v=values.get(name.upper())
        if v is None: raise ValueError(f"Missing neural input {name}")
        xs.append(float(v))
    x=np.asarray(xs,dtype=float)
    mean=np.asarray(model["x_mean"],dtype=float)
    scale=np.asarray(model["x_scale"],dtype=float)
    scale=np.where(scale==0,1.0,scale)
    a=(x-mean)/scale

    acts=model.get("activations",[])
    for i,(w,b) in enumerate(zip(model["weights"],model["biases"])):
        W=np.asarray(w,dtype=float); B=np.asarray(b,dtype=float)
        a=a@W.T+B
        if i < len(model["weights"])-1:
            a=_activation(a,acts[i] if i<len(acts) else "relu")

    mode=model.get("output_mode","softmax_distribution")
    interval=float(model.get("interval_seconds",600))
    if mode=="softmax_distribution":
        z=a-np.max(a)
        ex=np.exp(np.clip(z,-60,60))
        frac=ex/(np.sum(ex) or 1.0)
        seconds=frac*interval
    else:
        out_act=model.get("output_activation","softplus")
        y=_activation(a,out_act)
        seconds=np.maximum(y,0)*float(model.get("target_scale_seconds",interval))
        if model.get("normalize_outputs_to_interval_seconds",False) and np.sum(seconds)>0:
            seconds=seconds/np.sum(seconds)*interval
    return {name:float(seconds[i]) for i,name in enumerate(model["output_names"])}


def neural_state_seconds(values,model_json):
    model=model_json.get("neural_model") or {}
    mapping=model_json.get("output_mapping") or {}
    raw=neural_raw_outputs(values,model)
    state_seconds={}
    for output_name,seconds in raw.items():
        state_key=mapping.get(output_name)
        if state_key:
            state_seconds[state_key]=state_seconds.get(state_key,0.0)+max(0.0,float(seconds))
    return raw,state_seconds


def predict(values,states,method,model):
    if method=="logistic": return logistic(values,states,model)
    if method=="rules": return rules(values,states,model)
    raise ValueError("Use neural_state_seconds() for neural_network models")
