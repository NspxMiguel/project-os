"""Merging, checked against what a real house actually answered.

Every case here is a row that showed up on the Devices screen of the Pi on
10/08/2026, when a scan of Miguel's network returned 25 entries for about eleven
things. The screen was not wrong about any single observation -- it was wrong
about how many televisions he owns.
"""

from __future__ import annotations

from project_os.core.discovery import Observation, merge_observations


def mdns(kind, name, address, token, service_type="_googlecast._tcp.local.", port=8009):
    return Observation(
        source="mdns",
        service_type=service_type,
        instance=name,
        kind=kind,
        name_hint=name,
        address=address,
        port=port,
        properties={"fn": name},
        capabilities=[],
        strong_keys=[token],
        weak_keys=["addr:" + address],
    )


def neighbour(address, token):
    """What lan.scan_neighbours() produces: an address that replied to ARP."""
    return Observation(
        source="neighbour",
        kind="unknown",
        instance=address,
        name_hint=address,
        address=address,
        properties={"mac": "aa:bb:cc:dd:ee:ff"},
        strong_keys=[token],
        weak_keys=["addr:" + address],
        anonymous=True,
    )


def names(devices):
    return sorted((d.kind, d.name) for d in devices)


def test_a_television_is_one_device_not_three():
    """Chromecast + Android TV + an ARP hit, all at 10.0.0.25, all "TV Cozinha"."""
    devices = merge_observations([
        mdns("chromecast", "TV Cozinha", "10.0.0.25", "uuid:ae7826ac9e8a32"),
        mdns("tv", "TV Cozinha", "10.0.0.25", "id:android6466",
             service_type="_androidtvremote2._tcp.local.", port=6466),
        neighbour("10.0.0.25", "mac:08626607616e"),
    ])
    assert len(devices) == 1, names(devices)
    assert devices[0].name == "TV Cozinha"


def test_an_apple_tv_does_not_get_a_twin_called_by_its_address():
    """One named service plus one anonymous neighbour is one device."""
    devices = merge_observations([
        mdns("apple_tv", "Quarto miguel", "10.0.0.44", "mac:c2ffe4e5e4be",
             service_type="_airplay._tcp.local.", port=7000),
        neighbour("10.0.0.44", "mac:eca90702016c"),
    ])
    assert len(devices) == 1, names(devices)
    assert devices[0].name == "Quarto miguel"
    assert devices[0].kind == "apple_tv"


def test_a_homepod_answering_two_protocols_is_one_speaker():
    devices = merge_observations([
        mdns("homepod", "HomePod Direito", "10.0.0.68", "id:homepod-direito",
             service_type="_airplay._tcp.local.", port=7000),
        mdns("homepod", "HomePod Direito", "10.0.0.68", "mac:22d61dba1645",
             service_type="_raop._tcp.local.", port=49153),
    ])
    assert len(devices) == 1, names(devices)


def test_two_different_devices_on_one_address_stay_apart():
    """The name rule needs the *same* name. Different names are different things.

    This is the case the conservative rule was written for and it still holds:
    a host running two services that call themselves different things is not
    something to collapse on a hunch.
    """
    devices = merge_observations([
        mdns("chromecast", "Sala", "10.0.0.9", "uuid:aaaa1111"),
        mdns("speaker", "Cozinha", "10.0.0.9", "uuid:bbbb2222"),
    ])
    assert len(devices) == 2, names(devices)


def test_anonymous_neighbours_alone_are_still_listed():
    """An address with nothing else on it is the only thing worth showing."""
    devices = merge_observations([neighbour("10.0.0.1", "mac:0011223344ab")])
    assert len(devices) == 1
    assert devices[0].kind == "unknown"


def test_two_unnamed_neighbours_do_not_merge_by_their_placeholder_names():
    """Their "name" is their address; addresses are not names."""
    devices = merge_observations([
        neighbour("10.0.0.1", "mac:0011223344ab"),
        neighbour("10.0.0.2", "mac:0011223344cd"),
    ])
    assert len(devices) == 2


def test_the_whole_house_collapses_to_the_number_of_real_things():
    """The eight addresses that were listed 2-3 times each, together."""
    observations = []
    for address, uuid, tv_id, mac, name in [
        ("10.0.0.105", "uuid:e8f0ed5b8caab9", "id:android3af1", "mac:8e4c1a3de81c", "Laila"),
        ("10.0.0.25", "uuid:ae7826ac9e8a32", "id:android6466", "mac:08626607616e", "TV Cozinha"),
        ("10.0.0.95", "uuid:4dc107d4bfbc49", "id:androidf9dd", "mac:089b27142a3c",
         "TV Quarto Miguel"),
    ]:
        observations.append(mdns("chromecast", name, address, uuid))
        observations.append(mdns("tv", name, address, tv_id,
                                 service_type="_androidtvremote2._tcp.local.", port=6466))
        observations.append(neighbour(address, mac))
    observations.append(mdns("apple_tv", "Miguel's MacBook Pro", "10.0.0.72", "mac:4a03a92b7de7",
                             service_type="_airplay._tcp.local.", port=7000))
    observations.append(neighbour("10.0.0.72", "mac:2e4b87eb5d76"))
    observations.append(mdns("cast_audio", "Banheiro Suite", "10.0.0.37", "uuid:a2782d9fa8ed"))
    observations.append(neighbour("10.0.0.37", "mac:20dfb9577394"))

    devices = merge_observations(observations)
    assert len(devices) == 5, names(devices)
    assert sorted(d.name for d in devices) == [
        "Banheiro Suite", "Laila", "Miguel's MacBook Pro", "TV Cozinha", "TV Quarto Miguel",
    ]


def unnamed_service(kind, name, address, service_type):
    """A service with a name but no identity of its own: _companion-link."""
    return Observation(
        source="mdns",
        service_type=service_type,
        instance=name,
        kind=kind,
        name_hint=name,
        address=address,
        port=49152,
        properties={},
        strong_keys=[],
        weak_keys=["host:" + name.lower().replace(" ", "-"), "addr:" + address],
    )


def test_a_nameless_service_joins_the_device_that_shares_its_name():
    """HomePod Direito answered _companion-link with no id and became a twin.

    Two identified devices sit on that address -- the speaker and its HomeKit
    accessory -- so the "exactly one identified device here" rule did not apply
    and the anonymous service was left standing on its own.
    """
    devices = merge_observations([
        mdns("homepod", "HomePod Direito", "10.0.0.68", "mac:22d61dba1645",
             service_type="_airplay._tcp.local.", port=7000),
        mdns("homepod", "HomePod Direito", "10.0.0.68", "mac:22d61dba1645",
             service_type="_raop._tcp.local.", port=49153),
        mdns("homekit", "HomePodSensor 197688", "10.0.0.68", "mac:1a4c18d866f4",
             service_type="_hap._tcp.local.", port=80),
        unnamed_service("homepod", "HomePod Direito", "10.0.0.68",
                        "_companion-link._tcp.local."),
    ])
    assert sorted(d.name for d in devices) == ["HomePod Direito", "HomePodSensor 197688"]


def test_a_nameless_service_matching_nobody_is_still_its_own_row():
    """No name to match means no evidence: it stays, rather than being guessed at."""
    devices = merge_observations([
        mdns("homepod", "HomePod Direito", "10.0.0.68", "mac:22d61dba1645",
             service_type="_airplay._tcp.local.", port=7000),
        mdns("homekit", "HomePodSensor 197688", "10.0.0.68", "mac:1a4c18d866f4",
             service_type="_hap._tcp.local.", port=80),
        unnamed_service("speaker", "Alguma outra coisa", "10.0.0.68", "_sleep-proxy._udp.local."),
    ])
    assert len(devices) == 3, names(devices)


def test_an_arp_hit_is_dropped_where_two_named_devices_already_live():
    """It cannot say which of them it is, and it has nothing else to add.

    Merging it into one of them would pin that MAC on the wrong device; leaving
    it standing puts a row called "10.0.0.68" under "HomePod Direito".
    """
    devices = merge_observations([
        mdns("homepod", "HomePod Direito", "10.0.0.68", "mac:22d61dba1645",
             service_type="_airplay._tcp.local.", port=7000),
        mdns("homekit", "HomePodSensor 197688", "10.0.0.68", "mac:1a4c18d866f4",
             service_type="_hap._tcp.local.", port=80),
        neighbour("10.0.0.68", "mac:aabbccddeeff"),
    ])
    assert sorted(d.name for d in devices) == ["HomePod Direito", "HomePodSensor 197688"]


def test_an_arp_hit_at_an_address_nobody_claimed_is_kept():
    """That row is the only evidence the thing exists."""
    devices = merge_observations([
        mdns("homepod", "HomePod Direito", "10.0.0.68", "mac:22d61dba1645",
             service_type="_airplay._tcp.local.", port=7000),
        neighbour("10.0.0.200", "mac:aabbccddeeff"),
    ])
    assert len(devices) == 2, names(devices)
    assert "10.0.0.200" in [d.address for d in devices]
