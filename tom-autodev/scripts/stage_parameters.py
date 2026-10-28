"""Resolve the parameters a manual iPipe stage asks a person to type.

Historical BGW `P0新case回归` inputs map to the QA and bgwagent build products.
Only platform stage/job/dependency declarations decide which names are needed.
Products follow the frozen changed policy (branch release or exact candidate compile), so asking a person
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
    tokens = {
        str(module): str(token)
        for module, token in loaded.items()
        if isinstance(token, str) and token.strip()
    }
    # Every loaded value is redacted by exact match wherever it would be persisted
    # (observations, receipts, approval material), whatever its length or shape.
    from persistence_policy import register_secret
    for token in tokens.values():
        register_secret(token)
    return tokens


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


def declared_names(stage: dict[str, Any]) -> dict[str, Any]:
    """Only explicit manualParams, including explicit no-dynamic-inputs, are evidence."""
    if "manualParams" not in stage or stage.get("hasDynamicParams") is True:
        return {"ok": False, "reason_code": "STAGE_PARAMETERS_UNKNOWN"}
    params = stage["manualParams"]
    if params is None:
        if stage.get("hasDynamicParams") is not False:
            return {"ok": False, "reason_code": "STAGE_PARAMETERS_UNKNOWN"}
        params = []
    if isinstance(params, str):
        import json
        try:
            params = json.loads(params)
        except ValueError:
            return {"ok": False, "reason_code": "STAGE_PARAMETERS_UNKNOWN"}
    if isinstance(params, dict):
        names = list(params)
    elif isinstance(params, list):
        names = [item if isinstance(item, str) else item.get("name")
                 if isinstance(item, dict) else None for item in params]
    else:
        names = [None]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        return {"ok": False, "reason_code": "STAGE_PARAMETERS_UNKNOWN"}
    if len(names) != len(set(names)):
        return {"ok": False, "reason_code": "STAGE_PARAMETERS_UNKNOWN"}
    return {"ok": True, "reason_code": "OK", "names": sorted(names)}


def dependency_declaration(stage: dict[str, Any], detail: dict[str, Any],
                           configuration: dict[str, Any]) -> dict[str, Any]:
    """Discover stage/job inputs and inherited declarations without archiving defaults.

    Empty manualParams describes the UI, not shell environment dependencies.
    Configuration is live evidence, never silently treated as a build snapshot.
    """
    stages = configuration.get("stages") or []
    selected = [s for s in stages if isinstance(s, dict)
                and str(s.get("stageConfId")) == str(stage.get("stageConfId"))]
    if not selected and any(isinstance(s, dict) for s in stages):
        # Contract 3.3: the build's own snapshot decides which stages exist. The live
        # configuration lost this stage after the build started (BGW-1995: QA removed
        # 系统测试功能回归 from 348101 mid-run), so the build's explicit declaration is
        # the only one there is. An explicit empty one (manualParams null +
        # hasDynamicParams false) is parameterless; anything else stays unknown.
        own = declared_names(stage)
        if own.get("ok") and not own["names"]:
            return {"ok": True, "reason_code": "OK", "names": [],
                    "requires_job_override": False, "declarations": {},
                    "dependency_chain": [], "configuration_source": "build_snapshot",
                    "config_stage_missing": True}
        return {"ok": False, "reason_code": "STAGE_PARAMETERS_UNKNOWN",
                "config_stage_missing": True}
    if len(selected) != 1:
        return {"ok": False, "reason_code": "STAGE_PARAMETERS_UNKNOWN"}
    conf = selected[0]
    if conf.get("stageName") != stage.get("stageName"):
        return {"ok": False, "reason_code": "STAGE_PARAMETER_IDENTITY_MISMATCH"}
    jobs = [j for group in conf.get("groupJobs", []) for j in group if isinstance(j, dict)]
    built = [j for group in detail.get("realJobBuilds", []) for j in group if isinstance(j, dict)]
    if {str(j.get("jobConfId")) for j in jobs} != {
        str(j.get("jobConfId")) for j in built
    }:
        return {"ok": False, "reason_code": "STAGE_PARAMETER_IDENTITY_MISMATCH"}
    manual = declared_names(stage)
    if not manual.get("ok"):
        return manual
    declarations: dict[str, list[dict[str, Any]]] = {}

    def collect(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for p in value.get("paramBeans") or []:
                if isinstance(p, dict) and isinstance(p.get("name"), str):
                    declarations.setdefault(p["name"], []).append({
                        "source": path + ".paramBeans", "scope": path,
                        "upStageParam": p.get("upStageParam"),
                    })
            inputs = value.get("inParamsMap") or {}
            if isinstance(inputs, dict):
                for name in inputs:
                    if isinstance(name, str):
                        declarations.setdefault(name, []).append({
                            "source": path + ".inParamsMap", "scope": path})
            for key, item in value.items():
                if key not in {"paramBeans", "command", "defaultValue", "inParamsMap"}:
                    collect(item, path + "." + key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                collect(item, f"{path}[{index}]")

    # Only follow the selected stage's declared upstream chain.
    chain = []
    current = conf
    seen = set()
    while current:
        identity = str(current.get("stageConfId"))
        if identity in seen:
            return {"ok": False, "reason_code": "STAGE_DEPENDENCY_INVALID"}
        seen.add(identity)
        chain.append(identity)
        collect(current, "stage:" + identity)
        upstream = current.get("upstreamStageId")
        matches = [s for s in stages if str(s.get("stageConfId")) == str(upstream)]
        if upstream not in (None, 0, -1, "", "0", "-1") and len(matches) != 1:
            return {"ok": False, "reason_code": "STAGE_DEPENDENCY_INVALID"}
        current = matches[0] if matches else None
    references = set()
    for job in jobs:
        command = ((job.get("realJob") or {}).get("shellConf") or {}).get("command", "")
        references.update(name for name in re.findall(
            r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", command)
            if name in declarations or name.startswith("get_") or name == "test_cr_id")
    needed = set(manual["names"])
    needed.update(p.get("name") for p in conf.get("paramBeans", []) if p.get("name"))
    needed.update(references)
    unknown = needed - set(declarations) - set(manual["names"])
    if unknown:
        return {"ok": False, "reason_code": "STAGE_DEPENDENCY_INPUT_UNDECLARED",
                "missing_parameters": sorted(unknown)}
    return {"ok": True, "reason_code": "OK", "names": sorted(needed),
            "requires_job_override": bool(needed - set(manual["names"])),
            "declarations": {n: declarations.get(n, [{"source": "manualParams"}])
                             for n in sorted(needed)},
            "dependency_chain": chain, "configuration_source": "live_pipeline_conf"}


def _is_upstream_param(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def job_scope_names(declaration: dict[str, Any], stage_conf_id: Any) -> list[str]:
    """Names the stage's own manual form cannot carry.

    `dependency_declaration` marks `requires_job_override` whenever a needed name is not
    in `manualParams`, which includes names the stage itself declares in its own
    `paramBeans`/`inParamsMap`. Those are filled through the stage's manual-execute form
    like any other manual input. Only a name declared by another stage (an inherited
    declaration), by a nested job, or flagged `upStageParam` needs a job-level override.

    Fails closed: a declaration that cannot be read, or a name with no scope evidence,
    is reported as job-level.
    """
    names = declaration.get("names")
    declarations = declaration.get("declarations")
    if not isinstance(names, list) or not isinstance(declarations, dict):
        return sorted(str(name) for name in names) if isinstance(names, list) else ["*"]
    own_scope = f"stage:{stage_conf_id}"
    job_level = []
    for name in names:
        entries = declarations.get(name)
        if not isinstance(entries, list) or not entries:
            job_level.append(name)
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                job_level.append(name)
                break
            if entry.get("source") == "manualParams" and "scope" not in entry:
                continue
            if entry.get("scope") == own_scope and not _is_upstream_param(entry.get("upStageParam")):
                continue
            job_level.append(name)
            break
    return sorted(job_level)


def resolve(
    names: list[str],
    *,
    change_number: str | None,
    product_urls: dict[str, str],
    tokens: dict[str, str],
    mapping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Values for declared parameters, or every input that cannot be answered."""
    if not isinstance(names, list):
        return {"ok": False, "reason_code": "STAGE_PARAMETERS_UNKNOWN"}
    values: dict[str, str] = {}
    missing = []
    for name in names:
        rule = (mapping or {}).get(name)
        if mapping is None and name == "test_cr_id":
            rule = "test_cr_id"
        if rule == "test_cr_id":
            if not change_number:
                missing.append(_unresolved(name, "SUBMISSION_RECEIPT_REQUIRED"))
                continue
            values[name] = str(change_number)
            continue
        module = rule if isinstance(rule, str) else None
        if mapping is None:
            module = _module_of(name, product_urls)
        if module is None:
            missing.append(_unresolved(name, "PARAMETER_NOT_DERIVABLE"))
            continue
        url = product_urls.get(module)
        token = tokens.get(module)
        if not url:
            missing.append(_unresolved(name, "PRODUCT_URL_REQUIRED", module=module))
            continue
        if not token:
            missing.append(_unresolved(name, "IREPO_TOKEN_REQUIRED", module=module))
            continue
        values[name] = product_command(url, token)
    if missing:
        return {**missing[0], "missing_parameters": missing}
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


_BGW_MANUAL_STAGE_NAMES = {
    "P0新case回归", "P0新case回归测试", "p0级自动化回归测试",
    # BranchPipeline 348101 (release path): declares `get_bgwagent` in the stage's own
    # paramBeans/inParamsMap; its job runs `eval ${get_bgwagent}`, so it cannot be empty.
    "自动化回归测试",
}


def project_mapping(profile: dict[str, Any], stage_name: str) -> dict[str, Any]:
    """Exact stage-scoped mappings; profile entries override bundled project facts."""
    mapping = {}
    if profile.get("project_id") == "bgw" and stage_name in _BGW_MANUAL_STAGE_NAMES:
        mapping.update({name: modules[0] for name, modules in _PARAMETER_ALIASES.items()})
    configured = (profile.get("pipeline_profile") or {}).get("stage_parameter_mapping", {})
    if isinstance(configured, dict) and isinstance(configured.get(stage_name), dict):
        mapping.update(configured[stage_name])
    return mapping


def known_job_override_names(profile: dict[str, Any], stage_name: str) -> set[str]:
    """Project-knowledge names a job may reference that are NOT a security hole to allow.

    `project_mapping` is itself the authoritative, human-reviewed record of which parameter
    names are legitimate BGW manual-stage form fields (`get_bgw_test_case`, `get_bgwagent`),
    carried over from the historical `P0新case回归` stage and now also recorded for the
    `p0级自动化回归测试` stage used by the current BGW P0 automation run. Reusing exactly this
    mapping -- rather than inventing a second list -- means there is only one place that
    decides "this name is a known legitimate BGW form field", so the two call sites (deciding
    which names may bypass the override block, and deciding what value to assemble) can never
    disagree about what is whitelisted.
    """
    return set(project_mapping(profile, stage_name))


def job_override_decision(
    declaration: dict[str, Any], stage_conf_id: Any, profile: dict[str, Any] | None = None,
    *, manual_names: Any = None, capability: Any = None,
) -> dict[str, Any]:
    """The one answer to "can this stage's parameters be executed?".

    Used both when the G8 material is prepared (`IpipeRuntime.stage_parameter_names`)
    and when the binding is hashed or written (`IpipeRuntime._rerun_binding`), so the
    card can never show a stage as executable that the write then refuses.

    1. A name inherited from another stage, declared by a nested job or flagged
       `upStageParam` (`job_scope_names`) needs a job-level override. No such write
       exists, so the stage is not executable -- whether or not the name is recorded
       project knowledge and whatever the platform's override capability says.
    2. A name the stage declares itself but that is not in its `manualParams` form is
       executable through `manual_execute_stage` only when it is recorded project
       knowledge (`known_job_override_names`) or the platform proves an override
       contract (`capability()` returns supported + evidence_ref + contract_hash).
       Any other name stays blocked, so an unknown job variable never passes.
    """
    if not declaration.get("requires_job_override"):
        return {"executable": True, "job_level": [], "allowlisted": [], "unknown": []}
    chain = declaration.get("dependency_chain") or []
    conf_id = chain[0] if chain else stage_conf_id
    job_level = job_scope_names(declaration, conf_id)
    known = known_job_override_names(profile or {}, str(declaration.get("stage_name") or ""))
    decision: dict[str, Any] = {
        "executable": not job_level,
        "job_level": job_level,
        "allowlisted": sorted(name for name in job_level if name in known),
        "unknown": [],
    }
    if job_level:
        return decision
    form = set(manual_names or [])
    extra = {name for name in declaration.get("names") or [] if name not in form}
    decision["allowlisted"] = sorted(extra & known)
    unknown = sorted(extra - known)
    if unknown:
        proven = capability() if callable(capability) else None
        decision["override_capability"] = proven
        if not (isinstance(proven, dict) and proven.get("supported")
                and proven.get("evidence_ref") and proven.get("contract_hash")):
            decision.update(executable=False, unknown=unknown)
    return decision


def _module_of(name: str, product_urls: dict[str, str]) -> str | None:
    """Legacy exact aliases only; orchestration supplies a stage-scoped mapping."""
    for alias, modules in _PARAMETER_ALIASES.items():
        if alias == name:
            for module in modules:
                if module in product_urls:
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
