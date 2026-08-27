# Approval Policy

| Gate | Approval object |
|---|---|
| G0 | iCafe card and project binding |
| G1 | each human decision from `tom-grill` or architecture Review |
| G2 | Spec, test interface, environment requirements |
| G3 | end-to-end Task DAG |
| G4 | each task's `Task Plan` |
| G5 | each task's complete candidate diff |
| G6 | each repair diagnosis, repair plan, and repair diff |
| G7 | iCode submission or patchset |
| G8 | iPipe failed-stage rerun or manual-stage continuation |
| G9 | release evidence and release action |

Publish the same `approval_id`, evidence summary, and `input_hash` to Comate and Infoflow. Accept the first valid response. Audit later responses without changing the effective decision. Expire an approval whenever its bound input changes. Default wait limit is ten hours; timeout stops the run.
