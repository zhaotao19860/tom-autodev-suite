# NPL reference index

Read only the smallest source needed for the current decision.

| Question | Read | Authority / use |
|---|---|---|
| Is a construct legal? | [NPL Specification 1.5.1](npl-docs/NPL_Specification.1.5.1.md), then [Coding Guidelines](npl-docs/NPL_Coding_Guidelines_Baidu.md) | normative language and compiler-assist rules |
| How should an existing diagnostic be interpreted? | [compile diagnosis](npl-compile-diagnostics.md), then the relevant heading in [fixup examples](npl-docs/NPL_Compilation_Error_Fixup_Examples_Baidu.md) | current routing plus versioned example evidence |
| How is behavior observed? | [simulator evidence](simulator-evidence.md) | extracted NPLSIM model/capability checks |
| What does a special function or hardware object mean? | the target profile's approved chip document and current SF declaration | target evidence; do not infer chip-specific interfaces from language syntax |

Useful searches in the bundled specification: `Assignment operators`, `SPECIFY A STRUCT WITH OVERLAYS`, `Overlay Rules`, `Function`, `add_header Construct`, `Strength Resolve`. For compiler cases search the exact component/diagnostic before reading adjacent cases. Markdown conversion can distort code or tables; when a fragment is ambiguous, require the approved source document or matching current source before generating code from it.

Packaging limitation: the existing specification snapshot references an unbundled architecture figure (`_page_12_Figure_3.jpeg` in §3.2). Do not infer details from the unavailable image; use the adjacent prose or the profile's approved original document if the figure is necessary. The imported manual was kept unchanged.

Do not copy addresses, container IDs, binary hashes, compiler images, or shell commands from archived examples. Current repository, revision, iPipe profile, and environment evidence come from `tom-project-xflow` (or another caller-supplied project profile).
