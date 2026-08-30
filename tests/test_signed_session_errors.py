"""A provider's 401 must not cost the user a Turnstile challenge.

Signed-session requests pass through a gateway to a provider, and both can
answer 401. They mean opposite things: the gateway saying it means the
session is dead, the provider saying it means *that track* is unavailable —
no subscription, region-locked, their own token expired. The client used to
treat every 401 as the first case and clear the session, so one
subscription-only track triggered a re-verification that was never needed.

The gateway already distinguishes them in a JSON envelope. These tests
cover reading it, and — just as importantly — the fallback when it is
absent, which has to stay permissive or re-authentication stops working
against a gateway that has not adopted the contract.
"""

from __future__ import annotations

import json

import pytest

from SpotiFLAC.core.signed_session_errors import (
    ACTION_BOOTSTRAP,
    ACTION_VERIFY,
    gateway_action,
    parse_session_error,
    provider_retry_after,
    should_clear_session,
)

GATEWAY_401 = {
    "error": "session invalid",
    "code": "SESSION_INVALID",
    "origin": "gateway",
    "action": "bootstrap_session",
}
GATEWAY_428 = {
    "error": "verification required",
    "code": "VERIFY_REQUIRED",
    "origin": "gateway",
    "action": "verify",
}
PROVIDER_401 = {
    "error": "subscription required",
    "code": "PROVIDER_REJECTED",
    "origin": "provider",
}


# --- the failure this exists for -------------------------------------------


def test_a_providers_401_leaves_the_session_alone() -> None:
    assert not should_clear_session(401, PROVIDER_401)


def test_a_gateways_401_does_clear_the_session() -> None:
    assert should_clear_session(401, GATEWAY_401)
    assert gateway_action(401, parse_session_error(GATEWAY_401)) == ACTION_BOOTSTRAP


def test_a_gateway_428_asks_for_verification() -> None:
    assert gateway_action(428, parse_session_error(GATEWAY_428)) == ACTION_VERIFY


def test_a_gateway_401_with_an_unknown_code_is_not_acted_on() -> None:
    """The envelope names the decision explicitly. An unrecognised code is
    not an invitation to guess at one.
    """
    envelope = {"origin": "gateway", "code": "SOMETHING_ELSE"}
    assert gateway_action(401, parse_session_error(envelope)) == ""


# --- the fallback has to stay permissive -----------------------------------


@pytest.mark.parametrize("body", [b"", b"upstream timeout", {}, None, "not json"])
def test_without_an_envelope_the_old_behaviour_is_kept(body) -> None:
    """A gateway that has not adopted the contract must still be able to
    tell us the session died. Tightening this without checking the envelope
    is universal would break re-authentication outright.
    """
    assert should_clear_session(401, body)
    assert not should_clear_session(403, body)


def test_a_json_body_that_is_not_an_envelope_is_not_an_envelope() -> None:
    """Some gateways answer errors with an unrelated JSON document. Absent
    the contract's own fields, that is the no-envelope case.
    """
    err = parse_session_error({"message": "nope", "status": 401})
    assert not err.present
    assert should_clear_session(401, err and {"message": "nope"})


# --- parsing ---------------------------------------------------------------


def test_the_envelope_is_read_from_bytes_and_from_a_dict() -> None:
    from_dict = parse_session_error(GATEWAY_401)
    from_bytes = parse_session_error(json.dumps(GATEWAY_401).encode())
    assert from_dict == from_bytes
    assert from_dict.from_gateway
    assert not from_dict.from_provider


def test_fields_are_normalised() -> None:
    err = parse_session_error(
        {"code": " session_invalid ", "origin": " GATEWAY ", "action": " Bootstrap "}
    )
    assert err.code == "SESSION_INVALID"
    assert err.origin == "gateway"
    assert err.action == "bootstrap"


@pytest.mark.parametrize("value", [-5, "abc", None])
def test_a_nonsense_retry_after_becomes_zero(value) -> None:
    err = parse_session_error({"code": "X", "retry_after_seconds": value})
    assert err.retry_after_seconds == 0


# --- provider retry --------------------------------------------------------


def test_a_provider_asking_to_be_retried_is_not_a_session_problem() -> None:
    envelope = {
        "origin": "provider",
        "code": "PROVIDER_UNAVAILABLE",
        "retryable": True,
        "retry_mode": "same_operation",
        "retry_after_seconds": 12,
    }
    assert provider_retry_after(503, parse_session_error(envelope)) == 12
    assert not should_clear_session(503, envelope)


def test_a_gateway_503_is_not_a_provider_retry() -> None:
    envelope = {"origin": "gateway", "code": "PROVIDER_UNAVAILABLE", "retryable": True}
    assert provider_retry_after(503, parse_session_error(envelope)) == 0
