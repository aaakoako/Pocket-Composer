"""Isolated V3 data directory. Never the daily-use config.json location."""
from __future__ import annotations

import os
import socket
from pathlib import Path

PREVIEW_NAME = "preview-v3"
RELEASE_DATA_NAME = "workspace-v3"


def configure_launch(argv: list[str]) -> None:
    """Keep explicitly selected preview storage and IPC identity across Windows login."""
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir")
    parser.add_argument("--instance-name")
    options, _ = parser.parse_known_args(argv[1:])
    if options.data_dir:
        os.environ["DT_V3_DATA_DIR"] = str(Path(options.data_dir).resolve())
    if options.instance_name:
        os.environ["DT_V3_PIPE"] = options.instance_name


def daily_use_config_candidates() -> list[Path]:
    out: list[Path] = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            out.append(Path(base) / "DoubaoTypeless" / "config.json")
    return out


def v3_data_dir() -> Path:
    override = os.environ.get("DT_V3_DATA_DIR", "").strip()
    if override:
        return Path(override)
    from doubao_typeless.build_info import release_layout
    name = RELEASE_DATA_NAME if release_layout() else PREVIEW_NAME
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "DoubaoTypeless" / name
    return Path.home() / "DoubaoTypeless" / name


def pick_port(preferred: int = 8766) -> int:
    for port in range(preferred, preferred + 16):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            continue
        else:
            return port
        finally:
            sock.close()
    raise OSError("no free v3 port; will not kill the other instance")


def lan_ip() -> str:
    addresses = connection_addresses()
    return addresses[0][0] if addresses else "127.0.0.1"


def connection_addresses() -> list[tuple[str, str]]:
    """优先列出已连接的局域网网卡；VPN 地址保留为显式备选。"""
    from ipaddress import ip_address, ip_network
    from PySide6.QtNetwork import QNetworkInterface as Interface

    private = tuple(ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    overlay = ip_network('100.64.0.0/10')
    virtual_names = ('tailscale', 'wireguard', 'wintun', 'vpn', 'tun', 'tap',
                     'virtual', 'vmware', 'vethernet', 'docker', 'zerotier', 'clash')
    ranked = []
    for interface in Interface.allInterfaces():
        flags = interface.flags()
        if not (flags & Interface.IsUp and flags & Interface.IsRunning) or flags & Interface.IsLoopBack:
            continue
        name = interface.humanReadableName()
        virtual = any(word in (name + ' ' + interface.name()).lower() for word in virtual_names)
        physical = interface.type() in (Interface.Ethernet, Interface.Wifi) and not virtual
        for entry in interface.addressEntries():
            try:
                address = ip_address(entry.ip().toString())
            except ValueError:
                continue
            if address.version != 4 or address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
                continue
            local = any(address in network for network in private)
            vpn = virtual or address in overlay
            rank = (0 if physical and local else 1 if physical and not vpn else
                    2 if local and not vpn else 3 if local else 4)
            hint = '（需手机接入同一 VPN）' if address in overlay or 'tailscale' in name.lower() else '（虚拟网络，需确认手机可达）'
            label = f'{name} · {address}' + (hint if vpn else '')
            ranked.append((rank, interface.index(), str(address), label))
    seen = set()
    result = []
    for _, _, address, label in sorted(ranked):
        if address not in seen:
            seen.add(address)
            result.append((address, label))
    return result
