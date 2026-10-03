# Contributing to HealtOS

Focused fixes, documentation improvements, and reproducible bug reports are welcome. Before starting a large change, open an issue describing the problem and proposed scope.

## Make a change

1. Fork the repository and create a branch.
2. Follow the [development setup](docs/DEVELOPMENT.md).
3. Make the smallest change that solves the problem and add relevant verification.
4. Run lint, format checks, tests, and the configuration verifier. Build the frontend for UI changes.
5. Open a pull request explaining the before/after behavior and checks you ran.

## Interface requirements

The dashboard must fit the viewport without horizontal or vertical page scrolling. Check desktop and mobile layouts. Long content may scroll inside its panel; hiding inaccessible content is not a fix. The history panel must follow a drag and retain its released height.

## Use synthetic examples

Never include real health records, OAuth credentials, account identifiers, private hostnames, `.env` files, database dumps, or real-account screenshots in an issue or pull request. Replace data with a synthetic example that still reproduces the problem. Review staged files and commit history before pushing.

Preserve original source payloads. Google health-data requests must remain read-only. Keep manual entries distinguishable from imported records. AI access must stay explicit and limited to requested analysis.

## Sensitive reports

Do not post credentials or sensitive records publicly. Use GitHub private vulnerability reporting when available for security issues. Ordinary bugs can be reported publicly with synthetic reproduction steps.
