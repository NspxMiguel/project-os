"""The SSDP classifier and the neighbour table, without a network.

Every test here feeds bytes or files to the parsers. Nothing sends a packet:
a test that depends on what happens to be plugged in at home passes on my
machine and fails on yours, which is the least useful kind of test there is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from projectos.core import discovery, lan


def ssdp_reply(**headers: str) -> bytes:
    lines = ["HTTP/1.1 200 OK"]
    lines += ["%s: %s" % (key.upper(), value) for key, value in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


# --------------------------------------------------------------------- SSDP
def test_headers_are_parsed_case_insensitively() -> None:
    headers = lan.parse_ssdp(ssdp_reply(server="Foo/1.0", location="http://1.2.3.4:8060/"))
    assert headers["server"] == "Foo/1.0"
    assert headers["location"] == "http://1.2.3.4:8060/"


def test_a_reply_that_is_not_ssdp_is_ignored() -> None:
    assert lan.parse_ssdp(b"\x00\x01garbage") == {}


@pytest.mark.parametrize(
    "server,expected",
    [
        ("PlayStation 5/1.0 UPnP/1.0", lan.GAME_CONSOLE),
        ("Xbox/10.0 UPnP/1.0", lan.GAME_CONSOLE),
        ("OctoPrint/1.9.3", lan.PRINTER_3D),
        ("Jellyfin/10.8", lan.MEDIA_SERVER),
        ("Synology/DSM", lan.NAS),
        ("Linux/4.4 UPnP/1.0 MikroTik/6.49", lan.ROUTER),
        ("Roku/12.0 UPnP/1.0", "tv"),
    ],
)
def test_known_boxes_are_named_by_their_server_header(server: str, expected: str) -> None:
    kind, _ = lan.classify_ssdp({"server": server})
    assert kind == expected


def test_something_unrecognised_is_a_web_service_not_unknown() -> None:
    """It answered SSDP, so it has a description page. Saying "unknown" would
    hide the one useful thing we know about it."""
    kind, vendor = lan.classify_ssdp({"server": "Zonk/2.1 UPnP/1.0"})
    assert kind == "web_service"
    assert vendor == ""


def test_a_reply_becomes_an_observation_with_the_uuid_as_identity() -> None:
    payload = ssdp_reply(
        server="PlayStation 5/1.0",
        usn="uuid:5f9a1c30-0000-1000-8000-001122334455::upnp:rootdevice",
        location="http://192.168.0.40:9295/desc.xml",
    )
    observation = lan._ssdp_observation(payload, "192.168.0.40")
    assert observation is not None
    assert observation.kind == lan.GAME_CONSOLE
    assert observation.name_hint == "PlayStation"
    assert observation.port == 9295
    # the uuid is stable across reboots and DHCP leases; the address is not
    assert observation.strong_keys
    assert observation.weak_keys == ["addr:192.168.0.40"]


def test_the_specific_answer_wins_over_the_generic_one(monkeypatch) -> None:
    """One box answers every M-SEARCH it matches. The dashboard should end up
    saying "PlayStation", not "something with a web page"."""
    replies = [
        (ssdp_reply(server="Linux UPnP/1.0", st="upnp:rootdevice"), ("192.168.0.40", 1900)),
        (ssdp_reply(server="PlayStation 5/1.0", st="ssdp:all"), ("192.168.0.40", 1900)),
    ]

    class FakeSocket:
        def setsockopt(self, *_a): pass
        def settimeout(self, *_a): pass
        def sendto(self, *_a): pass
        def close(self): pass

        def recvfrom(self, _size):
            if replies:
                return replies.pop(0)
            raise OSError("done")

    monkeypatch.setattr(lan.socket, "socket", lambda *a, **k: FakeSocket())
    found = lan.scan_ssdp(timeout=1.0)
    assert [obs.kind for obs in found] == [lan.GAME_CONSOLE]


# ---------------------------------------------------------- neighbour table
ARP_SAMPLE = """\
IP address       HW type     Flags       HW address            Mask     Device
192.168.0.1      0x1         0x2         c4:eb:ff:e6:50:a9     *        wlan0
192.168.0.31     0x1         0x2         a4:83:e7:11:22:33     *        wlan0
192.168.0.99     0x1         0x0         00:00:00:00:00:00     *        wlan0
"""


def test_incomplete_entries_are_dropped(tmp_path: Path, monkeypatch) -> None:
    """An all-zero MAC means the ARP request never got an answer. Listing it
    would put a device on the dashboard that does not exist."""
    path = tmp_path / "arp"
    path.write_text(ARP_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(lan, "NEIGHBOUR_FILE", str(path))
    assert [ip for ip, _ in lan.neighbours()] == ["192.168.0.1", "192.168.0.31"]


def test_neighbours_are_observed_as_unknown_with_the_mac_as_identity(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "arp"
    path.write_text(ARP_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(lan, "NEIGHBOUR_FILE", str(path))
    monkeypatch.setattr(lan, "_reverse_name", lambda _address: "")
    monkeypatch.setattr(lan, "_own_addresses", lambda: [])
    found = lan.scan_neighbours()
    assert len(found) == 2
    # deliberately unknown: a MAC does not tell you a phone from a laptop
    assert {obs.kind for obs in found} == {"unknown"}
    assert found[0].properties["mac"] == "c4:eb:ff:e6:50:a9"
    assert found[0].strong_keys


def test_our_own_addresses_are_not_listed_as_devices(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "arp"
    path.write_text(ARP_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(lan, "NEIGHBOUR_FILE", str(path))
    monkeypatch.setattr(lan, "_reverse_name", lambda _address: "")
    monkeypatch.setattr(lan, "_own_addresses", lambda: ["192.168.0.31"])
    assert [obs.address for obs in lan.scan_neighbours()] == ["192.168.0.1"]


# ------------------------------------------------------------------ vendors
def test_no_oui_database_means_no_vendor_rather_than_a_guess(monkeypatch) -> None:
    """A wrong vendor is worse than a blank one: "Sony" next to the neighbour's
    fridge is a bug nobody can see."""
    monkeypatch.setattr(lan, "OUI_FILES", ())
    lan.reset_cache()
    assert lan.vendor_of("a4:83:e7:11:22:33") is None
    status = lan.oui_available()
    assert status["available"] is False
    assert status["install_hint"]
    lan.reset_cache()


@pytest.mark.parametrize(
    "line",
    [
        "A483E7\tApple, Inc.",                       # nmap / arp-scan
        "A4-83-E7   (hex)\t\tApple, Inc.",           # ieee-data
    ],
)
def test_both_oui_file_formats_are_understood(line: str, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "oui.txt"
    path.write_text("# a comment\n\n" + line + "\n", encoding="utf-8")
    monkeypatch.setattr(lan, "OUI_FILES", (str(path),))
    lan.reset_cache()
    assert lan.vendor_of("a4:83:e7:11:22:33") == "Apple, Inc."
    assert lan.oui_available()["available"] is True
    lan.reset_cache()


# ------------------------------------------------------------- registration
def test_the_lan_scanners_are_part_of_a_scan() -> None:
    names = [name for name, _ in discovery.all_scanners()]
    assert "ssdp" in names and "neighbours" in names


def test_every_kind_the_lan_scanners_emit_is_known_to_discovery() -> None:
    """A kind missing from these tables renders as a blank row with no icon."""
    kinds = {kind for _, kind, _ in lan.SSDP_SIGNATURES} | set(lan.EXTRA_KIND_CAPABILITIES)
    for kind in kinds:
        assert kind in discovery.KIND_CAPABILITIES, kind
        assert kind in discovery.KIND_PRIORITY, kind


def test_multicast_and_broadcast_addresses_are_not_devices():
    """The ARP table carries the groups mDNS and SSDP talk to.

    Listing them turned "everything on your network" into a screen where real
    finds sat between rows like "232.17.191.193, unknown, online".
    """
    from projectos.core import lan

    assert lan.is_real_host("10.0.0.95")
    assert lan.is_real_host("127.0.0.1")
    assert not lan.is_real_host("224.0.0.251")   # mDNS
    assert not lan.is_real_host("239.255.255.250")  # SSDP
    assert not lan.is_real_host("255.255.255.255")
    assert not lan.is_real_host("10.0.0.255")
    assert not lan.is_real_host("169.254.9.9")


def test_neighbours_drops_the_multicast_rows(monkeypatch):
    from projectos.core import lan

    monkeypatch.setattr(lan, "_read_proc_arp", lambda: [
        ("10.0.0.95", "3c:e8:75:a5:82:99"),
        ("224.0.0.251", "01:00:5e:00:00:fb"),
        ("239.255.255.250", "01:00:5e:7f:ff:fa"),
    ])
    assert [ip for ip, _ in lan.neighbours()] == ["10.0.0.95"]
