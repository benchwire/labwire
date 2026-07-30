---
name: evidence-backed-reporting
description: Applies whenever the agent reports the outcome of an instrument command to a user or operator. Labwire exists to produce truthful records of what physically happened, signed when the deployment records manifests. The agent must report only what the record supports, cite the run when the surface identifies it, quote numeric results with their units, and say plainly when evidence is missing.
license: Apache-2.0
---

# Evidence-backed reporting

**Intent:** Every Labwire run ends in a terminal record, and, when the
deployment records manifests, a signed bundle: terminal status, result
values, and, for cancelled runs, a settlement block naming what
physically happened. Reported state diverging from recorded state is the
failure the protocol exists to prevent, and the last step is the agent's:
what it tells the human must match the record. This spec applies whenever
the agent reports a command outcome.

**Evidence:** Before reporting, the agent SHOULD have the run's terminal
status, its result payload, its command id when the surface provides one,
and, when the run was cancelled, its settlement outcome.

**Decision:** The agent SHOULD determine what the record actually
supports: succeeded with these values, failed with this error, never
started, halted, halted at a boundary, ran to completion despite the
cancel, or unconfirmed. "The command returned" and "the physical
operation happened as described" are different claims, and the record
says which one is licensed.

**Execution:** The agent SHOULD report outcomes citing the command id or
run bundle when the surface provides one, quote numeric results with
their units exactly as returned, and distinguish an acknowledged cancel
from a settled one. When a run's evidence bundle is retained on the
instrument host, the agent says where, so an operator can verify it
independently.

**Recovery:** If the terminal record is missing, unreadable, or fails
verification, the agent SHOULD say exactly that and stop short of the
claim the record would have supported. It SHOULD NOT reconstruct an
outcome from its own memory of what it intended.

**Failure modes:** This spec is meant to prevent: reporting success for a
run with no terminal record; rounding, unit-swapping, or inventing result
values; presenting a cancelled run with an unconfirmed halt as safely
stopped; summarizing an error result as a success with caveats; citing no
run at all when the surface identified one, so the report cannot be
audited.
