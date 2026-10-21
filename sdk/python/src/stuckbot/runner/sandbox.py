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
import select
import shutil
import socket
import threading
from typing import Any,Callable,Dict,List,Optional,Sequence
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
    """A CONNECT-only proxy on a Unix socket that allows listed hosts on port 443 and logs every attempt."""
    def __init__(self,path:str,allow:Sequence[str],log:Callable[...,None],ports:Sequence[int]=(443,),allow_private:bool=False,resolver:Callable[[str,int],List[str]]=None):
        self.path=path
        self.allow=list(allow)
        self.ports=set(ports)
        self.allow_private=allow_private
        self.log=log
        self.resolver=resolver or (lambda h,p:[i[4][0] for i in socket.getaddrinfo(h,p,type=socket.SOCK_STREAM)])
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
        host,port,ip="",0,None
        if len(parts)==3 and parts[0].upper()=="CONNECT":
            h,_,p=parts[1].rpartition(":")
            host=h.strip("[]")
            port=int(p) if p.isdigit() else 0
            ip=self.decide(host,port)
        if not ip:
            self.denied+=1
            self.log("egress_denied",request=line[:200])
            try:
                c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            c.close()
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
    def env(self,env:Dict[str,str],run_dir:str)->Dict[str,str]:
        out=dict(env)
        out.setdefault("HOME",os.path.join(run_dir,"home"))
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
    def env(self,env:Dict[str,str],run_dir:str)->Dict[str,str]:
        out=dict(env)
        out["HOME"]="/home/agent"
        out["USER"]="agent"
        out["TMPDIR"]="/tmp"
        url=self.proxy_url()
        for k in ("HTTPS_PROXY","https_proxy","HTTP_PROXY","http_proxy","ALL_PROXY"):
            out[k]=url
        out["NO_PROXY"]=out["no_proxy"]=""
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
