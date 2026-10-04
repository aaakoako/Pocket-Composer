from types import SimpleNamespace
from PySide6.QtNetwork import QNetworkInterface as Interface
from doubao_typeless import runtime


def adapter(name, address, kind=Interface.Ethernet, up=True, index=1):
    flags = Interface.IsUp | Interface.IsRunning if up else Interface.InterfaceFlag(0)
    return SimpleNamespace(flags=lambda: flags, humanReadableName=lambda: name,
        name=lambda: name, type=lambda: kind, index=lambda: index,
        addressEntries=lambda: [SimpleNamespace(ip=lambda: SimpleNamespace(toString=lambda: address))])


def test_lan_precedes_tailscale_and_virtual_private_addresses(monkeypatch):
    monkeypatch.setattr(Interface, 'allInterfaces', lambda: [
        adapter('Tailscale', '100.101.2.3'), adapter('vEthernet', '172.20.1.1'),
        adapter('Wi-Fi', '192.168.8.20', Interface.Wifi), adapter('WireGuard', '10.1.1.2')])
    assert runtime.lan_ip() == '192.168.8.20'
    assert len(runtime.connection_addresses()) == 4
    assert 'VPN' in next(label for ip, label in runtime.connection_addresses() if ip == '100.101.2.3')


def test_disconnected_and_nonreachable_addresses_are_not_advertised(monkeypatch):
    monkeypatch.setattr(Interface, 'allInterfaces', lambda: [
        adapter('Ethernet', '192.168.1.2', up=False), adapter('Ethernet', '169.254.2.1'),
        adapter('Loopback', '127.0.0.1'), adapter('IPv6', 'fe80::1')])
    assert runtime.connection_addresses() == []
    assert runtime.lan_ip() == '127.0.0.1'


def test_overlay_only_remains_available_with_network_hint(monkeypatch):
    monkeypatch.setattr(Interface, 'allInterfaces', lambda: [adapter('Tailscale', '100.101.2.3')])
    assert runtime.lan_ip() == '100.101.2.3'
    assert '需手机接入同一 VPN' in runtime.connection_addresses()[0][1]
