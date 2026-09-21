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

For each C/C++ Change Set, select the checklist areas affected by its behavior: callers/ABI, ownership/lifetime, bounds/overflow, concurrency, errors, serialization and platform assumptions. Keep independent product-test fixtures and test IDs aligned with the business behavior. Classify remote failures before proposing repair; return actual iPipe evidence references in the caller's supported DraftContent fields. The worker packages the envelope.

**REQUIRED PARENT:** Return language decisions and source/test guidance to `tom-plan`, `tom-implement`, or `tom-review`; do not call iCode/iPipe directly.
