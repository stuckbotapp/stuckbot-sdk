"""ECDSA P-256 (ES256) signing for StuckBot verifier attestations, using only the standard library.

Signatures use deterministic nonces (RFC 6979), so a weak random source cannot leak the key.
This is plain Python big-integer arithmetic and is not constant-time: keep the key on a machine
you control, where other users cannot time its signing."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any,Dict,Optional,Tuple
P=0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
A=P-3
B=0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
N=0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
G=(0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)
_CURVE_OID=bytes.fromhex("06082a8648ce3d030107")
_SPKI_PREFIX=bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
def _add(p1,p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1,y1=p1
    x2,y2=p2
    if x1==x2:
        if (y1+y2)%P==0:
            return None
        m=(3*x1*x1+A)*pow(2*y1,-1,P)%P
    else:
        m=(y2-y1)*pow(x2-x1,-1,P)%P
    x3=(m*m-x1-x2)%P
    return (x3,(m*(x1-x3)-y1)%P)
def _mul(k,pt=G):
    out=None
    while k:
        if k&1:
            out=_add(out,pt)
        pt=_add(pt,pt)
        k>>=1
    return out
def _on_curve(pt)->bool:
    x,y=pt
    return 0<=x<P and 0<=y<P and (y*y-(x*x*x+A*x+B))%P==0
def _b(i:int)->bytes:
    return i.to_bytes(32,"big")
def b64url(data:bytes)->str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
def unb64url(s:str)->bytes:
    return base64.urlsafe_b64decode(s+"="*(-len(s)%4))
def _pem(label:str,der:bytes)->str:
    body=base64.b64encode(der).decode()
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n"%(label,"\n".join(body[i:i+64] for i in range(0,len(body),64)),label)
def _unpem(text:str)->Tuple[str,bytes]:
    lines=[l.strip() for l in text.strip().splitlines() if l.strip()]
    if len(lines)<3 or not lines[0].startswith("-----BEGIN ") or not lines[-1].startswith("-----END "):
        raise ValueError("not a PEM block")
    return lines[0][11:-5],base64.b64decode("".join(lines[1:-1]))
class SigningKey:
    """A P-256 private key. Make one with SigningKey.generate(), save it with to_pem(), load it with from_pem()."""
    def __init__(self,d:int):
        if not 1<=d<N:
            raise ValueError("private key out of range")
        self.d=d
        self.public=_mul(d)
    @classmethod
    def generate(cls)->"SigningKey":
        return cls(secrets.randbelow(N-1)+1)
    @classmethod
    def from_pem(cls,text:str)->"SigningKey":
        label,der=_unpem(text)
        if label not in ("EC PRIVATE KEY","PRIVATE KEY") or _CURVE_OID not in der:
            raise ValueError("expected a P-256 private key (EC PRIVATE KEY or PRIVATE KEY)")
        i=der.find(b"\x02\x01\x01\x04\x20")
        if i<0:
            raise ValueError("could not find the private key in the PEM")
        return cls(int.from_bytes(der[i+5:i+37],"big"))
    @classmethod
    def load(cls,path:str)->"SigningKey":
        with open(os.path.expanduser(path)) as f:
            return cls.from_pem(f.read())
    def to_pem(self)->str:
        x,y=self.public
        pub=b"\x00\x04"+_b(x)+_b(y)
        der=b"\x02\x01\x01\x04\x20"+_b(self.d)+b"\xa0\x0a"+_CURVE_OID+b"\xa1\x44\x03\x42"+pub
        return _pem("EC PRIVATE KEY",b"\x30"+bytes([len(der)])+der)
    def save(self,path:str)->None:
        path=os.path.expanduser(path)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w") as f:
            f.write(self.to_pem())
    def verifying_key(self)->"VerifyingKey":
        return VerifyingKey(self.public)
    def sign(self,message:bytes)->bytes:
        """Sign message with SHA-256 and return the 64-byte r||s signature."""
        h1=hashlib.sha256(message).digest()
        e=int.from_bytes(h1,"big")
        x=_b(self.d)
        h=_b(e%N)
        V=b"\x01"*32
        K=b"\x00"*32
        K=hmac.new(K,V+b"\x00"+x+h,hashlib.sha256).digest()
        V=hmac.new(K,V,hashlib.sha256).digest()
        K=hmac.new(K,V+b"\x01"+x+h,hashlib.sha256).digest()
        V=hmac.new(K,V,hashlib.sha256).digest()
        while True:
            V=hmac.new(K,V,hashlib.sha256).digest()
            k=int.from_bytes(V,"big")
            if 1<=k<N:
                r=_mul(k)[0]%N
                s=pow(k,-1,N)*(e+r*self.d)%N
                if r and s:
                    return _b(r)+_b(s)
            K=hmac.new(K,V+b"\x00",hashlib.sha256).digest()
            V=hmac.new(K,V,hashlib.sha256).digest()
class VerifyingKey:
    """A P-256 public key, as registered under Policy, Your verifiers."""
    def __init__(self,point:Tuple[int,int]):
        if not _on_curve(point):
            raise ValueError("point is not on P-256")
        self.point=point
    @classmethod
    def from_pem(cls,text:str)->"VerifyingKey":
        label,der=_unpem(text)
        if label!="PUBLIC KEY" or not der.startswith(_SPKI_PREFIX) or len(der)!=len(_SPKI_PREFIX)+65:
            raise ValueError("expected a P-256 PUBLIC KEY")
        raw=der[len(_SPKI_PREFIX)+1:]
        return cls((int.from_bytes(raw[:32],"big"),int.from_bytes(raw[32:],"big")))
    def der(self)->bytes:
        return _SPKI_PREFIX+b"\x04"+_b(self.point[0])+_b(self.point[1])
    def to_pem(self)->str:
        return _pem("PUBLIC KEY",self.der())
    def fingerprint(self)->str:
        """SHA-256 of the key in DER form, as shown by StuckBot and sent as attestation.key."""
        return hashlib.sha256(self.der()).hexdigest()
    def verify(self,message:bytes,signature:bytes)->bool:
        if len(signature)!=64:
            return False
        r=int.from_bytes(signature[:32],"big")
        s=int.from_bytes(signature[32:],"big")
        if not (1<=r<N and 1<=s<N):
            return False
        e=int.from_bytes(hashlib.sha256(message).digest(),"big")
        w=pow(s,-1,N)
        pt=_add(_mul(e*w%N),_mul(r*w%N,self.point))
        return pt is not None and pt[0]%N==r
def attestation(key:SigningKey,verification_id:str,ticket_id:str,criterion_key:str,stage:str,type:str,payload:Dict[str,Any],signed_at:Optional[float]=None)->Dict[str,str]:
    """Build the attestation object StuckBot expects on a verifier's evidence item."""
    statement=json.dumps({
        "v":1,
        "verification_id":verification_id,
        "ticket_id":ticket_id,
        "criterion_key":criterion_key,
        "stage":stage,
        "type":type,
        "payload":payload,
        "signed_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(signed_at)),
    },separators=(",",":"),sort_keys=True).encode()
    return {
        "key":key.verifying_key().fingerprint(),
        "statement":b64url(statement),
        "signature":b64url(key.sign(statement)),
    }
def check_attestation(public:VerifyingKey,att:Dict[str,Any])->Optional[Dict[str,Any]]:
    """Return the signed statement if att carries a valid signature by public, else None."""
    if att.get("key")!=public.fingerprint() or not att.get("statement") or not att.get("signature"):
        return None
    raw=unb64url(att["statement"])
    if not public.verify(raw,unb64url(att["signature"])):
        return None
    return json.loads(raw)
