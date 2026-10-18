import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S",time.gmtime(t))+".%03dZ"%int((t%1)*1000)
def err(status,code,message="x",details=None,headers=None):
    e={"code":code,"message":message,"request_id":"req_mock"}
    if details:
        e["details"]=details
    return status,headers or {},{"error":e}
class Req:
    def __init__(self,method,target,headers,body):
        u=urllib.parse.urlsplit(target)
        self.method=method
        self.path=u.path
        self.query={k:v[0] for k,v in urllib.parse.parse_qs(u.query).items()}
        self.headers=headers
        self.body=body
        self.at=time.monotonic()
    @property
    def json(self):
        return json.loads(self.body) if self.body else None
    @property
    def token(self):
        h=self.headers.get("authorization","")
        return h[7:] if h.startswith("Bearer ") else None
class Handler(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"
    def log_message(self,*a):
        pass
    def handle_one(self):
        n=int(self.headers.get("Content-Length") or 0)
        req=Req(self.command,self.path,{k.lower():v for k,v in self.headers.items()},self.rfile.read(n) if n else b"")
        with self.server.lock:
            self.server.requests.append(req)
        out=self.server.mock.answer(req)
        if out=="drop":
            self.close_connection=True
            return
        status,headers,body=out
        if isinstance(body,(dict,list)):
            raw=json.dumps(body).encode()
            headers={"Content-Type":"application/json",**headers}
        else:
            raw=body or b""
        self.send_response(status)
        for k,v in headers.items():
            self.send_header(k,v)
        self.send_header("X-Request-Id",headers.get("X-Request-Id","req_mock"))
        if status!=304:
            self.send_header("Content-Length",str(len(raw)))
        self.send_header("Connection","close")
        self.end_headers()
        if status!=304:
            self.wfile.write(raw)
        self.close_connection=True
    do_GET=handle_one
    do_POST=handle_one
class Mock:
    def __init__(self):
        self.routes={}
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        self.server.daemon_threads=True
        self.server.mock=self
        self.server.requests=[]
        self.server.lock=threading.Lock()
        self.url=f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={"poll_interval":0.02},daemon=True)
        self.thread.start()
    @property
    def requests(self):
        return list(self.server.requests)
    def calls(self,method,path):
        return [r for r in self.requests if r.method==method and r.path==path]
    def on(self,method,path,*answers):
        self.routes[(method,path)]=list(answers)
    def answer(self,req):
        q=self.routes.get((req.method,req.path))
        if q is None:
            return err(404,"not_found","No such endpoint.")
        a=q.pop(0) if len(q)>1 else q[0]
        return a(req) if callable(a) else a
    def close(self):
        self.server.shutdown()
        self.server.server_close()
