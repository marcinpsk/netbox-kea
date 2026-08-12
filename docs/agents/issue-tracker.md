# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply or remove labels**: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`
- **Close an issue**: `gh issue close <number> --comment "..."`

Infer the repository from `git remote -v`. The `gh` CLI does this automatically when it runs inside the clone.

## Pull requests as a triage surface

**PRs as a request surface: yes.** The `$triage` skill includes external pull requests in the triage queue.

Pull requests use the same labels and states as issues:

- **Read a pull request**: Run `gh pr view <number> --comments` and `gh pr diff <number>`.
- **List external pull requests**: Run `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`. Keep only `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` author associations.
- **Comment, label, or close**: Use `gh pr comment`, `gh pr edit`, or `gh pr close`.

GitHub shares one number space across issues and pull requests. Resolve a bare `#42` with `gh pr view 42`, then fall back to `gh issue view 42`.

## Publishing to the issue tracker

When a skill says to publish to the issue tracker, create a GitHub issue.

## Fetching a ticket

When a skill says to fetch the relevant ticket, run:

```bash
gh issue view <number> --comments
```

## Wayfinding operations

The `$wayfinder` skill uses one map issue and child issues as tickets.

- **Map**: Create one issue with the `wayfinder:map` label. Store Notes, Decisions-so-far, and Fog in its body.
- **Child ticket**: Link each ticket to the map as a GitHub sub-issue. If sub-issues are unavailable, add the child to a task list in the map and add `Part of #<map>` to the child body. Use a `wayfinder:<type>` label with `research`, `prototype`, `grilling`, or `task`.
- **Blocking**: Use GitHub issue dependencies. If dependencies are unavailable, add `Blocked by: #<n>, #<n>` to the child body.
- **Frontier query**: List the map's open children. Exclude assigned tickets and tickets with open blockers. Select the first remaining ticket in map order.
- **Claim**: Run `gh issue edit <number> --add-assignee @me`.
- **Resolve**: Comment with the answer, close the issue, and add a context pointer to the map's Decisions-so-far section.
