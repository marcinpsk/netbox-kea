# Domain documentation

Use these files when exploring the codebase or changing domain terminology and architecture:

- Read `CONTEXT.md` at the repository root.
- Read relevant ADRs under `docs/adr/`.

If these files do not exist, proceed without reporting their absence. The `$domain-modeling` skill creates them when the project resolves domain terms or architectural decisions.

## Layout

This repository uses a single domain context:

```text
/
├── CONTEXT.md
├── docs/adr/
└── netbox_kea/
```

## Vocabulary

Use terms as defined in `CONTEXT.md` in issue titles, proposals, tests, and code.

If a required concept is missing, first check whether the project already uses a different term. Record a genuine terminology gap for `$domain-modeling`.

## ADR conflicts

Report when proposed work conflicts with an existing ADR. Name the ADR and explain why the decision might need review.
