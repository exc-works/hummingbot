"""
Initialize import paths for generated protobuf modules.

See PROTO_IMPORT_FIX.md for details.
"""
import os
import sys

_DEEPPROTO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _DEEPPROTO_ROOT not in sys.path:
    sys.path.insert(0, _DEEPPROTO_ROOT)

_THIRD_PARTY = os.path.join(_DEEPPROTO_ROOT, "third_party")
if os.path.isdir(_THIRD_PARTY) and _THIRD_PARTY not in sys.path:
    sys.path.append(_THIRD_PARTY)
