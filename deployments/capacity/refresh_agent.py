"""Mac-side outbound poller: turns a phone refresh request into one bounded local collection.

The dashboard never reaches into the Mac. This agent polls the authenticated cloud endpoint
with the upload token, runs the operator-configured collector command, uploads the sanitized
snapshot and reports a fixed-shape outcome. It cannot launch inference, change credentials or
shorten a provider cooldown: the collector command owns cooldown enforcement and reports it.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from upload import upload

MAX_COLLECTOR_OUTPUT=64*1024
PROVIDER_STATUSES=('ok','cooldown','auth_required','error','unknown')


def _now():return datetime.now(timezone.utc).isoformat()


def load_config(path):
    config=json.loads(Path(path).read_text())
    for key in ('remote_url','upload_token','local_feed'):
        if not isinstance(config.get(key),str) or not config[key]:raise ValueError(key+' required')
    argv=config.get('collect_argv')
    if argv is not None and (not isinstance(argv,list) or not argv or not all(isinstance(a,str) for a in argv) or not os.path.isabs(argv[0])):
        raise ValueError('collect_argv must be a non-empty list starting with an absolute program path')
    timeout=config.get('collect_timeout',60);poll=config.get('poll_interval',5)
    if type(timeout) not in (int,float) or not 1<=timeout<=120:raise ValueError('collect_timeout must be 1..120 seconds')
    if type(poll) not in (int,float) or not 1<=poll<=60:raise ValueError('poll_interval must be 1..60 seconds')
    return config


def _api(config,path,body=None):
    req=urllib.request.Request(config['remote_url']+path,data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+config['upload_token'],'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(req,timeout=20) as response:
        raw=response.read(MAX_COLLECTOR_OUTPUT)
        return response.status,(json.loads(raw) if raw else None)


def run_collector(config):
    """Run the trusted collector once with a wall-clock bound. Returns (reason, providers)."""
    argv=config.get('collect_argv')
    if argv is None:return 'collector_unavailable',[]
    try:
        proc=subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,timeout=config.get('collect_timeout',60),check=False)
    except subprocess.TimeoutExpired:return 'collector_timeout',[]
    except OSError:return 'collector_error',[]
    if proc.returncode!=0 or len(proc.stdout)>MAX_COLLECTOR_OUTPUT:return 'collector_error',[]
    try:
        report=json.loads(proc.stdout);providers=report['providers']
        if not isinstance(providers,list) or len(providers)>10:raise ValueError
        clean=[]
        for p in providers:
            if not isinstance(p.get('provider'),str) or p.get('status') not in PROVIDER_STATUSES:raise ValueError
            eligible=p.get('next_eligible_at')
            if eligible is not None and not isinstance(eligible,str):raise ValueError
            clean.append({'provider':p['provider'][:40],'status':p['status'],'next_eligible_at':eligible})
    except (ValueError,TypeError,KeyError,AttributeError):return 'collector_error',[]
    return 'collected',clean


def handle(config,request):
    """Collect, upload, and build the sanitized outcome for one claimed request."""
    reason,providers=run_collector(config)
    collected=reason=='collected'
    if reason in ('collector_timeout','collector_error'):
        return {'state':'failed','reason':reason,'collected':False,'providers':[]}
    status=upload(config)
    if status['status']!='ok':
        return {'state':'failed','reason':'upload_failed','collected':collected,'providers':providers}
    if reason=='collector_unavailable':
        return {'state':'completed','reason':'snapshot_only','collected':False,'providers':[]}
    state='cooldown' if any(p['status']=='cooldown' for p in providers) else 'completed'
    return {'state':state,'reason':'collected','collected':True,'providers':providers}


def run(config,*,once=False,status_path=None):
    stop=[]
    if not once:
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.append(True))
    record={'started_at':_now(),'handled':0,'last_error':None}
    while not stop:
        try:
            code,request=_api(config,'/api/refresh/claim')
        except Exception as exc:
            record['last_error']=type(exc).__name__;_write_status(status_path,record)
            if once:return 1
            time.sleep(config.get('poll_interval',5));continue
        if code!=200 or not request:
            _write_status(status_path,record)
            if once:return 0
            time.sleep(config.get('poll_interval',5));continue
        outcome=handle(config,request)
        try:
            _api(config,'/api/refresh/complete',{'id':request['id'],'outcome':outcome})
            record['last_error']=None
        except Exception as exc:record['last_error']=type(exc).__name__
        record.update(handled=record['handled']+1,last_request=request['id'],last_outcome={'state':outcome['state'],'reason':outcome['reason']},last_completed_at=_now())
        _write_status(status_path,record)
        if once:return 0
    return 0


def _write_status(path,record):
    if path is None:return
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(record));os.chmod(temp,0o600);temp.replace(path)


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    once='--once' in argv;paths=[a for a in argv if a!='--once']
    if len(paths)!=1:print('usage: refresh_agent.py [--once] /path/to/private-client.json',file=sys.stderr);return 2
    path=Path(paths[0]);config=load_config(path)
    return run(config,once=once,status_path=path.with_name('refresh-status.json'))

if __name__=='__main__':raise SystemExit(main())
