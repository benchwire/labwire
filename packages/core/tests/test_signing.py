"""Tests for ed25519 manifest signing and verification (SPEC §12)."""

import base64
import hashlib
from pathlib import Path
from typing import Any

from labwire.core.signing import Manifest, SigningKey, sign_manifest, verify_manifest


def _manifest() -> dict[str, Any]:
    return {
        "manifest_version": "0.1",
        "protocol_version": "0.1",
        "run_id": "b7e0a1c2-4d5e-4f60-8a9b-0c1d2e3f4a5b",
        "instrument": {
            "manufacturer": "Labwire Project",
            "model": "SimBalance-120",
            "serial_number": "SIM-0003",
            "firmware_version": "0.1.0",
        },
        "command": {"name": "measure", "params": {"settle_timeout_s": 30.0}},
        "status": "succeeded",
        "result": {"mass_g": 12.3456},
        "data": {
            "digest_alg": "sha256",
            "digest": hashlib.sha256(b"").hexdigest(),
            "channels": [],
        },
        "timestamps": {
            "submitted": "2026-07-23T15:30:00.123456Z",
            "started": "2026-07-23T15:30:00.234567Z",
            "completed": "2026-07-23T15:30:12.345678Z",
        },
    }


def test_key_generation_save_load_round_trip(tmp_path: Path) -> None:
    key = SigningKey.generate()
    path = tmp_path / "signing.key"
    key.save(path)
    loaded = SigningKey.load(path)
    assert loaded.key_id == key.key_id
    assert loaded.public_key_b64 == key.public_key_b64


def test_key_id_is_sha256_of_raw_public_key() -> None:
    key = SigningKey.generate()
    raw = base64.b64decode(key.public_key_b64)
    assert len(raw) == 32
    assert key.key_id == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_sign_then_verify_round_trip() -> None:
    key = SigningKey.generate()
    doc = sign_manifest(_manifest(), key)
    assert doc["signer"]["alg"] == "ed25519"
    assert doc["signer"]["public_key"] == key.public_key_b64
    assert "signature" in doc
    outcome = verify_manifest(doc)
    assert outcome.ok, outcome.errors


def test_tampered_manifest_fails_verification() -> None:
    doc = sign_manifest(_manifest(), SigningKey.generate())
    doc["command"]["params"]["settle_timeout_s"] = 31.0
    outcome = verify_manifest(doc)
    assert not outcome.ok
    assert any("signature" in e for e in outcome.errors)


def test_mismatched_key_id_fails_verification() -> None:
    doc = sign_manifest(_manifest(), SigningKey.generate())
    doc["signer"]["key_id"] = "sha256:" + "0" * 64
    outcome = verify_manifest(doc)
    assert not outcome.ok
    assert any("key_id" in e for e in outcome.errors)


def test_missing_or_garbage_signature_fails() -> None:
    doc = sign_manifest(_manifest(), SigningKey.generate())
    del doc["signature"]
    assert not verify_manifest(doc).ok
    doc2 = sign_manifest(_manifest(), SigningKey.generate())
    doc2["signature"] = "!!!not-base64url!!!"
    assert not verify_manifest(doc2).ok


def test_manifest_model_round_trips_the_signed_document() -> None:
    doc = sign_manifest(_manifest(), SigningKey.generate())
    parsed = Manifest.model_validate(doc)
    assert parsed.model_dump(mode="json", exclude_unset=True) == doc


def test_saved_key_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "signing.key"
    SigningKey.generate().save(path)
    assert (path.stat().st_mode & 0o777) == 0o600
