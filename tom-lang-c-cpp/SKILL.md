---
name: tom-lang-c-cpp
description: Use when tom-autodev generates, reviews, or designs tests for C or C++ changes in a configured project such as BGW.
---

# Tom C/C++

## Scope

Provide language-level rules for approved Spec and Task Plan work. Keep project topology, module names, toolchain versions, and release rules in the project child skill.

## Generation Rules

- Preserve public ABI/API and existing ownership, error, logging, and concurrency conventions unless the Spec explicitly changes them.
- Prefer the smallest change at the existing boundary; inspect callers and transitive impact before changing a signature.
- Make ownership, lifetime, nullability, integer widths, serialization, thread safety, and failure behavior explicit.
- Reuse existing abstractions and test fixtures; do not add a framework or generic layer without a traced requirement.
- Keep business-repository and independent product-test-repository changes aligned to one external behavior and Change Set.

## Review and Tests

Use `references/testing-and-review.md` for behavior-first test and source-review checks. Design valid, boundary, invalid, compatibility, and regression cases at the highest stable external interface. Put compiler, unit, regression, and integration execution parameters in iPipe evidence; never run project validation locally on Mac.

The checklist is mandatory for every C/C++ Change Set: inspect callers and ABI, ownership/lifetime, bounds/overflow, concurrency, errors, serialization, and platform assumptions; pair each business patch with independent product-test fixtures and test IDs. Classify remote failures before proposing repair and preserve the exact iPipe evidence references in the phase ArtifactEnvelope.

**REQUIRED PARENT:** Return language decisions and source/test guidance to `tom-plan`, `tom-implement`, or `tom-review`; do not call iCode/iPipe directly.
