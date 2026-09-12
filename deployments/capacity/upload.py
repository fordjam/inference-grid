"""Upload only sanitized usage observations; never native provider credentials."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.request
import urllib.error


def upload(config):
    """Read the local sanitized feed and post it; returns a status record without secrets."""
    now=datetime.now(timezone.utc).isoformat()
    status={'attempted_at':now,'status':'failed'}
    try:
        with urllib.request.urlopen(config['local_feed'],timeout=12) as response:raw=json.load(response)
        accounts=[]
        for a in raw.get('accounts',[]):
            accounts.append({k:a.get(k) for k in ['provider','status','observed_at','windows','monthly_remaining']})
        body=json.dumps({'captured_at':now,'accounts':accounts},allow_nan=False).encode()
        req=urllib.request.Request(config['remote_url']+'/api/snapshot',data=body,headers={'Authorization':'Bearer '+config['upload_token'],'Content-Type':'application/json'},method='POST')
        with urllib.request.urlopen(req,timeout=20) as response:
            if response.status!=200:raise RuntimeError('upload refused')
        status.update(status='ok',uploaded_at=now,providers=len(accounts))
    except Exception as exc:status.update(error=type(exc).__name__,http_status=getattr(exc,'code',None))
    return status


def main():
    path=Path(sys.argv[1]);status=upload(json.loads(path.read_text()))
    out=path.with_name('upload-status.json');temp=out.with_suffix('.tmp');temp.write_text(json.dumps(status));os.chmod(temp,0o600);temp.replace(out)
    print(json.dumps(status))
    return 0 if status['status']=='ok' else 1

if __name__=='__main__':raise SystemExit(main())
