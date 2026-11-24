"""Tests of the client configuration and the retry policy."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import pytest

from truvali import ConfigurationError, RetryPolicy, TruvaliConfig
from truvali.config import ENV_API_KEY, ENV_API_SECRET, ENV_BASE_URL

from .fakes import API_KEY, API_SECRET, BASE_URL, make_config


def test_base_url_trailing_slashes_are_removed() -> None:
    """Trailing slashes of the base URL are stripped."""
    config = make_config(base_url=f"{BASE_URL}///")
    assert config.base_url == BASE_URL


def test_base_url_may_carry_a_path_prefix() -> None:
    """A base URL with a path prefix is kept as is."""
    config = make_config(base_url="https://gateway.example.com/truvali/")
    assert config.base_url == "https://gateway.example.com/truvali"


def test_secret_is_hidden_from_repr() -> None:
    """The secret never appears in repr or str."""
    config = make_config()
    assert API_SECRET not in repr(config)
    assert API_SECRET not in str(config)
    assert API_KEY in repr(config)


@pytest.mark.parametrize(
    "base_url",
    ["", "truvali.example.com", "ftp://truvali.example.com", "https://", "https://x.test/?a=1"],
)
def test_invalid_base_url_is_rejected(base_url: str) -> None:
    """Base URLs that are not absolute http(s) URLs are rejected.

    Args:
        base_url: The invalid base URL.
    """
    with pytest.raises(ConfigurationError):
        make_config(base_url=base_url)


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": ""},
        {"api_secret": ""},
        {"api_secret": " secret-0000-test\n"},
        {"connect_timeout": 0},
        {"read_timeout": -1.0},
        {"read_timeout": float("inf")},
        {"connect_timeout": True},
        {"max_batch_size": 0},
        {"max_batch_size": 1001},
        {"max_batch_size": True},
        {"user_agent_suffix": ""},
        {"user_agent_suffix": "app/1.0\r\nX-Injected: 1"},
        {"retry": "fast"},
    ],
)
def test_invalid_settings_are_rejected(overrides: dict[str, Any]) -> None:
    """Out-of-range and malformed settings raise ConfigurationError.

    Args:
        overrides: The invalid settings.
    """
    with pytest.raises(ConfigurationError):
        make_config(**overrides)


def test_max_batch_size_bounds_are_inclusive() -> None:
    """Both 1 and 1000 are accepted batch sizes."""
    assert make_config(max_batch_size=1).max_batch_size == 1
    assert make_config(max_batch_size=1000).max_batch_size == 1000


def test_config_is_frozen_and_replaceable() -> None:
    """The configuration is immutable; replace() validates the copy."""
    config = make_config()
    attribute = "read_timeout"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(config, attribute, 1.0)
    longer = dataclasses.replace(config, read_timeout=60.0)
    assert longer.timeout == (5.0, 60.0)
    with pytest.raises(ConfigurationError):
        dataclasses.replace(config, max_batch_size=5000)


def test_from_env_reads_the_three_variables() -> None:
    """from_env builds a configuration from the documented variables."""
    config = TruvaliConfig.from_env(
        {ENV_BASE_URL: BASE_URL + "/", ENV_API_KEY: API_KEY, ENV_API_SECRET: API_SECRET}
    )
    assert config.base_url == BASE_URL
    assert config.api_key == API_KEY
    assert config.api_secret == API_SECRET


def test_from_env_names_missing_variables_without_values() -> None:
    """Missing variables are named; present values are not echoed."""
    with pytest.raises(ConfigurationError) as caught:
        TruvaliConfig.from_env({ENV_API_SECRET: API_SECRET, ENV_API_KEY: ""})
    message = str(caught.value)
    assert ENV_BASE_URL in message
    assert ENV_API_KEY in message
    assert API_SECRET not in message


def test_retry_policy_backoff_doubles_and_caps() -> None:
    """The backoff doubles per retry and never exceeds backoff_max."""
    policy = RetryPolicy(max_retries=10, backoff_initial=0.5, backoff_max=3.0)
    assert [policy.backoff(retry) for retry in range(5)] == [0.5, 1.0, 2.0, 3.0, 3.0]
    assert policy.backoff(10_000) == 3.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_retries": -1},
        {"max_retries": 1.5},
        {"backoff_initial": -0.1},
        {"backoff_max": 0.1, "backoff_initial": 1.0},
        {"retry_on_timeout": "yes"},
    ],
)
def test_invalid_retry_policy_is_rejected(overrides: dict[str, Any]) -> None:
    """Malformed retry policies raise ConfigurationError.

    Args:
        overrides: The invalid settings.
    """
    with pytest.raises(ConfigurationError):
        RetryPolicy(**overrides)


def test_plain_http_to_a_remote_host_logs_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Plain http is accepted for a remote host but logs one warning.

    Args:
        caplog: The pytest log capture fixture.
    """
    caplog.set_level(logging.WARNING, logger="truvali")
    config = make_config(base_url="http://truvali.example.com/")
    assert config.base_url == "http://truvali.example.com"
    warnings = [record for record in caplog.records if record.name == "truvali"]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert "http://" in warnings[0].getMessage()
    assert API_SECRET not in caplog.text


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000",
        "http://LOCALHOST",
        "http://127.0.0.1:8080/",
        "http://[::1]:8000",
        "https://truvali.example.com",
    ],
)
def test_local_http_and_https_do_not_warn(base_url: str, caplog: pytest.LogCaptureFixture) -> None:
    """Local sandboxes over http and every https URL log nothing.

    Args:
        base_url: The accepted base URL.
        caplog: The pytest log capture fixture.
    """
    caplog.set_level(logging.WARNING, logger="truvali")
    make_config(base_url=base_url)
    assert [record for record in caplog.records if record.name == "truvali"] == []
