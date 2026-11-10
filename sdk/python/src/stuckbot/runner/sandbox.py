"""Sandboxes for runner sessions.

bwrap (bubblewrap): a fresh user, mount, PID, IPC, UTS and network namespace. The session sees
read-only system folders, its own workspace copy, an output folder and an empty home. It has no
network of its own: the only way out is an HTTP CONNECT proxy that the runner serves on a Unix
socket, and that proxy only connects to the host names the config allows, on port 443, and never
to private, loopback or link-local addresses.

none: no isolation at all. Only for machines that are already disposable (a throwaway VM or
container you built for this). The runner refuses it unless the config says so explicitly.
"""
from __future__ import annotations
import ipaddress
import os
import re
import secrets
import select
import shutil
import socket
import ssl
import threading
import urllib.parse
from typing import Any,Callable,Dict,List,Optional,Sequence
DEFAULT_UPSTREAMS={
    "anthropic":("https://api.anthropic.com","x-api-key",""),
    "openai":("https://api.openai.com","Authorization","Bearer "),
    "openrouter":("https://openrouter.ai/api","Authorization","Bearer "),
}
SHIM=r'''
import os,select,signal,socket,subprocess,sys,threading
port=int(sys.argv[1]);path=sys.argv[2];argv=sys.argv[4:]
srv=socket.socket();srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);srv.bind(("127.0.0.1",port));srv.listen(64)
def pipe(a,b):
    try:
        while True:
            r,_,_=select.select([a,b],[],[],600)
            if not r:return
            for s in r:
                d=s.recv(65536)
                if not d:return
                (b if s is a else a).sendall(d)
    except OSError:pass
    finally:
        a.close();b.close()
def serve():
    while True:
        c,_=srv.accept()
        u=socket.socket(socket.AF_UNIX)
        try:u.connect(path)
        except OSError:c.close();continue
        threading.Thread(target=pipe,args=(c,u),daemon=True).start()
threading.Thread(target=serve,daemon=True).start()
p=subprocess.Popen(argv)
for s in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):
    signal.signal(s,lambda n,f:p.send_signal(n))
sys.exit(p.wait())
'''
PROXY_PORT=3128
ETC=["/etc/ssl","/etc/pki","/etc/ca-certificates","/etc/crypto-policies","/etc/alternatives","/etc/localtime","/etc/nsswitch.conf","/etc/ld.so.cache","/etc/ld.so.conf","/etc/ld.so.conf.d","/etc/gitconfig","/etc/mime.types"]
def _blocked_ip(ip:str)->bool:
    a=ipaddress.ip_address(ip)
    if isinstance(a,ipaddress.IPv6Address) and a.ipv4_mapped:
        a=a.ipv4_mapped
    return a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified or (isinstance(a,ipaddress.IPv4Address) and a in ipaddress.ip_network("100.64.0.0/10"))
def host_allowed(host:str,allow:Sequence[str])->bool:
    host=host.lower().rstrip(".")
    for a in allow:
        a=a.lower().rstrip(".")
        if a.startswith("*."):
            if host.endswith(a[1:]) and host!=a[2:]:
                return True
        elif host==a:
            return True
    return False
class EgressProxy:
    """A CONNECT and credential-brokering proxy on a Unix socket that allows listed hosts and logs every attempt."""
    def __init__(self,path:str,allow:Sequence[str],log:Callable[...,None],ports:Sequence[int]=(443,),allow_private:bool=False,resolver:Callable[[str,int],List[str]]=None,brokers:Optional[Dict[str,Dict[str,Any]]]=None):
        self.path=path
        self.allow=list(allow)
        self.ports=set(ports)
        self.allow_private=allow_private
        self.log=log
        self.resolver=resolver or (lambda h,p:[i[4][0] for i in socket.getaddrinfo(h,p,type=socket.SOCK_STREAM)])
        self.brokers:Dict[str,Dict[str,Any]]={}
        self.tokens:Dict[str,str]={}
        for name,b in (brokers or {}).items():
            self.register_broker(name,b["upstream"],b["secret"],b.get("header","Authorization"),b.get("prefix","Bearer "),b.get("token"))
        self.bytes_out=0
        self.bytes_in=0
        self.allowed=0
        self.denied=0
        self._stop=threading.Event()
        if os.path.exists(path):
            os.unlink(path)
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.bind(path)
        os.chmod(path,0o600)
        self.sock.listen(64)
        self.sock.settimeout(0.5)
        self._thread=threading.Thread(target=self._serve,name="stuckbot-egress",daemon=True)
        self._thread.start()
    def register_broker(self,provider:str,upstream:str,secret:str,header:str="Authorization",prefix:str="Bearer ",token:Optional[str]=None)->str:
        tok=token or ("sb-ephemeral-"+secrets.token_hex(16))
        self.brokers[provider.lower()]={"upstream":upstream.rstrip("/"),"secret":secret,"header":header,"prefix":prefix,"token":tok}
        self.tokens[tok]=secret
        return tok
    def broker_credentials(self,creds:Dict[str,str],secret_keys:Optional[Sequence[str]]=None,base_url:Optional[str]=None)->Dict[str,str]:
        out=dict(creds)
        burl=(base_url or f"http://127.0.0.1:{PROXY_PORT}").rstrip("/")
        skeys=set(secret_keys or [])
        for k,v in list(creds.items()):
            if not v:
                continue
            if k in skeys or re.search(r"(?i)(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH|PRIVATE_KEY)",k) or str(v).startswith(("sk-","sba_","sbe_","ghp_","gho_","github_pat_")):
                prov="anthropic" if k.upper().startswith(("ANTHROPIC_","CLAUDE_")) else ("openai" if k.upper().startswith("OPENAI_") else ("openrouter" if k.upper().startswith("OPENROUTER_") else re.sub(r"(?i)_(api_key|auth_token|token|secret|password|key)$","",k).lower()))
                def_up,hdr,pfx=DEFAULT_UPSTREAMS.get(prov,("https://api."+prov+".com","Authorization","Bearer "))
                if k.upper()=="ANTHROPIC_API_KEY":
                    hdr,pfx="x-api-key",""
                up_key=f"{prov.upper()}_BASE_URL"
                upstream=str(creds.get(up_key) or def_up)
                tok=self.register_broker(prov,upstream,str(v),hdr,pfx)
                out[k]=tok
                out[up_key]=f"{burl}/_broker/{prov}"
        return out
    def close(self)->None:
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self.sock.close()
            os.unlink(self.path)
        except OSError:
            pass
    def _serve(self):
        while not self._stop.is_set():
            try:
                c,_=self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._one,args=(c,),daemon=True).start()
    def decide(self,host:str,port:int)->Optional[str]:
        """The address to connect to, or None when the request is refused."""
        if port not in self.ports or not host_allowed(host,self.allow):
            return None
        try:
            ips=self.resolver(host,port)
        except OSError:
            return None
        if not ips:
            return None
        if not self.allow_private and any(_blocked_ip(i) for i in ips):
            return None
        return ips[0]
    def _deny(self,c:socket.socket,line:str,reason:str="egress_denied")->None:
        self.denied+=1
        self.log(reason,request=line[:200])
        try:
            c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        c.close()
    def _broker_one(self,c:socket.socket,line:str,parts:List[str],head:bytes,rest:bytes)->None:
        method=parts[0].upper()
        target=parts[1]
        if target.startswith(("http://","https://")):
            u=urllib.parse.urlsplit(target)
            target=u.path+(("?"+u.query) if u.query else "")
        m=re.match(r"^/_broker/([a-z0-9_-]+)(/.*)?$",target,re.I)
        if not m:
            self._deny(c,line)
            return
        prov=m.group(1).lower()
        subpath=m.group(2) or "/"
        b=self.brokers.get(prov)
        if not b:
            self._deny(c,line)
            return
        raw_lines=head.decode("latin-1").split("\r\n")[1:]
        tok=b["token"]
        if not any(tok in h for h in raw_lines):
            self._deny(c,line,"egress_broker_unauthenticated")
            return
        up=urllib.parse.urlsplit(b["upstream"])
        scheme=(up.scheme or "https").lower()
        host=(up.hostname or "").strip("[]")
        port=up.port or (443 if scheme=="https" else 80)
        ip=self.decide(host,port)
        if not ip:
            self._deny(c,line)
            return
        base_path=up.path.rstrip("/")
        full_path=(base_path+subpath) if subpath.startswith("/") else (base_path+"/"+subpath)
        out_headers=[]
        has_auth=False
        clen=0
        for h in raw_lines:
            if not h or ":" not in h:
                continue
            hk,hv=h.split(":",1)
            hkl=hk.strip().lower()
            if hkl in ("host","connection","proxy-connection","proxy-authorization"):
                continue
            if hkl=="content-length":
                try:
                    clen=int(hv.strip())
                except ValueError:
                    clen=0
            for etok,sec in self.tokens.items():
                if etok in hv:
                    hv=hv.replace(etok,sec)
            if hkl==b["header"].lower():
                has_auth=True
            out_headers.append(f"{hk.strip()}: {hv.strip()}")
        if not has_auth:
            out_headers.append(f"{b['header']}: {b['prefix']}{b['secret']}")
        host_hdr=host if (scheme=="https" and port==443) or (scheme=="http" and port==80) else f"{host}:{port}"
        out_headers.insert(0,f"Host: {host_hdr}")
        out_headers.append("Connection: close")
        while len(rest)<clen and len(rest)<10*1024*1024:
            try:
                d=c.recv(min(65536,clen-len(rest)))
                if not d:
                    break
                rest+=d
            except OSError:
                break
        try:
            u_sock=socket.create_connection((ip,port),timeout=10)
            if scheme=="https":
                ctx=ssl.create_default_context()
                u_sock=ctx.wrap_socket(u_sock,server_hostname=host)
        except OSError as e:
            self.log("egress_failed",host=host,port=port,error=str(e)[:200])
            try:
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            c.close()
            return
        self.allowed+=1
        self.log("egress_allowed",host=host,port=port,ip=ip,broker=prov)
        req_bytes=(f"{method} {full_path} HTTP/1.1\r\n"+"\r\n".join(out_headers)+"\r\n\r\n").encode("latin-1")+rest
        try:
            self.bytes_out+=len(req_bytes)
            u_sock.sendall(req_bytes)
            self._pipe(c,u_sock)
        except OSError:
            pass
        finally:
            c.close()
            u_sock.close()
    def _one(self,c:socket.socket):
        c.settimeout(15)
        buf=b""
        try:
            while b"\r\n\r\n" not in buf and len(buf)<8192:
                d=c.recv(4096)
                if not d:
                    c.close()
                    return
                buf+=d
        except OSError:
            c.close()
            return
        head,_,rest=buf.partition(b"\r\n\r\n")
        line=head.split(b"\r\n",1)[0].decode("latin-1")
        parts=line.split()
        if len(parts)==3 and parts[0].upper()!="CONNECT" and ("/_broker/" in parts[1]):
            self._broker_one(c,line,parts,head,rest)
            return
        host,port,ip="",0,None
        if len(parts)==3 and parts[0].upper()=="CONNECT":
            h,_,p=parts[1].rpartition(":")
            host=h.strip("[]")
            port=int(p) if p.isdigit() else 0
            ip=self.decide(host,port)
        if not ip:
            self._deny(c,line)
            return
        try:
            u=socket.create_connection((ip,port),timeout=10)
        except OSError as e:
            self.log("egress_failed",host=host,port=port,error=str(e)[:200])
            try:
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            c.close()
            return
        self.allowed+=1
        self.log("egress_allowed",host=host,port=port,ip=ip)
        try:
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                u.sendall(rest)
            self._pipe(c,u)
        except OSError:
            pass
        finally:
            c.close()
            u.close()
    def _pipe(self,a,b):
        a.settimeout(None)
        b.settimeout(None)
        while not self._stop.is_set():
            r,_,_=select.select([a,b],[],[],1.0)
            for s in r:
                d=s.recv(65536)
                if not d:
                    return
                if s is a:
                    self.bytes_out+=len(d)
                    b.sendall(d)
                else:
                    self.bytes_in+=len(d)
                    a.sendall(d)
class Sandbox:
    kind="none"
    def __init__(self,cfg:Dict[str,Any]):
        self.cfg=cfg
    def available(self)->Optional[str]:
        return None
    def wrap(self,argv:List[str],env:Dict[str,str],run_dir:str,ro:Sequence[str],work:Optional[str]=None)->List[str]:
        """The command line that runs argv in the sandbox. env is given to the process, never put on the command line, so other users cannot read credentials from the process list."""
        return argv
    def env(self,env:Dict[str,str],run_dir:str,proxy:bool=True)->Dict[str,str]:
        out=dict(env)
        out.setdefault("HOME",os.path.join(run_dir,"home"))
        if not proxy:
            for k in ("HTTPS_PROXY","https_proxy","HTTP_PROXY","http_proxy","ALL_PROXY","NO_PROXY","no_proxy"):
                out.pop(k,None)
        return out
    def paths(self,run_dir:str)->Dict[str,str]:
        return {"work":os.path.join(run_dir,"work"),"out":os.path.join(run_dir,"out"),"home":os.path.join(run_dir,"home")}
    def proxy_url(self)->Optional[str]:
        return None
class Bwrap(Sandbox):
    kind="bwrap"
    def available(self)->Optional[str]:
        if not shutil.which("bwrap"):
            return "bwrap (bubblewrap) is not installed"
        return None
    def paths(self,run_dir:str)->Dict[str,str]:
        return {"work":"/work","out":"/out","home":"/home/agent"}
    def proxy_url(self)->Optional[str]:
        return f"http://127.0.0.1:{PROXY_PORT}"
    def env(self,env:Dict[str,str],run_dir:str,proxy:bool=True)->Dict[str,str]:
        out=dict(env)
        out["HOME"]="/home/agent"
        out["USER"]="agent"
        out["TMPDIR"]="/tmp"
        if proxy:
            url=self.proxy_url()
            for k in ("HTTPS_PROXY","https_proxy","HTTP_PROXY","http_proxy","ALL_PROXY"):
                out[k]=url
            out["NO_PROXY"]=out["no_proxy"]="127.0.0.1,localhost"
        else:
            for k in ("HTTPS_PROXY","https_proxy","HTTP_PROXY","http_proxy","ALL_PROXY","NO_PROXY","no_proxy"):
                out.pop(k,None)
        return out
    def wrap(self,argv:List[str],env:Dict[str,str],run_dir:str,ro:Sequence[str],work:Optional[str]=None)->List[str]:
        uid=os.getuid()
        gid=os.getgid()
        etc=os.path.join(run_dir,"etc")
        os.makedirs(etc,exist_ok=True)
        with open(os.path.join(etc,"passwd"),"w") as f:
            f.write(f"agent:x:{uid}:{gid}:agent:/home/agent:/bin/sh\n")
        with open(os.path.join(etc,"group"),"w") as f:
            f.write(f"agent:x:{gid}:\n")
        with open(os.path.join(etc,"hosts"),"w") as f:
            f.write("127.0.0.1 localhost\n::1 localhost\n")
        with open(os.path.join(run_dir,"shim.py"),"w") as f:
            f.write(SHIM)
        cmd=["bwrap","--die-with-parent","--new-session","--unshare-all","--cap-drop","ALL",
             "--ro-bind","/usr","/usr","--proc","/proc","--dev","/dev","--tmpfs","/tmp","--tmpfs","/run","--dir","/var/tmp"]
        for d in ("bin","sbin","lib","lib64"):
            p="/"+d
            if os.path.islink(p):
                cmd+=["--symlink",os.readlink(p),p]
            elif os.path.isdir(p):
                cmd+=["--ro-bind",p,p]
        for p in ETC+list(self.cfg.get("ro_binds",[]))+list(ro):
            if os.path.exists(p):
                cmd+=["--ro-bind",p,p]
        for name in ("passwd","group","hosts"):
            cmd+=["--ro-bind",os.path.join(etc,name),"/etc/"+name]
        for d in ("out","home","sock"):
            os.makedirs(os.path.join(run_dir,d),mode=0o700,exist_ok=True)
        cmd+=["--bind",work or os.path.join(run_dir,"work"),"/work","--bind",os.path.join(run_dir,"out"),"/out","--bind",os.path.join(run_dir,"home"),"/home/agent",
              "--ro-bind",os.path.join(run_dir,"shim.py"),"/runner/shim.py","--bind",os.path.join(run_dir,"sock"),"/runner/sock",
              "--hostname","sandbox","--remount-ro","/","--chdir","/work"]
        python=self.cfg.get("python","/usr/bin/python3")
        return cmd+["--",python,"-I","/runner/shim.py",str(PROXY_PORT),"/runner/sock/egress.sock","--"]+argv
def make(cfg:Dict[str,Any])->Sandbox:
    kind=cfg.get("kind","bwrap")
    if kind=="bwrap":
        return Bwrap(cfg)
    if kind=="none":
        return Sandbox(cfg)
    raise ValueError("sandbox.kind must be bwrap or none")
