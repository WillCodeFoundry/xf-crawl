"""Moving from TunnelBear server to server, against a stand-in account."""

import random

import pytest

from xfcrawl.proxy import ProxyDown
from xfcrawl.tunnelbear.account import Endpoints, Server, TunnelBearError
from xfcrawl.tunnelbear.rotation import REQUESTS_PER_SERVER, ServerRotation, Turns

SERVERS = [Server(f"{country.lower()}{n}.example", country) for country in ("FR", "DE", "NL") for n in (1, 2)]


class StandInAccount:
    endpoints = Endpoints.from_environment({})

    def __init__(self) -> None:
        self.servers = list(SERVERS)
        self.listings = self.tokens = 0
        self.listing_fails = False

    def vpn_token(self) -> str:
        self.tokens += 1
        return f"tok{self.tokens}"

    def all_servers(self) -> list[Server]:
        if self.listing_fails:
            raise TunnelBearError("down", 503)
        self.listings += 1
        return list(self.servers)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def rotation(account=None, turns=None, clock=None) -> ServerRotation:
    return ServerRotation(account or StandInAccount(), turns or Turns(requests_per_server=3), clock=clock or Clock(),
                          rng=random.Random(4))


def hosts_used(route: ServerRotation, requests: int) -> list[str]:
    used = []
    for _ in range(requests):
        route.proxies()
        used.append(route.host)
    return used


def test_the_default_is_8000_requests_a_server():
    assert REQUESTS_PER_SERVER == 8000
    assert Turns().requests_per_server == REQUESTS_PER_SERVER


def test_each_server_gets_its_share_of_requests_then_the_next_one_does():
    used = hosts_used(rotation(), 3 * len(SERVERS))
    shares = [used[i:i + 3] for i in range(0, len(used), 3)]
    assert all(len(set(share)) == 1 for share in shares)
    assert sorted(share[0] for share in shares) == sorted(s.host for s in SERVERS)   # every one, once


def test_the_order_is_shuffled_across_countries():
    order = [server.host for server in rotation().servers]
    assert sorted(order) == sorted(s.host for s in SERVERS)
    assert order != [s.host for s in SERVERS]
    assert [s.host for s in ServerRotation(StandInAccount(), rng=random.Random(5)).servers] != order


def test_a_failed_server_is_left_at_once_and_the_next_gets_a_full_share():
    route = rotation()
    first = hosts_used(route, 2)[0]
    route.failed(auth_problem=False)
    after = hosts_used(route, 4)
    assert first not in after
    assert len(set(after[:3])) == 1 and after[3] != after[0]


def test_tokens_are_renewed_on_time_and_the_server_list_rarely():
    account, clock = StandInAccount(), Clock()
    route = rotation(account, Turns(requests_per_server=10, renew_every=300, relist_every=3600), clock)
    assert (account.tokens, account.listings) == (1, 1)
    clock.now = 301
    route.proxies()
    assert (account.tokens, account.listings) == (2, 1)
    clock.now = 3601
    route.proxies()
    assert (account.tokens, account.listings) == (3, 2)


def test_relisting_stays_on_the_current_server_if_still_listed():
    account, clock = StandInAccount(), Clock()
    route = rotation(account, Turns(requests_per_server=10, relist_every=60), clock)
    current = hosts_used(route, 1)[0]
    clock.now = 61
    assert hosts_used(route, 1) == [current]
    account.servers = [s for s in SERVERS if s.host != current]
    clock.now = 200
    assert hosts_used(route, 1)[0] != current


def test_failing_to_list_servers_takes_the_route_down():
    account, clock = StandInAccount(), Clock()
    route = rotation(account, Turns(relist_every=60), clock)
    account.listing_fails = True
    clock.now = 61
    with pytest.raises(ProxyDown, match="could not list the TunnelBear servers"):
        route.proxies()


def test_a_request_tries_at_most_ten_servers_before_the_route_is_down():
    account = StandInAccount()
    account.servers = [Server(f"s{n}.example", "FR") for n in range(300)]
    assert rotation(account).max_failures == 21
    assert rotation().max_failures == 2 * len(SERVERS) + 1


def test_it_says_where_it_is():
    route = rotation()
    assert route.describe().startswith("TunnelBear, 6 servers in 3 countries, using ")
