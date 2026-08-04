# Working agreement

## All work happens against an issue

Every change traces to a GitHub issue. Before starting work:

- If an issue exists, comment on it saying what is being done.
- If none exists, open one first — including for small fixes and for work that
  arrives as a passing request in conversation.

Reference the issue in the commit (`Fixes #12`, or `Refs #12` for partial work),
and close it once the work is deployed, not merely merged. Keep the tracker
current: if scope changes mid-flight, update the issue rather than silently
doing something else.

Findings that are not being acted on now still get an issue, so the reasoning
is not lost — including the caveats and anything that was checked and found
wrong.

## Verify claims before acting on them

Two suggestions from review have turned out to be wrong in ways only reading the
source caught: that the pending-pins layer had no stable identifier (it exposes
`GlobalID`), and that Thames Water's "12-week road closure permit" line to Ofwat
was about repairs (it is about installing water meters). Check the primary
source. Record what was checked in the issue.

## Publishing

`main` deploys itself via `deploy.yml` on push. `collect.yml` handles data only
and calls the deploy workflow when it commits a snapshot. A change is not done
until it is live — verify against the published site, not the repository.

## Standards for anything published

Every figure on the site must be reproducible from the committed change log.
State the caveat next to the number, not only in the README. Never assert a
legal or financial claim (money owed, breach of a standard) without a citation
to the regulation, and prefer reporting elapsed time against a threshold over
asserting a consequence.
