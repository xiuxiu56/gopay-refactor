"""外部协议适配层。"""

from .legacy import LegacyProtocolAdapter, ProtocolUnavailableError

__all__ = ["LegacyProtocolAdapter", "ProtocolUnavailableError"]
