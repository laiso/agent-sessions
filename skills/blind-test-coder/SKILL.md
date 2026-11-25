---
name: blind-test-coder
description: Write an independent test suite from the specification alone, without reading the implementation or the existing tests, when asked to test a project as a fresh agent and report what implementer-written tests miss.
---

# Write tests from the spec alone

You are the independent test author. You have not been involved in implementing the project. Your job is to write a fresh test suite from the specification alone. The owner wants to find out what an independent suite catches that implementer-written tests miss.

The supervisor's request gives you: the project checkout, the spec documents, the branch to start from, and where to send your completion report. Use only those. Do not ask for the implementation.

## Setup

Work on a new branch `<your-name>/tests-from-spec`, created from the branch holding the latest implementation. Do NOT push to the remote. Commit locally.

## Step 1 — Wipe the existing tests

Delete all existing test files in your branch (unit tests, end-to-end tests, test helpers). Do NOT read them first. They stay recoverable in version history; deleting them in your branch is the point — a clean slate. Do NOT delete anything else.

## Step 2 — Write the new suite from the spec

The spec is:

- The user-facing documentation in the project root (the contract: commands, options, target syntax, output formats, exit codes where documented).
- The feature specification documents the supervisor named.
- The CLI's own `--help` output (run the top-level `--help` and each subcommand's `--help`).

Do NOT read the implementation source while writing tests. You may run the CLI as a black box as much as you like.

Where the spec is silent or ambiguous: write the test from the most reasonable reading, and FLAG the ambiguity in your completion report — do not silently guess, and do not read the implementation to resolve it.

Coverage: every command across every backend the CLI supports, both output formats, the output schema for structured results, error cases and exit codes, and the structured-vs-raw distinction between backends. Include end-to-end tests that run the real CLI against LIVE sessions on this machine, not just mocked fixtures. Follow the project's existing test conventions (language, framework, layout).

## Step 3 — Run everything and report

Run the full suite (unit + end-to-end) against the CLI on this machine. Every failure is a finding: either a bug in the implementation or a spec ambiguity. Do NOT fix the implementation — report findings.

Your completion report must include: test counts (unit / end-to-end, passed / failed / skipped), the CLI evidence (test output, and live command output), a list of every failure with your judgment (bug vs spec ambiguity), and every spec ambiguity you flagged.

Goal condition: the suite is complete and runnable, and every failure is classified. A suite that passes 100% with no findings is also a valid result — report it as such, with the evidence.
