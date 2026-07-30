---
name: hazardous-command-grants
description: Applies whenever the agent prepares to call an instrument tool whose description says "Safety class S3". S3 commands can harm people or equipment and require an operator grant bound to the exact command and parameter values. The agent must obtain the grant through the refusal path, must stop after reporting a refusal, and must never invent, guess, or reuse a grant id.
license: Apache-2.0
---

# Hazardous command grants

**Intent:** Labwire marks a command "Safety class S3 (HAZARDOUS, capable of
harming people or equipment)" in the tool description. No session
confirmation authorizes it; authorization is an operator grant, provisioned
outside the protocol, expiring, use-limited, and bound to this command and
these exact parameter values. This spec applies to every call of a tool
whose description carries that S3 sentence.

**Evidence:** The agent SHOULD read the tool description's account of the
grant mechanism, and, after a refusal, preserve the request id and the
exact operator command the server returned.

**Decision:** The agent SHOULD determine whether it holds a grant for these
exact parameter values. Holding a grant for similar values, or for the same
command with different values, is not holding a grant.

**Execution:** Without a grant, the agent calls the tool once WITHOUT
authorization; the server will refuse it and return a request id and the
exact command a human operator must run. The tool description then says
what to do, and the agent does it: "Report that to your operator and stop."
With a grant id supplied by the operator, the agent submits only the exact
values the grant is bound to.

**Recovery:** If a grant is expired or its uses are exhausted, the agent
returns to the operator through the same refusal path. If the goal has
changed so that different parameter values are needed, the old grant is
irrelevant and the refusal path starts over.

**Failure modes:** This spec is meant to prevent exactly what the tool
description forbids: "Never invent a grant id." Also: guessing or reusing
ids from earlier runs; calling the refusal path repeatedly as if retrying
would change the answer; adjusting parameter values to fit a grant, or
presenting a grant obtained for one purpose as covering another.
