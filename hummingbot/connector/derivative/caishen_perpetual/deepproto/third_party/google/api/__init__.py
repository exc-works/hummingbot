# google.api package
# This __init__.py ensures proto files are imported in the correct order
# to satisfy protobuf descriptor pool dependencies

# Import http_pb2 first (it has no dependencies on other google.api files)
# This must be imported before annotations_pb2 to satisfy descriptor pool dependencies
# We need to import it directly to ensure the DESCRIPTOR is registered
import importlib.util
from pathlib import Path
from google.protobuf import descriptor_pool

# Get the directory containing this __init__.py
_api_dir = Path(__file__).parent

# Import http_pb2 directly from file to ensure it's loaded first
_http_pb2_spec = importlib.util.spec_from_file_location(
    "google.api.http_pb2",
    _api_dir / "http_pb2.py"
)
http_pb2 = importlib.util.module_from_spec(_http_pb2_spec)
_http_pb2_spec.loader.exec_module(http_pb2)

# Force the DESCRIPTOR to be created and registered in the descriptor pool
_ = http_pb2.DESCRIPTOR

# Register http_pb2 DESCRIPTOR with alias 'google/api/http.proto' 
# because annotations_pb2 references it as 'google/api/http.proto'
# but http_pb2.DESCRIPTOR.name is 'third_party/google/api/http.proto'
# We need to re-register it with the correct name
pool = descriptor_pool.Default()
try:
    # Check if 'google/api/http.proto' is already registered
    pool.FindFileByName('google/api/http.proto')
except KeyError:
    # Not found, try to register it by re-serializing with correct name
    try:
        # Get the serialized data from http_pb2.py file
        # The serialized data is in the DESCRIPTOR creation line
        _http_serialized = b'\n!third_party/google/api/http.proto\x12\ngoogle.api\"T\n\x04Http\x12#\n\x05rules\x18\x01 \x03(\x0b\x32\x14.google.api.HttpRule\x12\'\n\x1f\x66ully_decode_reserved_expansion\x18\x02 \x01(\x08\"\x81\x02\n\x08HttpRule\x12\x10\n\x08selector\x18\x01 \x01(\t\x12\r\n\x03get\x18\x02 \x01(\tH\x00\x12\r\n\x03put\x18\x03 \x01(\tH\x00\x12\x0e\n\x04post\x18\x04 \x01(\tH\x00\x12\x10\n\x06\x64\x65lete\x18\x05 \x01(\tH\x00\x12\x0f\n\x05patch\x18\x06 \x01(\tH\x00\x12/\n\x06\x63ustom\x18\x08 \x01(\x0b\x32\x1d.google.api.CustomHttpPatternH\x00\x12\x0c\n\x04\x62ody\x18\x07 \x01(\t\x12\x15\n\rresponse_body\x18\x0c \x01(\t\x12\x31\n\x13\x61\x64\x64itional_bindings\x18\x0b \x03(\x0b\x32\x14.google.api.HttpRuleB\t\n\x07pattern\"/\n\x11\x43ustomHttpPattern\x12\x0c\n\x04kind\x18\x01 \x01(\t\x12\x0c\n\x04path\x18\x02 \x01(\tBj\n\x0e\x63om.google.apiB\tHttpProtoP\x01ZAgoogle.golang.org/genproto/googleapis/api/annotations;annotations\xf8\x01\x01\xa2\x02\x04GAPIb\x06proto3'
        
        # Replace the name in serialized data
        # Format: \n + length_byte + name_string
        # 'third_party/google/api/http.proto' (33 bytes = 0x21)
        # 'google/api/http.proto' (21 bytes = 0x15)
        # We need to replace both the length byte and the name string
        _new_data = bytearray(_http_serialized)
        # Replace length byte from 0x21 (33) to 0x15 (21)
        _new_data[1] = 0x15
        # Replace the name string
        _new_data = bytes(_new_data).replace(
            b'third_party/google/api/http.proto',
            b'google/api/http.proto'
        )
        # Register with the correct name
        pool.AddSerializedFile(_new_data)
    except Exception as e:
        # If that fails, we'll need to handle it in annotations_pb2
        import warnings
        warnings.warn(f"Failed to register http_pb2 with alias: {e}")

# Now import annotations_pb2 (it depends on http_pb2)
_annotations_pb2_spec = importlib.util.spec_from_file_location(
    "google.api.annotations_pb2",
    _api_dir / "annotations_pb2.py"
)
annotations_pb2 = importlib.util.module_from_spec(_annotations_pb2_spec)
_annotations_pb2_spec.loader.exec_module(annotations_pb2)

# Import other proto files
from . import httpbody_pb2
from . import visibility_pb2

__all__ = ['http_pb2', 'annotations_pb2', 'httpbody_pb2', 'visibility_pb2']

