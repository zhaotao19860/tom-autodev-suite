# Diagnose Phase Contract

Input is a frozen failure evidence bundle (stage/job, logs, revisions, diff, Spec/Plan, environment fingerprint, and repair history). Output is a `diagnosis` envelope with reproducibility from remote evidence, comparison to a passing revision, one falsifiable root-cause hypothesis, failure class, minimal iPipe verification, confidence, and route. Persist to KU and comment iCafe through the parent; G6 approval is required for repairs. Stop with `DIAGNOSIS_INCOMPLETE` when evidence is insufficient; environment failures never create business patches.
