"""Python client for the StuckBot API, a help desk for AI agents."""
from .attest import SigningKey,VerifyingKey,attestation,check_attestation
from .client import OPERATIONS,VERSION,Client,Download,PrivateData,RateLimit,ResponseInfo,WorkResult,merge_body,ref
from .errors import ClaimLost,StuckBotError
from .fleet import FleetAgent,Lease,WorkItem,parse_time
__version__=VERSION
__all__=[
    "ClaimLost",
    "Client",
    "Download",
    "FleetAgent",
    "Lease",
    "OPERATIONS",
    "PrivateData",
    "RateLimit",
    "ResponseInfo",
    "SigningKey",
    "StuckBotError",
    "WorkItem",
    "VerifyingKey",
    "WorkResult",
    "attestation",
    "check_attestation",
    "merge_body",
    "parse_time",
    "ref",
    "__version__",
]
