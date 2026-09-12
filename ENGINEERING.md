# Engineering and interview notes

## Why this project

This project demonstrates data validation, repeatable ingestion, transaction boundaries, and traceable errors. It complements a user-facing health platform without claiming to process patient records.

## Decisions

**Explicit contracts.** Inferred types can silently change between files. The versioned contract instead makes required fields and constraints reviewable. A hash rejects changes under an existing contract name. Contract names separate data namespaces; a changed name does not automatically migrate data.

**Validate before writing.** A malformed file fails before any warehouse creation. In default mode, row-level validation failures are quarantined while valid rows are accepted. Strict mode rejects the batch. Both modes use the same parser and validations.

**Atomic import.** Batch metadata, accepted upserts, and rejection details commit together. An injected database failure test proves that partial records and batch metadata do not remain after rollback.

**Two kinds of identity.** The file hash prevents exact replays. The contract's record key controls updates across distinct files. These solve different problems: idempotency of the import and identity of the business record.

**Decimal strings.** Monetary-looking synthetic values use Decimal during validation and strings in JSON storage. This avoids binary floating-point rounding, at the cost of requiring deliberate conversion for analytics.

**JSON records in fixed tables.** This keeps the loader general and avoids dynamic SQL. It sacrifices strong database column types and simple column-oriented analytics. The JSON contract remains the application-level validator.

## Walkthrough

Run `python demo.py`, inspect the contract, then use `--dry-run` to explain why each rejected sample row fails. Trace the first successful batch and the second skipped replay. Finally, explain why an invalid update cannot replace a valid record.

## Challenges worth discussing

- A journal record cannot be committed before the records it represents.
- Replay detection must occur under the write lock to avoid concurrent duplicate batches.
- A valid later duplicate in the same file is rejected when the first occurrence was invalid; that deterministic policy needs explicit documentation.
- Default quarantine mode needs monitoring, because a successful process exit can still include rejected records.
- A later file can contain older business data. Last-commit-wins is implemented; event-time conflict resolution is not.

## Contribution record

Codex created and locally verified the initial code, tests, contract, and synthetic fixtures during this task. The user's independent review and changes remain to be established. Label this as a new AI-assisted personal portfolio project, and explain your actual review or modifications when discussing authorship.

Accurate now: “This portfolio pipeline separates file replay identity from record identity and commits the import journal with accepted records.” Avoid claiming ownership of historical work, production reliability, healthcare compliance, or data-volume improvements.
