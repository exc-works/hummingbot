# Ensure http_pb2 DESCRIPTOR is registered before annotations_pb2 consumers import it.
from google.api import http_pb2 as _http_pb2  # noqa: F401

_ = _http_pb2.DESCRIPTOR
