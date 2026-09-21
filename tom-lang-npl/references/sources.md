# NPL source provenance

Merged on 2026-09-21 from the local `npl-coder` source package. Names below identify source material at import time; they are provenance, not instructions to locate or execute an external skill. Runtime guidance is contained in this skill and the caller's project profile. Existing document snapshots were preserved unchanged.

| Source in that package | Imported scope / authority | SHA-256 of source bytes |
|---|---|---|
| `references/docs/npl/NPL_Specification.1.5.1.md` | bundled [specification](npl-docs/NPL_Specification.1.5.1.md); normative language, including §4.2.5, Assignment operators, §9.3 | `4552a3897947ffe0813f041547ac4147dd49ce9527fd7f918b46bea61c5dfca0` |
| `references/docs/npl/NPL_Coding_Guidelines_Baidu.md` | bundled [guidelines](npl-docs/NPL_Coding_Guidelines_Baidu.md); overlay and compiler-assist directives | `1023c12aac387c392db0b9758d015ffcf6bf3fdf23fdcdfb165d5de5464082aa` |
| `references/docs/npl/NPL_Compilation_Error_Fixup_Examples_Baidu.md` | bundled [cases](npl-docs/NPL_Compilation_Error_Fixup_Examples_Baidu.md); compiler-version-specific observations, especially §6.3's tentative 4-bit explanation | `d2585810185fa0ee0c3b45e23e32da4ddf59fd55dd99bbe3fb51fb8908466889` |
| `references/docs/npl/NPL_Debug_User_Guide_Baidu.md` | §2 and §2.6 summarized in [simulator evidence](simulator-evidence.md); source guide's model/capability scope | `4ea30dafd6a31d3e832fcf62b99a74a72c7b15c1582627250335630bfffb74f4` |
| `SKILL.md` | Step 5 and architectural patterns distilled in [diagnosis](npl-compile-diagnostics.md) / [idioms](npl-idioms.md); project observations only | `45b57c1280ec5e3e575443c2d127569ed687dd78a46565b8546502eed4da8277` |

Normative specification, target back-end constraints, project conventions, and historical fixes have different authority. A past compiler fix or wrapper/editor convention does not override the language specification or current profile. Require matching evidence before applying it to another revision. The old byte-alignment and exact-width blanket claims were removed because the bundled specification contradicts them.

No remote commands, container IDs, deployed-binary identifiers, workspace assumptions, or automatic execution instructions were imported. These hashes identify documentation, not deployment artifacts.
