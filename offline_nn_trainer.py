"""Offline trainer for Monnit 10-minute machine-state time distributions.

Excel columns:
  X1, X2, ... = sensor average inputs
  Y1, Y2, ... = seconds spent in machine states during the interval

Menu:
  1 = train a new model
  2 = validate an exported JSON model on another Excel file
"""
import json,re,math
from pathlib import Path
from datetime import datetime,timezone
import tkinter as tk
from tkinter import filedialog
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader,TensorDataset
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error,mean_squared_error

ACTS={
 'relu':nn.ReLU,'tanh':nn.Tanh,'sigmoid':nn.Sigmoid,'leaky_relu':nn.LeakyReLU,'elu':nn.ELU,'gelu':nn.GELU
}

def choose_file(title,types):
    root=tk.Tk();root.withdraw();path=filedialog.askopenfilename(title=title,filetypes=types);root.destroy();return path

def save_file(title,default):
    root=tk.Tk();root.withdraw();path=filedialog.asksaveasfilename(title=title,defaultextension='.json',initialfile=default,filetypes=[('JSON','*.json')]);root.destroy();return path

def sorted_cols(cols,prefix):
    out=[c for c in cols if re.fullmatch(prefix+r'\d+',str(c),re.I)]
    return sorted(out,key=lambda c:int(re.search(r'\d+',str(c)).group()))

class Net(nn.Module):
    def __init__(self,n_in,n_out,hidden,activations):
        super().__init__();layers=[];last=n_in
        for n,a in zip(hidden,activations):layers += [nn.Linear(last,n),ACTS[a]()] ; last=n
        layers.append(nn.Linear(last,n_out));self.net=nn.Sequential(*layers)
    def forward(self,x):return self.net(x)

def eval_export(model,x):
    x=np.asarray(x,float);mean=np.asarray(model['x_mean']);scale=np.asarray(model['x_scale']);scale=np.where(scale==0,1,scale);a=(x-mean)/scale
    def act(z,n):
        if n=='relu':return np.maximum(z,0)
        if n=='tanh':return np.tanh(z)
        if n=='sigmoid':return 1/(1+np.exp(-np.clip(z,-60,60)))
        if n=='leaky_relu':return np.where(z>=0,z,.01*z)
        if n=='elu':return np.where(z>=0,z,np.expm1(z))
        if n=='gelu':return .5*z*(1+np.tanh(np.sqrt(2/np.pi)*(z+.044715*z**3)))
        if n=='softplus':return np.log1p(np.exp(-np.abs(z)))+np.maximum(z,0)
        return z
    for i,(w,b) in enumerate(zip(model['weights'],model['biases'])):
        a=a@np.asarray(w,float).T+np.asarray(b,float)
        if i<len(model['weights'])-1:a=act(a,model['activations'][i])
    interval=float(model['interval_seconds'])
    if model['output_mode']=='softmax_distribution':
        z=a-np.max(a,axis=-1,keepdims=True);e=np.exp(np.clip(z,-60,60));return e/e.sum(axis=-1,keepdims=True)*interval
    y=act(a,model.get('output_activation','softplus'))*float(model.get('target_scale_seconds',interval));y=np.maximum(y,0)
    if model.get('normalize_outputs_to_interval_seconds'):
        sums=y.sum(axis=-1,keepdims=True);y=np.where(sums>0,y/sums*interval,y)
    return y

def report(y_true,y_pred,names,label):
    print(f'\n{label}')
    print('-'*72)
    for i,n in enumerate(names):
        mae=mean_absolute_error(y_true[:,i],y_pred[:,i]);rmse=math.sqrt(mean_squared_error(y_true[:,i],y_pred[:,i]))
        print(f'{n:>6s} | MAE={mae:9.3f} sec | RMSE={rmse:9.3f} sec')
    print('Overall MAE:',mean_absolute_error(y_true,y_pred))

def train():
    path=choose_file('Select training Excel file',[('Excel','*.xlsx *.xls')]);
    if not path:return
    df=pd.read_excel(path);xcols=sorted_cols(df.columns,'X');ycols=sorted_cols(df.columns,'Y')
    if not xcols or not ycols:raise ValueError('Excel must contain X1..Xn and Y1..Ym headers.')
    data=df[xcols+ycols].dropna();X=data[xcols].to_numpy(float);Y=data[ycols].to_numpy(float)
    interval=float(input('Interval length in seconds [600]: ') or '600')
    hidden=[int(x.strip()) for x in (input('Hidden layer sizes, comma-separated [64,32]: ') or '64,32').split(',') if x.strip()]
    acts=[x.strip().lower() for x in (input('Activation per hidden layer [relu,relu]: ') or ','.join(['relu']*len(hidden))).split(',') if x.strip()]
    if len(acts)==1 and len(hidden)>1:acts=acts*len(hidden)
    if len(acts)!=len(hidden) or any(a not in ACTS for a in acts):raise ValueError('Activation list must match hidden layers. Options: '+', '.join(ACTS))
    mode=(input('Output mode: 1=softmax time distribution (recommended), 2=independent seconds [1]: ') or '1').strip()
    output_mode='softmax_distribution' if mode=='1' else 'independent_seconds'
    output_activation='linear'
    normalize=False
    if output_mode=='independent_seconds':
        output_activation=(input('Output activation linear/softplus/relu [softplus]: ') or 'softplus').strip().lower();normalize=(input('Normalize predicted outputs to sum to interval seconds? y/n [n]: ') or 'n').lower().startswith('y')
    epochs=int(input('Epochs [500]: ') or '500');batch=int(input('Batch size [32]: ') or '32');lr=float(input('Learning rate [0.001]: ') or '0.001')
    Xtr,Xte,Ytr,Yte=train_test_split(X,Y,test_size=.20,random_state=42,shuffle=True)
    mean=Xtr.mean(axis=0);scale=Xtr.std(axis=0);scale=np.where(scale==0,1,scale);XtrN=(Xtr-mean)/scale;XteN=(Xte-mean)/scale
    if output_mode=='softmax_distribution':
        sums=Ytr.sum(axis=1,keepdims=True);target=np.where(sums>0,Ytr/sums,0)
    else:target=Ytr/interval
    torch.manual_seed(42);net=Net(len(xcols),len(ycols),hidden,acts);opt=torch.optim.Adam(net.parameters(),lr=lr);lossfn=nn.MSELoss();loader=DataLoader(TensorDataset(torch.tensor(XtrN,dtype=torch.float32),torch.tensor(target,dtype=torch.float32)),batch_size=batch,shuffle=True)
    for ep in range(1,epochs+1):
        net.train();running=0
        for xb,yb in loader:
            opt.zero_grad();raw=net(xb);pred=torch.softmax(raw,dim=1) if output_mode=='softmax_distribution' else (torch.nn.functional.softplus(raw) if output_activation=='softplus' else torch.relu(raw) if output_activation=='relu' else raw);loss=lossfn(pred,yb);loss.backward();opt.step();running+=loss.item()*len(xb)
        if ep==1 or ep%max(1,epochs//10)==0:print(f'Epoch {ep}/{epochs} loss={running/len(XtrN):.6f}')
    weights=[];biases=[]
    for layer in net.net:
        if isinstance(layer,nn.Linear):weights.append(layer.weight.detach().cpu().numpy().tolist());biases.append(layer.bias.detach().cpu().numpy().tolist())
    export={'format':'monnit_time_distribution_nn_v1','created_utc':datetime.now(timezone.utc).isoformat(),'input_names':[str(c).upper() for c in xcols],'output_names':[str(c).upper() for c in ycols],
            'interval_seconds':interval,'output_mode':output_mode,'hidden_layers':hidden,'activations':acts,'output_activation':output_activation,'normalize_outputs_to_interval_seconds':normalize,'target_scale_seconds':interval,
            'x_mean':mean.tolist(),'x_scale':scale.tolist(),'weights':weights,'biases':biases,'training':{'train_rows':len(Xtr),'test_rows':len(Xte),'epochs':epochs,'batch_size':batch,'learning_rate':lr,'random_state':42}}
    pred=eval_export(export,Xte);report(Yte,pred,ycols,'20% TEST SET')
    if (input('\nValidate on a separate Excel file now? y/n [n]: ') or 'n').lower().startswith('y'):
        vp=choose_file('Select validation Excel file',[('Excel','*.xlsx *.xls')])
        if vp:
            vd=pd.read_excel(vp);VX=vd[xcols].dropna().to_numpy(float);vpredict=eval_export(export,VX)
            if all(c in vd.columns for c in ycols):V=vd[xcols+ycols].dropna();report(V[ycols].to_numpy(float),eval_export(export,V[xcols].to_numpy(float)),ycols,'EXTERNAL VALIDATION SET')
            pd.DataFrame(vpredict,columns=[f'Pred_{c}_sec' for c in ycols]).to_excel(Path(vp).with_name(Path(vp).stem+'_predictions.xlsx'),index=False)
    out=save_file('Save trained neural model JSON','machine_time_distribution_nn.json')
    if out:Path(out).write_text(json.dumps(export,indent=2),encoding='utf-8');print('Saved:',out)

def validate_existing():
    mp=choose_file('Select exported model JSON',[('JSON','*.json')]);
    if not mp:return
    model=json.loads(Path(mp).read_text(encoding='utf-8'));xp=choose_file('Select validation Excel file',[('Excel','*.xlsx *.xls')]);
    if not xp:return
    df=pd.read_excel(xp);xcols=model['input_names'];ycols=model['output_names'];clean=df[xcols].dropna();pred=eval_export(model,clean.to_numpy(float));out=pd.DataFrame(pred,columns=[f'Pred_{c}_sec' for c in ycols]);out.to_excel(Path(xp).with_name(Path(xp).stem+'_predictions.xlsx'),index=False)
    if all(c in df.columns for c in ycols):both=df[xcols+ycols].dropna();report(both[ycols].to_numpy(float),eval_export(model,both[xcols].to_numpy(float)),ycols,'VALIDATION RESULTS')
    print('Prediction workbook saved beside validation file.')

if __name__=='__main__':
    print('Monnit Multi-Input / Multi-Output Neural Network Trainer')
    choice=(input('1 = Train new model\n2 = Validate existing JSON model\nChoose [1]: ') or '1').strip()
    validate_existing() if choice=='2' else train()
