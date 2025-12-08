# deepproto package
# This module provides path redirection for proto imports
# 
# Import strategy:
# - google.protobuf: Use system package from site-packages (highest priority)
# - google.api: Use from third_party/google/api (for annotations_pb2, etc.)
# - gogoproto: Use from third_party/gogoproto
# - schema: Use from deepproto/schema (relative imports)

import sys
from pathlib import Path

# Get the directory containing this __init__.py
_deepproto_dir = Path(__file__).parent

# IMPORTANT: Import system google.protobuf FIRST before modifying sys.path
# This ensures it's loaded from site-packages and cached in sys.modules
try:
    import google.protobuf
    # Force import of descriptor to ensure it's available
    from google.protobuf import descriptor
except ImportError:
    pass  # Will be imported later

# Add deepproto itself to sys.path for relative imports like "from schema import ..."
# This must be added first for relative imports to work
_deepproto_path = str(_deepproto_dir)
if _deepproto_path not in sys.path:
    sys.path.insert(1, _deepproto_path)

# Add third_party to sys.path so imports like "from google.api import ..." and "from gogoproto import ..." work
# We insert it AFTER site-packages so that google.protobuf uses the system version,
# but google.api and gogoproto can still be found in third_party
_third_party_path = str(_deepproto_dir / "third_party")
if _third_party_path not in sys.path:
    # Find the position of site-packages in sys.path
    site_packages_idx = None
    for i, path in enumerate(sys.path):
        if 'site-packages' in path:
            site_packages_idx = i
            break
    
    if site_packages_idx is not None:
        # Insert AFTER site-packages so system google.protobuf takes precedence
        # but third_party/google/api and third_party/gogoproto are still accessible
        sys.path.insert(site_packages_idx + 1, _third_party_path)
    else:
        # Fallback: insert at position 1 (after current directory)
        sys.path.insert(1, _third_party_path)

# Ensure google namespace package includes third_party/google
# This allows imports like "from google.api import ..." to work
if 'google' in sys.modules:
    google = sys.modules['google']
    # Add third_party/google to google namespace package __path__
    if hasattr(google, '__path__'):
        _third_party_google_path = str(_deepproto_dir / "third_party" / "google")
        if _third_party_google_path not in google.__path__:
            # Convert to list, add path, convert back
            _path_list = list(google.__path__)
            _path_list.append(_third_party_google_path)
            google.__path__ = _path_list
    # Ensure google.protobuf is available from system package
    if not hasattr(google, 'protobuf') or not hasattr(google.protobuf, 'descriptor'):
        import importlib
        # Force reload from system
        if 'google.protobuf' in sys.modules:
            del sys.modules['google.protobuf']
        import google.protobuf
        google.protobuf = google.protobuf
else:
    # If google is not in sys.modules yet, we need to create the namespace
    # But this should not happen as system google should be imported first
    pass

