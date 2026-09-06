"""Serializers for different telephony providers."""

from .jambonz_serializer import JambonzFrameSerializer
from .vobiz_serializer import VobizFrameSerializer

__all__ = ["VobizFrameSerializer", "JambonzFrameSerializer"]
