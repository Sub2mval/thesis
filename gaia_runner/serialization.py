"""
One JSON-safe recursive serializer, used everywhere a request or response
object touching an LLM call needs to be written into a trace (see
PART 2 / PART 3 / PART 10 of the instrumentation pass this module was
added for). Converts:

    Pydantic models (v1 and v2)
    dataclasses
    Mappings (dict-likes)
    Sequences (list/tuple/set), excluding str/bytes
    Enums
    namedtuples
    plain objects with __dict__

into plain JSON-safe structures (dict/list/str/int/float/bool/None),
WITHOUT silently reducing any of the above to str(...) when richer
structured fields are available -- that reduction is only a last resort,
for objects json.dumps already accepts as-is or that expose none of the
richer shapes above.

Binary payloads (bytes/bytearray) are not embedded as huge blobs; instead
size/hash/a bounded preview are recorded (see _serialize_bytes) --
matching PART 10's "do not embed huge binary blobs blindly, but don't
lose the fact that an attachment was supplied" requirement. Ordinary
strings (including base64 text already embedded in a message, e.g.
images_b64) are left untouched -- PART 15 says not to truncate long
strings, and that requirement applies to those.
"""

from __future__ import annotations

import base64
import dataclasses
import enum
import hashlib
import json
from typing import Any, FrozenSet

_MAX_DEPTH = 60
_BYTES_PREVIEW_LEN = 2000


def to_jsonsafe(obj: Any, _depth: int = 0, _seen: FrozenSet[int] = frozenset()) -> Any:
    """Recursively convert `obj` into a JSON-safe structure. Never raises --
    worst case, a value that can't be introspected further comes back as
    repr(obj) tagged with its type, so a serialization edge case degrades
    to a lossy-but-present string rather than crashing the whole trace
    write (per PART 15: the trace must still be written)."""
    try:
        return _to_jsonsafe(obj, _depth, _seen)
    except Exception as exc:  # pragma: no cover - defensive fallback only
        return {"__serialization_error__": f"{type(exc).__name__}: {exc}", "__repr__": _safe_repr(obj)}


def _safe_repr(obj: Any) -> str:
    try:
        return repr(obj)
    except Exception:
        return f"<unreprable {type(obj).__name__}>"


def _to_jsonsafe(obj: Any, _depth: int, _seen: FrozenSet[int]) -> Any:
    if _depth > _MAX_DEPTH:
        return f"<max recursion depth reached: {type(obj).__name__}>"

    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj

    if isinstance(obj, (bytes, bytearray)):
        return _serialize_bytes(bytes(obj))

    obj_id = id(obj)
    if obj_id in _seen:
        return f"<circular reference: {type(obj).__name__}>"
    _seen = _seen | {obj_id}

    if isinstance(obj, enum.Enum):
        return obj.value

    # Pydantic v2
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump) and hasattr(type(obj), "model_fields"):
        try:
            return _to_jsonsafe(model_dump(mode="json"), _depth + 1, _seen)
        except Exception:
            try:
                return _to_jsonsafe(model_dump(), _depth + 1, _seen)
            except Exception:
                pass

    # Pydantic v1
    if callable(getattr(obj, "dict", None)) and hasattr(type(obj), "__fields__"):
        try:
            return _to_jsonsafe(obj.dict(), _depth + 1, _seen)
        except Exception:
            pass

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        try:
            return {f.name: _to_jsonsafe(getattr(obj, f.name), _depth + 1, _seen) for f in dataclasses.fields(obj)}
        except Exception:
            pass

    if isinstance(obj, dict):
        return {_key_to_str(k): _to_jsonsafe(v, _depth + 1, _seen) for k, v in obj.items()}

    # namedtuple
    if hasattr(obj, "_asdict") and callable(obj._asdict):
        try:
            return _to_jsonsafe(obj._asdict(), _depth + 1, _seen)
        except Exception:
            pass

    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_to_jsonsafe(v, _depth + 1, _seen) for v in obj]

    if hasattr(obj, "__dict__"):
        try:
            data = {k: _to_jsonsafe(v, _depth + 1, _seen) for k, v in vars(obj).items() if not k.startswith("_")}
            data["__type__"] = type(obj).__name__
            return data
        except Exception:
            pass

    if hasattr(obj, "__slots__"):
        try:
            data = {
                slot: _to_jsonsafe(getattr(obj, slot), _depth + 1, _seen)
                for slot in obj.__slots__
                if hasattr(obj, slot) and not slot.startswith("_")
            }
            data["__type__"] = type(obj).__name__
            return data
        except Exception:
            pass

    try:
        json.dumps(obj)
        return obj
    except Exception:
        return _safe_repr(obj)


def _key_to_str(k: Any) -> str:
    return k if isinstance(k, str) else _safe_repr(k)


def _serialize_bytes(b: bytes, max_len: int = _BYTES_PREVIEW_LEN) -> dict:
    return {
        "__type__": "bytes",
        "size": len(b),
        "sha256": hashlib.sha256(b).hexdigest(),
        "preview_base64": base64.b64encode(b[:max_len]).decode("ascii"),
        "truncated": len(b) > max_len,
    }
