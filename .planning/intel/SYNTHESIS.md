# Synthesis Summary

Mode: `new` (no existing `.planning/` context to merge against)

## Docs synthesized: 2

| Source | Type | Confidence | Precedence | Locked |
|---|---|---|---|---|
| `README.md` | SPEC | high | 0 (highest, manifest override) | false |
| `CLAUDE.md` | ADR | high | 1 (manifest override) | false |

No PRD or DOC-classified documents were present in this ingest set.

## Cycle detection

Cross-ref graph: `CLAUDE.md → [README.md, .env, .env.example]`; `README.md → []`. `.env`/`.env.example` are not classified docs (leaf references). No cycle exists. Both docs were fully synthesized.

## Decisions locked: 0

16 decisions extracted from CLAUDE.md (ADR), all `proposed` (non-locked) per the classifier's determination — see `decisions.md` for D-01 through D-16. Every decision was cross-checked against README.md and found consistent or directly reaffirmed; none required precedence arbitration because no contradiction existed. The precedence rule itself (README.md wins on disagreement) is recorded in `decisions.md` as the governing note for any future conflict.

## Requirements: 0

No PRDs in this ingest set — `requirements.md` is a placeholder. README.md's phase-by-phase build/done-when content is captured as `protocol`-type constraints instead (see below), consistent with its SPEC classification.

## Constraints: 37 (README.md, SPEC)

Breakdown: 9 api-contract · 6 schema · 7 nfr · 15 protocol. See `constraints.md`.

## Context topics: 0

No DOC-classified documents in this ingest set — `context.md` is a placeholder.

## Conflicts: 0 blockers, 0 competing-variants, 2 auto-resolved (info-only)

Both INFO entries are transparency records, not actual contradiction resolutions: (1) the manifest's precedence override of README.md (SPEC=0) over CLAUDE.md (ADR=1), which matches CLAUDE.md's own stated deference to README.md; (2) confirmation that a full line-by-line cross-check of overlapping content between the two documents found no divergence. See `C:\Users\Manas\Desktop\NLP\.planning\INGEST-CONFLICTS.md` for detail.

## Files

- `C:\Users\Manas\Desktop\NLP\.planning\intel\decisions.md`
- `C:\Users\Manas\Desktop\NLP\.planning\intel\requirements.md`
- `C:\Users\Manas\Desktop\NLP\.planning\intel\constraints.md`
- `C:\Users\Manas\Desktop\NLP\.planning\intel\context.md`
- `C:\Users\Manas\Desktop\NLP\.planning\INGEST-CONFLICTS.md`

## Status

READY — safe to route. No blockers, no competing variants requiring user resolution.
