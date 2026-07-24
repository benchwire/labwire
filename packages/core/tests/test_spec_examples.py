"""Round-trip every JSON example in spec/SPEC.md through the message models.

SPEC §15: from milestone M2, every marked fenced JSON block must round-trip
through the model registered for its method. Manifest examples validate from
M4 and are skipped here; ``signature-excerpt`` blocks are exempt.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from labwire.core.messages import MESSAGE_TYPES
from labwire.core.signing import Manifest
from labwire.core.types import (
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
)

SPEC = Path(__file__).parents[3] / "spec" / "SPEC.md"

_BLOCK = re.compile(r"```json\n<!-- example: (?P<marker>\S+) -->\n(?P<body>.*?)```", re.DOTALL)


def _examples() -> list[tuple[str, dict[str, Any]]]:
    text = SPEC.read_text()
    found = [(m["marker"], json.loads(m["body"])) for m in _BLOCK.finditer(text)]
    assert found, f"no examples found in {SPEC}"
    return found


def _split(marker: str) -> tuple[str, str]:
    name, _, kind = marker.rpartition("/")
    return name, kind


@pytest.mark.parametrize(
    ("marker", "raw"), _examples(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_spec_example_round_trips(marker: str, raw: dict[str, Any]) -> None:
    name, kind = _split(marker)
    if name == "manifest":
        if kind == "signature-excerpt":
            # excerpt blocks are validated only for the fields present (SPEC §15)
            assert "signature" in raw
            assert set(raw) <= set(Manifest.model_fields), "unknown field in excerpt"
            return
        manifest = Manifest.model_validate(raw)
        assert manifest.model_dump(mode="json", exclude_unset=True) == raw
        return
    if kind in {"request", "notification", "notification-terminal"}:
        envelope = JsonRpcRequest if kind == "request" else JsonRpcNotification
        parsed = envelope.model_validate(raw)
        assert parsed.method == name
        params_model = MESSAGE_TYPES[name].params
        params = params_model.model_validate(parsed.params)
        assert params.model_dump(mode="json", exclude_unset=True) == raw["params"]
    elif kind == "result":
        parsed_response = JsonRpcResponse.model_validate(raw)
        result_model = MESSAGE_TYPES[name].result
        assert result_model is not None, f"{name} has no result model"
        result = result_model.model_validate(parsed_response.result)
        assert result.model_dump(mode="json", exclude_unset=True) == raw["result"]
    elif kind == "response":
        parsed_error = JsonRpcErrorResponse.model_validate(raw)
        dumped = parsed_error.model_dump(mode="json", exclude_none=True)
        assert dumped == raw
    else:
        pytest.fail(f"unknown example kind {kind!r} in marker {marker!r}")


def test_every_registry_method_has_at_least_one_example() -> None:
    exampled = {_split(marker)[0] for marker, _ in _examples()}
    missing = {m for m in MESSAGE_TYPES if m not in exampled}
    # notifications/command_status has two examples; every method needs >= 1
    assert not missing, f"spec §15 lacks examples for: {sorted(missing)}"
