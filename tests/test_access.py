import pytest

from dct_hub.auth.access import AccessPolicy, Forbidden, Unauthenticated, require
from dct_hub.auth.config import AccessConfig
from dct_hub.auth.identity import Identity


def make_policy() -> AccessPolicy:
    config = AccessConfig(
        grants=[
            {"path": "*", "role": "viewer", "groups": ["data"]},
            {"path": "finance/*", "role": "operator", "groups": ["finance"]},
            {"path": "restricted/board", "role": "admin", "users": ["ceo@corp.test"]},
        ]
    )
    return AccessPolicy(config)


def ident(sub="u1", email="", groups=()) -> Identity:
    return Identity(sub=sub, name=sub, email=email, groups=frozenset(groups))


def test_wildcard_viewer():
    policy = make_policy()
    assert policy.allows(ident(groups=["data"]), "view", "anything")
    assert not policy.allows(ident(groups=["data"]), "refresh", "anything")


def test_more_specific_grant_wins():
    policy = make_policy()
    assert policy.allows(ident(groups=["data", "finance"]), "refresh", "finance/q4")
    assert policy.allows(ident(groups=["finance"]), "view", "finance/q4")  # operator includes view
    assert not policy.allows(ident(groups=["finance"]), "view", "other/board")  # finance group, no data grant


def test_no_matching_group_denies():
    policy = make_policy()
    assert policy.capabilities(ident(groups=["marketing"]), "finance/q4") == set()


def test_specific_grant_shadows_wildcard():
    policy = make_policy()
    # `*` grants view to data, but the exact `restricted/board` grant names only
    # the CEO and shadows it — carve-outs must not fall through to the wildcard.
    assert not policy.allows(ident(groups=["data"]), "view", "restricted/board")
    assert policy.allows(ident(sub="x", email="ceo@corp.test"), "manage", "restricted/board")


def test_exact_match_and_user_grant():
    policy = make_policy()
    assert policy.allows(ident(sub="x", email="ceo@corp.test"), "manage", "restricted/board")
    assert not policy.allows(ident(sub="x", email="ceo@corp.test"), "view", "restricted/other")


def test_glob_does_not_match_sibling_prefix():
    policy = make_policy()
    assert not policy.allows(ident(groups=["finance"]), "refresh", "finance2/board")


def test_require_raises():
    policy = make_policy()
    with pytest.raises(Unauthenticated):
        require(policy, None, "view", "x")
    with pytest.raises(Forbidden):
        require(policy, ident(groups=["data"]), "refresh", "x")
    require(policy, ident(groups=["data"]), "view", "x")  # no raise
    require(None, None, "view", "x")  # auth disabled: no-op


def test_unknown_role_rejected():
    with pytest.raises(ValueError, match="unknown roles"):
        AccessConfig(grants=[{"path": "*", "role": "superuser", "groups": ["data"]}])


def test_grant_needs_groups_or_users():
    with pytest.raises(ValueError):
        AccessConfig(grants=[{"path": "*", "role": "viewer"}])
