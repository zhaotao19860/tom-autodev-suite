# BGW Knowledge and Evidence

- Business repository revision is the final code fact.
- GitNexus is an optional impact map. When missing or stale, use source/caller/configuration search and record the limitation.
- Independent product tests assert the external BGW boundary and retain deterministic fixtures.
- A passing result is valid only when business revisions, test revision, test plan hash, iPipe build, and environment fingerprint match the approved Change Set.
- New failure patterns become knowledge candidates and require human approval before write-back.
- Product-case diagnosis starts from the live topology and log order in `runtime-topology.md`: iPipe job log, client `out`/`res.csv`, then `/var/log/messages`.
- A control-plane RST with `Wrong message magic number: 20210705` means the client `tool/bgwagent` is the QA-tarball vintage, not a NAT64/offer-hash assertion failure.
