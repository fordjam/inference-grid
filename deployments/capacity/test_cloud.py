import http.client,json,tempfile,threading,time,unittest
from pathlib import Path
from urllib.parse import urlencode
from http.server import ThreadingHTTPServer
from cloud_server import Store,handler,password_hash,token,valid_token,clean_snapshot,COOKIE

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db'
  self.store=Store(self.path)
  self.config=dict(host='dashboard.test',username='owner',salt='ab'*16,password_hash=password_hash('test-password','ab'*16),session_key='s'*64,upload_token='u'*64)
  self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.config,self.store));self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
 def request(self,path,method='GET',body=None,headers=None):
  conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
  conn.request(method,path,body=body,headers={'Host':'dashboard.test',**(headers or {})});r=conn.getresponse();out=(r.status,dict(r.getheaders()),r.read());conn.close();return out
 def sample(self):
  from datetime import datetime,timezone
  return dict(captured_at=datetime.now(timezone.utc).isoformat(),accounts=[dict(provider='claude',observed_at=datetime.now(timezone.utc).isoformat(),status='ok',windows=[dict(id='weekly',used_percent=10)],secret='DO_NOT_SHARE')],attempts=[{'task':'private prompt'}],credentials='DO_NOT_SHARE')
 def test_unauthenticated_cannot_read(self):
  self.assertEqual(self.request('/api/usage')[0],401)
  self.assertEqual(self.request('/')[0],303)
  self.assertEqual(self.request('/index.html')[0],303)
 def test_host_rejected(self):self.assertEqual(self.request('/api/usage',headers={'Host':'evil.test'})[0],403)
 def test_login_origin_and_password(self):
  form=urlencode(dict(username='owner',password='test-password'))
  self.assertEqual(self.request('/login','POST',form)[0],403)
  self.assertEqual(self.request('/login','POST',urlencode(dict(username='owner',password='wrong')),{'Origin':'https://dashboard.test'})[0],401)
  status,headers,_=self.request('/login','POST',form,{'Origin':'https://dashboard.test'})
  self.assertEqual(status,303)
  for attr in ['Secure','HttpOnly','SameSite=Strict']:self.assertIn(attr,headers['Set-Cookie'])
  cookie=headers['Set-Cookie'].split(';')[0]
  self.assertEqual(self.request('/api/usage',headers={'Cookie':cookie})[0],200)
 def test_browser_login_without_origin(self):
  import re
  status,headers,page=self.request('/login')
  self.assertEqual(status,200)
  self.assertEqual(headers['Referrer-Policy'],'same-origin')
  csrf=re.search(rb'name="csrf" value="([^"]+)"',page).group(1).decode()
  cookie=headers['Set-Cookie'].split(';')[0]
  form=urlencode(dict(username='owner',password='test-password',csrf=csrf))
  for origin in (None,'null'):
   h={'Cookie':cookie}
   if origin is not None:h['Origin']=origin
   self.assertEqual(self.request('/login','POST',form,h)[0],303)
  self.assertEqual(self.request('/login','POST',form,{'Cookie':cookie,'Origin':'https://evil.test'})[0],403)
  self.assertEqual(self.request('/login','POST',form,{'Origin':'null'})[0],403)
  self.assertEqual(self.request('/login','POST',form.replace(csrf,'invalid'),{'Cookie':cookie})[0],403)
  self.assertEqual(self.request('/logout','POST','',{'Cookie':cookie})[0],403)
 def test_upload_token_separate_from_login(self):
  payload=json.dumps(self.sample())
  self.assertEqual(self.request('/api/snapshot','POST',payload)[0],401)
  self.assertEqual(self.request('/api/snapshot','POST',payload,{'Cookie':COOKIE+'='+token(self.config['session_key'])})[0],401)
  self.assertEqual(self.request('/api/snapshot','POST',payload,{'Authorization':'Bearer '+self.config['upload_token']})[0],200)
  self.assertEqual(self.request('/api/usage',headers={'Authorization':'Bearer '+self.config['upload_token']})[0],401)
  self.assertNotIn('DO_NOT_SHARE',json.dumps(self.store.get()));self.assertNotIn('private prompt',json.dumps(self.store.get()))
 def test_durability_and_old_upload_refusal(self):
  data,captured=clean_snapshot(self.sample());self.assertTrue(self.store.put(data,captured));self.assertFalse(self.store.put(data,captured-1))
  self.assertEqual(Store(self.path).get()['captured_at'],data['captured_at'])
 def test_expiry_tamper_and_key_rotation(self):
  v=token('one',now=100)
  self.assertTrue(valid_token(v,'one',now=101));self.assertFalse(valid_token(v,'two',now=101));self.assertFalse(valid_token(v+'x','one',now=101));self.assertFalse(valid_token(v,'one',now=100+31*86400))
 def test_oversize_rejected_before_body_read(self):
  self.assertEqual(self.request('/api/snapshot','POST','{}',{'Authorization':'Bearer '+self.config['upload_token'],'Content-Length':'200000'})[0],400)
 def test_future_or_naive_observation_rejected(self):
  s=self.sample();s['accounts'][0]['observed_at']='2999-01-01T00:00:00Z'
  with self.assertRaises(ValueError):clean_snapshot(s)
  s=self.sample();s['captured_at']='2026-09-12T12:00:00'
  with self.assertRaises(ValueError):clean_snapshot(s)

if __name__=='__main__':unittest.main()
