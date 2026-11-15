"""P3B-02: routing is a table, and the table is the allowlist.

The claims here are the ones a reviewer can check without trusting the server: which upstream a path goes to, what
path the upstream then sees, that an unknown name is an error rather than a fallback, that a non-loopback bind needs to
be said out loud, and that the reachable destinations are exactly the configured set.
"""
from __future__ import annotations

import pytest

from blackbox.proxy.routes import Route, RouteError, Routes, check_bind, is_ours


def table(*, default: str | None = "openai") -> Routes:
    return Routes.from_specs([
        "openai=https://api.openai.com",
        "anthropic=https://api.anthropic.com",
        "local=http://127.0.0.1:8000/v1@/local",
    ], default=default)


def test_the_single_default_route_takes_everything_that_no_prefix_claims():
    routes = table()
    route = routes.resolve("/v1/chat/completions", [])
    assert route.name == "openai"
    assert route.target_path("/v1/chat/completions") == "/v1/chat/completions", "no prefix, no rewrite"


def test_the_longest_prefix_wins_and_is_stripped_into_the_base_path():
    routes = table()
    route = routes.resolve("/local/v1/models", [])
    assert route.name == "local"
    assert route.target_path("/local/v1/models") == "/v1/v1/models", "base path + the path with the prefix removed"
    assert route.target_path("/local") == "/v1", "the bare prefix is the base path, not nothing"
    assert route.target_path("/local/") == "/v1/"


def test_the_header_names_a_route_and_an_unknown_name_is_refused_rather_than_fallen_back_on():
    routes = table()
    assert routes.resolve("/v1/models", [("X-Blackbox-Upstream", "anthropic")]).name == "anthropic"
    with pytest.raises(RouteError) as caught:
        routes.resolve("/v1/models", [("x-blackbox-upstream", "gemini")])
    assert caught.value.code == "BBX-E140"
    assert caught.value.status == 502 and caught.value.reason == "upstream-not-allowed"


def test_no_default_and_no_match_is_an_error_not_a_guess():
    routes = table(default=None)
    with pytest.raises(RouteError) as caught:
        routes.resolve("/v1/models", [])
    assert caught.value.reason == "upstream-not-allowed"


def test_the_bind_is_loopback_unless_the_caller_says_otherwise_in_so_many_words():
    assert check_bind("127.0.0.1") == "127.0.0.1"
    assert check_bind("::1") == "::1"
    with pytest.raises(RouteError) as caught:
        check_bind(".".join(["0"] * 4))
    assert caught.value.code == "BBX-E143"
    assert check_bind(".".join(["0"] * 4), unsafe=True) == ".".join(["0"] * 4)


def test_destinations_are_a_closed_set_and_that_is_the_allowlist():
    routes = table()
    assert routes.destinations() == {("api.openai.com", 443), ("api.anthropic.com", 443), ("127.0.0.1", 8000)}
    assert routes.names() == ["anthropic", "local", "openai"]


def test_a_route_may_skip_verification_and_then_be_hardened_again_by_one_call():
    routes = Routes.from_specs(["local=http://127.0.0.1:8000"], default="local", unverified=("local",))
    assert routes.route("local").verify_tls is False
    routes.verify_all()
    assert routes.route("local").verify_tls is True, "the only direction this table moves is towards verification"


def test_a_base_url_that_is_not_http_or_has_no_host_is_refused_at_construction():
    for bad in ("ftp://example.com", "api.openai.com", "https://api.openai.com?x=1"):
        with pytest.raises(RouteError):
            Route(name="bad", base=bad)
    with pytest.raises(RouteError, match="two upstreams"):
        Routes([Route(name="a", base="http://127.0.0.1:1"), Route(name="a", base="http://127.0.0.1:2")])
    with pytest.raises(RouteError, match="default route"):
        Routes([Route(name="a", base="http://127.0.0.1:1")], default="b")


def test_our_headers_are_ours_in_either_direction():
    assert is_ours("X-Blackbox-Run") and is_ours("x-blackbox-upstream")
    assert not is_ours("authorization") and not is_ours("x-request-id")


def test_the_table_reports_itself_in_a_form_the_docs_can_print():
    described = table().as_dict()
    assert described["default"] == "openai"
    assert described["upstreams"]["local"] == {"base": "http://127.0.0.1:8000/v1", "prefix": "/local",
                                               "strip": True, "verify_tls": True}
    assert described["bind"] == "127.0.0.1"
