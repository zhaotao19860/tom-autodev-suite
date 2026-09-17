"""Resolve the parameters a manual iPipe stage asks a person to type.

BGW's `P0新case回归` stage takes two: the QA repository's CR id, and a wget command for
the bgwagent build product. Both are facts this control plane already holds -- the CR id
is in the submission receipt, the product URL is on the compile job -- so asking a person
to copy them in is a place for a typo and a reason the stage cannot be driven
automatically.

The product download needs an irepo token, which is the one part that cannot be derived:
iPipe's API does not expose it (every product/token endpoint answers OBJECT_NOT_FOUND),
and the pipeline-internal token service at 10.144.223.34:8006 is only reachable from
agent networks. It is a per-repository, long-lived credential copied once from iPipe's
release records, so it lives in a 0600 file and is read from there.

Nothing here ever puts a token in a return value that gets archived: `resolve` returns the
values to send, and `redacted` returns the same mapping with every token replaced, which
is what goes into evidence.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_TOKEN_FILE = "credentials/irepo-tokens.yaml"
_REDACTED = "<IREPO-TOKEN>"
# Redact by the token's known LOCATION, not its shape: product_command always embeds the
# credential as `--header "IREPO-TOKEN:<token>"`, so scrub everything from `IREPO-TOKEN:`
# up to the closing quote. A shape-based (UUID-only) rule leaked any hex/JWT/opaque token
# verbatim into the G8 approval binding and evidence.
_TOKEN_HEADER = re.compile(r'(IREPO-TOKEN:)[^"]*')


def load_tokens(config_root: Path | str) -> dict[str, str]:
    """Per-module irepo tokens, or an empty mapping when none are configured.

    A world-readable credential file is refused rather than used: a token that anyone on
    the machine can read is not a secret, and silently accepting it would hide that.
    """
    import yaml

    path = Path(config_root).expanduser() / _TOKEN_FILE
    if not path.is_file():
        return {}
    if path.stat().st_mode & 0o077:
        raise ValueError("IREPO_TOKEN_FILE_PERMISSIONS")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    return {
        str(module): str(token)
        for module, token in loaded.items()
        if isinstance(token, str) and token.strip()
    }


def product_command(url: str, token: str) -> str:
    """The download the stage expects, in the only form irepo accepts.

    Measured on the BGW product node: `--user getprod --password getprod` against the
    irepo REST path fails authentication (wget exit 6) and the scm path is unreachable
    (exit 4); the `IREPO-TOKEN` header succeeds (exit 0, a gzip archive). So this is not a
    stylistic choice.
    """
    return (
        'wget -O output.tar.gz --no-check-certificate '
        f'--header "IREPO-TOKEN:{token}" "{url}"'
    )


def resolve(
    names: list[str],
    *,
    change_number: str | None,
    product_urls: dict[str, str],
    tokens: dict[str, str],
) -> dict[str, Any]:
    """Values for the named parameters, or the first name that cannot be answered."""
    if not isinstance(names, list) or not names:
        return {"ok": False, "reason_code": "STAGE_PARAMETERS_UNKNOWN"}
    values: dict[str, str] = {}
    for name in names:
        lowered = str(name).lower()
        if "cr_id" in lowered or "cr-id" in lowered:
            if not change_number:
                return _unresolved(name, "SUBMISSION_RECEIPT_REQUIRED")
            values[name] = str(change_number)
            continue
        module = _module_of(lowered, product_urls)
        if module is None:
            return _unresolved(name, "PARAMETER_NOT_DERIVABLE")
        url = product_urls.get(module)
        token = tokens.get(module)
        if not url:
            return _unresolved(name, "PRODUCT_URL_REQUIRED", module=module)
        if not token:
            return _unresolved(name, "IREPO_TOKEN_REQUIRED", module=module)
        values[name] = product_command(url, token)
    return {"ok": True, "reason_code": "OK", "parameters": values}


def redacted(values: dict[str, Any]) -> dict[str, Any]:
    """The same mapping with every embedded irepo token replaced, for evidence."""
    return {
        key: _TOKEN_HEADER.sub(r"\1" + _REDACTED, value) if isinstance(value, str) else value
        for key, value in (values or {}).items()
    }


_PARAMETER_ALIASES = {
    "get_bgw_test_case": ("baidu/nsiqa/x86bgw",),
    "get_bgwagent": ("baidu/sysip/bgwagent",),
}


def _module_of(lowered: str, product_urls: dict[str, str]) -> str | None:
    """Which module a parameter is about, by alias or the repository name it mentions."""
    for name, modules in _PARAMETER_ALIASES.items():
        if name == lowered or name in lowered:
            for module in modules:
                if module in product_urls:
                    return module
    for module in product_urls:
        if os.path.basename(module).lower() in lowered:
            return module
    return None


def _unresolved(name: str, reason: str, **detail: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "reason_code": "STAGE_PARAMETER_UNRESOLVED",
        "parameter": name,
        "detail": reason,
        **detail,
    }
