# Security Policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:
**[Report a vulnerability](https://github.com/benchwire/labwire/security/advisories/new)**
(the Security tab of this repository). Please do not open a public issue
for anything you believe is exploitable. You can expect an acknowledgment
within a few days; this is a small project and there is no bounty program.

Supported versions: the latest release only. Pre-1.0, fixes land in the
next release rather than being backported.

## Threat model, stated precisely

Credibility here comes from precision, so this section says exactly what
the project's two security mechanisms protect against, and what they do
not. It is the security counterpart of the LIMITATIONS sections in the
package READMEs.

### What ed25519-signed run manifests protect against

A signed bundle (SPEC 13) proves, to anyone holding the server's public
key, that: the manifest and its streamed records were produced by a holder
of the signing key; they have not been modified since signing; and the
command name, normalized parameters, safety class, status, timestamps, and
data digest are the ones the server recorded. Tampering with any of it is
detectable (`labwire verify`, and the conformance suite proves the
detection).

They do NOT protect against:

- **A compromised or malicious server.** The signer attests to what it
  chose to write. A server that lies about what the instrument did signs
  its lie. Signatures give you tamper-evidence after the fact, not truth.
- **Key theft.** The signing key lives where the server runs, by default
  as a file beside the manifests. Whoever reads it can sign anything.
  There is no key rotation or revocation protocol.
- **Operator identity.** `identity_verified` is `false` in every manifest
  this implementation produces. A manifest proves a key signed it, never
  WHO approved or ran anything. Cryptographic operator identity (JWS) is
  future work, tracked in ROADMAP.md.
- **Omission.** A bundle proves what it contains; nothing proves a run
  was not simply never recorded.

### What operator grants protect against

An S3 grant (SPEC 8.6) proves that someone with write access to the grant
store approved THIS command with THESE exact parameters (bound by RFC 8785
digest), within a bounded time window and use count. An agent holding a
session confirmation cannot escalate it into an S3 action, cannot mint a
grant, and cannot reuse one against different parameters; the reference
server refuses each of these, and the conformance suite checks the
refusals.

They do NOT protect against:

- **A grant store the agent can write.** The whole mechanism assumes the
  store lives where the agent has no write path. Nothing in-protocol
  enforces that separation; it is a deployment property. Run the agent and
  the grant store as one user on one machine and S3 is theater.
- **Approver identity.** `issued_by` is an unauthenticated string. As with
  manifests: policy binding, not identity.
- **A malicious operator.** Grants encode that someone approved; they do
  not make the approval wise.

### What the protocol itself does not provide

- **No transport authentication or encryption beyond the transport's own.**
  `ws://` is plaintext; deployments crossing any network boundary should
  use `wss://` and their own authentication in front (SPEC 14). The
  `api_key` field in `initialize` is a deferred stub and currently does
  nothing in the reference implementation.
- **No authorization model below S2/S3.** Any connected client can run
  S0/S1 commands and read every resource. Isolation is the deployment's
  job (network segmentation, a fronting proxy).
- **No resource limits.** The reference server does not rate-limit or
  bound payload sizes beyond what the transport imposes; do not expose it
  to untrusted networks unfronted.

If your deployment needs a property this list says is absent, the honest
answer today is: put the mechanism providing it in front of Labwire, and
consider opening a spec question issue so the gap is at least recorded.
