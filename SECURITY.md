# Security policy

Community Pool holds member funds in custody and submits paid work to The
Innovation Game on members' behalf, so security reports matter to us.

## Reporting a vulnerability

Please report vulnerabilities privately rather than in a public issue. A
private contact address will be published here before the first public
release; until then, open a GitHub security advisory on this repository if that
option is available to you.

Include what you found, how to reproduce it, and what you believe the impact
is. We will acknowledge the report, keep you informed while we assess and fix
it, and credit you when the fix is published unless you prefer otherwise.

## Scope

- The pool API and website (`pool_manager/pool_v2`), including authentication,
  member funds, withdrawals, work assignment and settlement.
- The operations tools under `tools/` and the deployment templates under
  `deploy/`.
- The member worker, which lives in its own repository.

Findings about a particular deployment's hosting, DNS or hardware belong to
that deployment's operator, not to this project.

## What we ask

Do not test against a live pool with real member funds without the operator's
written permission, and do not access data that is not yours.
