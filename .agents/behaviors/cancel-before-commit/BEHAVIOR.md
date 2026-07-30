---
name: cancel-before-commit
description: Applies whenever the agent plans or cancels instrument commands. Every instrument command tool description states what cancellation can physically do (abort, between steps, or none). The agent must plan around the declared semantics before calling, never treat cancel as an undo, and report a run as stopped only when the settled record says so.
license: Apache-2.0
---

# Cancel before commit

**Intent:** Cancellation of a physical process is physics, not
bookkeeping. Every instrument command tool description carries a cancel
sentence stating what a cancel request can actually do to the device. The
agent plans around that sentence before committing to a call. This spec
applies to every instrument command the agent submits or cancels.

**Evidence:** Before calling, the agent SHOULD read the tool's "Cancel:"
sentence. Before reporting on a cancellation, it SHOULD read the run's
terminal record, not just the cancel acknowledgment.

**Decision:** For a "Cancel: none" command, the tool description's own
words govern the decision: "Once started this runs to completion; the
operation is committed to the device and a cancel request will be
refused. Decide before calling, not after." For "Cancel: between steps
only", the agent expects that the step in flight finishes and partial
physical effects (such as liquid already aspirated) remain. For
"Cancel: abort", the agent knows a cancelled run's record states whether
the halt was confirmed, and that an unconfirmed halt means the physical
state must be treated as unknown.

**Execution:** The agent SHOULD NOT submit a committed command
speculatively on the assumption that cancel is an undo. When it does
cancel, it waits for settlement and reports the settled outcome (never
started, halted, halted at a boundary, ran to completion, or
unconfirmed), never "cancelled" on the strength of an acknowledgment
alone.

**Recovery:** A refusal of a cancel against a "none" command is the
protocol working, not an error: the agent SHOULD report that the run will
complete and plan from there, rather than retrying the cancel.

**Failure modes:** This spec is meant to prevent: claiming a run stopped
because a cancel was acknowledged; treating a cancel refusal as a failure
to route around; submitting irreversible or long commands as experiments
with cancel as the imagined escape; ignoring an unconfirmed halt and
acting as if the instrument is in a known state.
