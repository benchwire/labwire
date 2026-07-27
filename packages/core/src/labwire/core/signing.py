"""ed25519 run-manifest signing and verification (SPEC §13).

The signature covers the RFC 8785 canonicalization of the manifest minus
its ``signature`` field, and is encoded as unpadded base64url.

Example:
    >>> from labwire.core.signing import SigningKey
    >>> key = SigningKey.generate()
    >>> key.key_id.startswith("sha256:")
    True
"""

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Self, cast

from labwire.core._meta import MANIFEST_VERSION as MANIFEST_VERSION
from labwire.core.capabilities import IdentityInfo
from labwire.core.jcs import jcs_canonical
from labwire.core.messages import CommandState
from labwire.core.types import JsonRpcError
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey as _NaclSigningKey
from nacl.signing import VerifyKey
from pydantic import BaseModel, ConfigDict


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SigningKey:
    """An ed25519 signing key with TOFU-friendly file persistence.

    Example:
        >>> key = SigningKey.generate()
        >>> # key.save(Path("~/.labwire/signing.key"))
    """

    def __init__(self, raw: _NaclSigningKey) -> None:
        self._raw = raw

    @classmethod
    def generate(cls) -> Self:
        """Generate a fresh keypair."""
        return cls(_NaclSigningKey.generate())

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load a key saved by :meth:`save`."""
        return cls(_NaclSigningKey(bytes.fromhex(path.read_text().strip())))

    @classmethod
    def load_or_generate(cls, path: Path) -> Self:
        """Load the key at ``path``, generating and saving it on first run.

        Example:
            >>> # key = SigningKey.load_or_generate(Path("signing.key"))
        """
        if path.exists():
            return cls.load(path)
        key = cls.generate()
        key.save(path)
        return key

    def save(self, path: Path) -> None:
        """Persist the private seed (hex) with owner-only permissions."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(bytes(self._raw).hex() + "\n")

    @property
    def public_key_b64(self) -> str:
        """The 32-byte public key, standard base64 (SPEC §13.1)."""
        return base64.b64encode(bytes(self._raw.verify_key)).decode()

    @property
    def key_id(self) -> str:
        """``sha256:`` + hex SHA-256 of the raw public key (SPEC §13.1)."""
        return "sha256:" + hashlib.sha256(bytes(self._raw.verify_key)).hexdigest()

    def sign(self, message: bytes) -> bytes:
        """Detached ed25519 signature over ``message``."""
        return self._raw.sign(message).signature


class _M(BaseModel):
    model_config = ConfigDict(extra="allow")


class ManifestCommand(_M):
    """The submitted command, verbatim, and its enforced class (SPEC §13.1)."""

    name: str
    params: dict[str, Any]
    safety_class: str | None = None


class ManifestData(_M):
    """Digest of the run's record stream (SPEC §13.1)."""

    digest_alg: str
    digest: str
    channels: list[str]


class ManifestTimestamps(_M):
    """RFC 3339 UTC run timestamps (SPEC §13.1)."""

    submitted: str
    started: str
    completed: str


class SignerInfo(_M):
    """Key identification for the manifest signature (SPEC §13.1)."""

    alg: str
    public_key: str
    key_id: str


class Manifest(_M):
    """The SPEC §13.1 run manifest document (optionally signed).

    Example:
        >>> # Manifest.model_validate(json.loads(bundle_manifest_json))
    """

    manifest_version: str
    protocol_version: str
    run_id: str
    instrument: IdentityInfo
    command: ManifestCommand
    status: CommandState
    result: Any = None
    error: JsonRpcError | None = None
    data: ManifestData
    timestamps: ManifestTimestamps
    signer: SignerInfo | None = None
    signature: str | None = None


class VerificationResult(BaseModel):
    """Outcome of manifest/bundle verification.

    Example:
        >>> VerificationResult(ok=True, errors=[]).ok
        True
    """

    ok: bool
    errors: list[str]
    warnings: list[str] = []


def sign_manifest(manifest: dict[str, Any], key: SigningKey) -> dict[str, Any]:
    """Attach ``signer`` and an ed25519 ``signature`` to a manifest document.

    Example:
        >>> # doc = sign_manifest(manifest, SigningKey.generate())
    """
    doc = dict(manifest)
    doc["signer"] = {"alg": "ed25519", "public_key": key.public_key_b64, "key_id": key.key_id}
    doc["signature"] = _b64url(key.sign(jcs_canonical(doc)))
    return doc


def verify_manifest(doc: dict[str, Any]) -> VerificationResult:
    """Verify a signed manifest's key_id and signature (SPEC §13.2).

    Example:
        >>> # outcome = verify_manifest(json.loads(manifest_json))
    """
    errors: list[str] = []
    signer = doc.get("signer")
    signature = doc.get("signature")
    if not isinstance(signer, dict) or signature is None:
        return VerificationResult(ok=False, errors=["manifest is not signed"])
    untyped_signer = cast("dict[Any, Any]", signer)
    signer_fields: dict[str, Any] = {str(k): v for k, v in untyped_signer.items()}
    if signer_fields.get("alg") != "ed25519":
        errors.append(f"unsupported signer.alg: {signer_fields.get('alg')!r}")
    try:
        raw_key = base64.b64decode(str(signer_fields.get("public_key", "")), validate=True)
        if len(raw_key) != 32:
            raise ValueError(f"expected 32 bytes, got {len(raw_key)}")
    except Exception as exc:
        return VerificationResult(ok=False, errors=[*errors, f"bad signer.public_key: {exc}"])
    expected_key_id = "sha256:" + hashlib.sha256(raw_key).hexdigest()
    if signer_fields.get("key_id") != expected_key_id:
        errors.append("signer.key_id does not match signer.public_key")
    unsigned = {k: v for k, v in doc.items() if k != "signature"}
    try:
        VerifyKey(raw_key).verify(jcs_canonical(unsigned), _unb64url(str(signature)))
    except BadSignatureError:
        errors.append("signature invalid: manifest does not match")
    except Exception as exc:
        errors.append(f"signature unverifiable: {exc}")
    return VerificationResult(ok=not errors, errors=errors)


def verify_bundle(path: Path) -> VerificationResult:
    """Verify a run bundle: signature, key_id, and record-stream digest.

    ``path`` is a bundle directory containing ``manifest.json`` (and,
    usually, ``records.jsonl``) or a path to a manifest file directly.

    Example:
        >>> # outcome = verify_bundle(Path("runs/<run_id>"))
    """
    manifest_path = path if path.is_file() else path / "manifest.json"
    if not manifest_path.exists():
        return VerificationResult(ok=False, errors=[f"no manifest at {manifest_path}"])
    try:
        doc: dict[str, Any] = json.loads(manifest_path.read_text())
    except (ValueError, OSError) as exc:
        return VerificationResult(ok=False, errors=[f"unreadable manifest: {exc}"])
    outcome = verify_manifest(doc)
    errors = list(outcome.errors)
    warnings: list[str] = []
    try:
        parsed = Manifest.model_validate(doc)
    except Exception as exc:
        errors.append(f"manifest does not match SPEC §13.1: {exc}")
        return VerificationResult(ok=False, errors=errors)
    records_path = manifest_path.parent / "records.jsonl"
    if records_path.exists():
        digest = hashlib.sha256(records_path.read_bytes()).hexdigest()
        if digest != parsed.data.digest:
            errors.append("data.digest does not match records.jsonl")
    else:
        warnings.append("records.jsonl absent: data.digest not recomputed")
    return VerificationResult(ok=not errors, errors=errors, warnings=warnings)
