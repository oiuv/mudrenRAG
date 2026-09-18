"""服务监听配置；不加载模型或数据库配置，供启动与自测共用。"""
import ipaddress
import os
import re
from dataclasses import dataclass

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8008


@dataclass(frozen=True)
class ServerSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @classmethod
    def from_env(cls):
        host = os.getenv("HOST", DEFAULT_HOST).strip()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            labels = host.rstrip(".").split(".")
            if not host or len(host) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in labels
            ):
                raise ValueError("HOST 必须是有效的 IP 地址或主机名，不要包含协议、端口或路径。") from None
        try:
            port = int(os.getenv("PORT", str(DEFAULT_PORT)))
        except ValueError:
            raise ValueError("PORT 必须是 1 到 65535 之间的整数。") from None
        if not 1 <= port <= 65535:
            raise ValueError("PORT 必须是 1 到 65535 之间的整数。")
        return cls(host=host, port=port)

    @property
    def local_url(self) -> str:
        host = self.host
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if address.is_unspecified:
                host = "127.0.0.1" if address.version == 4 else "::1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"
