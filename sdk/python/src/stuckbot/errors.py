import json
import time
from email.utils import parsedate_to_datetime
def parse_retry_after(value):
    if value is None or value=="":
        return None
    try:
        return max(0.0,float(value))
    except ValueError:
        pass
    try:
        return max(0.0,parsedate_to_datetime(value).timestamp()-time.time())
    except (TypeError,ValueError,IndexError):
        return None
class StuckBotError(Exception):
    """An error answer from the StuckBot API, or a network failure (status 0)."""
    def __init__(self,status,code,message,request_id=None,details=None,retry_after=None,headers=None):
        super().__init__(message)
        self.status=status
        self.code=code
        self.message=message
        self.request_id=request_id
        self.details=details or {}
        self.retry_after=retry_after
        self.headers=headers or {}
    def __str__(self):
        rid=f" [request_id {self.request_id}]" if self.request_id else ""
        return f"{self.code} ({self.status}): {self.message}{rid}"
    def __repr__(self):
        return f"{type(self).__name__}(status={self.status!r}, code={self.code!r}, message={self.message!r}, request_id={self.request_id!r})"
    @classmethod
    def from_response(cls,status,headers,raw):
        """Build the error from a non-2xx answer."""
        err={}
        try:
            body=json.loads(raw.decode("utf-8")) if raw else None
            if isinstance(body,dict) and isinstance(body.get("error"),dict):
                err=body["error"]
        except (ValueError,UnicodeDecodeError):
            pass
        rid=err.get("request_id") or headers.get("x-request-id")
        code=err.get("code") or f"http_{status}"
        message=err.get("message") or f"The StuckBot API answered HTTP {status}."
        details=err.get("details") if isinstance(err.get("details"),dict) else {}
        return cls(status,code,message,rid,details,parse_retry_after(headers.get("retry-after")),headers)
CLAIM_LOST_CODES=("already_claimed","invalid_transition","ticket_paused")
class ClaimLost(StuckBotError):
    """A claim or continue answered 409 because another solver got the ticket first, or it is paused or no longer in a state you can take."""
    @classmethod
    def of(cls,e):
        """ClaimLost for a 409 from claim_ticket or continue_task that means the ticket is no longer yours to take, else None."""
        if e.status==409 and e.code in CLAIM_LOST_CODES:
            return cls(e.status,e.code,e.message,e.request_id,e.details,e.retry_after,e.headers)
        return None
