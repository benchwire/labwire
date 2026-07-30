---
name: irreversible-command-approval
description: Applies whenever the agent prepares to call an instrument tool whose description says "Safety class S2". S2 commands are costly or irreversible (they consume reagent or destroy samples). The agent must obtain informed human approval for the exact call, submit exactly what was approved, and treat a decline as a final answer, not an obstacle.
license: Apache-2.0
---

# Irreversible command approval

**Intent:** Labwire marks a command "Safety class S2 (costly or
IRREVERSIBLE, e.g. consumes reagent or destroys a sample)" in the tool
description the agent reads at discovery. The protocol refuses an S2
submission without a confirmation. On hosts where the adapter raises an
approval dialog, the adapter itself shows the human the exact parameters;
on the legacy parameter path, only the agent controls whether the human
who approved saw what they were approving. This spec applies to every
call of a tool whose description carries that S2 sentence.

**Evidence:** Before deciding to call, the agent SHOULD read the tool's
safety class and cancel sentences, read the instrument state needed to say
concretely what will be consumed or altered, and fix the exact parameter
values it intends to submit.

**Decision:** The agent SHOULD conclude that the call is necessary for the
user's goal, that no reversible alternative achieves the same result, and
that the parameter values it will submit are the ones a human will see.

**Execution:** The agent SHOULD present the exact command and parameter
values when approval is sought, then submit those values unchanged. On
hosts where the adapter raises an approval dialog, the agent lets the human
answer it. On the legacy parameter path, the tool description says:
"Requires a `confirmation` value; supply the operator-provided
confirmation string." The agent supplies it only for a call whose
parameters the operator has seen.

**Recovery:** If approval is declined, the agent SHOULD stop that path,
report the decline, and continue with whatever does not require the
irreversible step. If it is unsure whether an approval covered the current
values, it asks again with the current values.

**Failure modes:** This spec is meant to prevent: submitting with a
confirmation for parameters no human saw; changing parameter values between
approval and submission; re-asking repeatedly until someone clicks yes;
treating a decline as an error to be engineered around; describing an
irreversible call as routine to make approval more likely.
