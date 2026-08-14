# Contributing

Thanks for your interest in improving Persona Growth Loop.

## Ground rules

- **Issue first.** Open a GitHub issue before starting non-trivial work. State *why* the change is needed, *what "done" looks like* (checkable conditions), and *which files you expect to touch*. One-line fixes such as typos are exempt.
- **Contracts are frozen documents.** The three contract documents ([docs/contracts/](docs/contracts/)) and the frozen architecture ([docs/architecture-v1.md](docs/architecture-v1.md)) are the source of truth; code follows them, not the other way round. A change that relaxes a cap, a gate, or a fail-closed path needs an explicit contract amendment in the same review, and loosening changes get the stricter review lane.
- **Honest completion.** A change is done when its stated done-conditions pass with evidence, not when it looks done. Pull requests should list which conditions passed and how they were checked.

## Running the tests

From the repository root:

```sh
python3 -m unittest discover -s tests
```

The suite is pure standard-library Python 3 and needs no network. All tests must pass before a pull request is reviewed. If your change alters collector filtering, gate checks, applier behavior, or the deploy dispatcher, add or extend a test that pins the new contract.

## Pull requests

- Keep one pull request per issue, and keep branches short-lived.
- List the files you changed and confirm they match what the issue predicted; explain any difference.
- Never include real personal identifiers (names, chat/user IDs, hostnames, private paths) in code, fixtures, or docs — use the generic placeholder values already present in the repository.

## Code style

- Python 3 standard library plus PyYAML (`requirements.txt`); do not add further runtime dependencies without an issue making the case.
- Prefer deterministic code, atomic writes, and fail-closed gates over background state — that is the design principle of the whole project.
