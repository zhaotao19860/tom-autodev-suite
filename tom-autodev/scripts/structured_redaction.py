"""Canonical control-plane redaction of secrets, emails and phone numbers.

Moved out of `run_summary` when G10 (post-run optimization) was removed; `preflight`
uses it for structured external diagnostics.
"""

from __future__ import annotations

import json
import re
from typing import Any

_SECRET_NAME = r"authorization|api[\s_-]*key|token|credential|password|secret|access[_-]?key"
_QUOTED_SECRET = re.compile(r"(?ix)(?P<prefix>[\"']?(?:" + _SECRET_NAME + r")[\"']?\s*[:=]\s*)(?P<quote>[\"'])(?P<value>(?:\\.|(?!\2).)*)\2")
_BARE_SECRET = re.compile(r"(?ix)(?P<prefix>\b(?:bearer\s+|authorization\s*[:=]\s*(?:bearer\s+)?|api[\s_-]*key\s*[:=]\s*|token\s*[:=]?\s*|credential\s*[:=]\s*|password\s*[:=]\s*|secret\s*[:=]\s*))(?P<value>[^\s,;]+)")
_URL_SECRET = re.compile(r"(?ix)(?P<prefix>(?://[^/\s:@]+:)(?P<value>[^@\s/]+)(?=@)|(?P<query>[?&](?:" + _SECRET_NAME + r")=)(?P<qvalue>[^&#\s]+))")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")


def _redact(v:str)->str:
    try:
        structured=json.loads(v)
    except (TypeError, ValueError, json.JSONDecodeError):
        structured=None
    if structured is not None:
        return json.dumps(_redact_structured(structured), ensure_ascii=False, separators=(",", ":"))
    return _redact_text(v)

def _redact_structured(value:Any)->Any:
    if isinstance(value,dict):
        return {key:("[REDACTED_SECRET]" if isinstance(key,str) and re.search(_SECRET_NAME,key,re.I) else _redact_structured(child)) for key,child in value.items()}
    if isinstance(value,list): return [_redact_structured(item) for item in value]
    return _redact_text(value) if isinstance(value,str) else value

def redact_structured(value:Any)->Any:
    """Canonical control-plane redaction for structured external diagnostics."""
    return _redact_structured(value)

def _redact_text(v:str)->str:
    def quoted(match:re.Match[str])->str: return match.group("prefix") + match.group("quote") + "[REDACTED_SECRET]" + match.group("quote")
    def bare(match:re.Match[str])->str: return match.group("prefix") + "[REDACTED_SECRET]"
    def url(match:re.Match[str])->str:
        if match.group("query"): return match.group("query") + "[REDACTED_SECRET]"
        return match.group("prefix").split(":",1)[0] + "://[REDACTED_SECRET]"
    value=_QUOTED_SECRET.sub(quoted,v)
    value=_URL_SECRET.sub(url,value)
    value=_BARE_SECRET.sub(bare,value)
    return _PHONE.sub("[REDACTED_PHONE]",_EMAIL.sub("[REDACTED_EMAIL]",value))
def _redacted(v:Any)->Any:return _redact(v) if isinstance(v,str) else ([_redacted(x) for x in v] if isinstance(v,list) else ({k:_redacted(x) for k,x in v.items()} if isinstance(v,dict) else v))
def _has_secret(v:str)->bool:
    try:
        structured=json.loads(v)
    except (TypeError, ValueError, json.JSONDecodeError):
        structured=None
    if structured is not None and _structured_has_secret(structured): return True
    return bool(_QUOTED_SECRET.search(v) or _BARE_SECRET.search(v) or _URL_SECRET.search(v))

def _structured_has_secret(value:Any)->bool:
    if isinstance(value,dict): return any((isinstance(key,str) and re.search(_SECRET_NAME,key,re.I)) or _structured_has_secret(child) for key,child in value.items())
    if isinstance(value,list): return any(_structured_has_secret(item) for item in value)
    return isinstance(value,str) and bool(_URL_SECRET.search(value))
