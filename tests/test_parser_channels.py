import pytest

from parser_channels import normalize_channel, parse_channel_command


def test_normalize_channel_accepts_common_telegram_formats():
    assert normalize_channel("@hse_events") == "hse_events"
    assert normalize_channel("t.me/hse_events") == "hse_events"
    assert normalize_channel("https://t.me/hse_events/") == "hse_events"


def test_normalize_channel_rejects_invalid_values():
    with pytest.raises(ValueError):
        normalize_channel("not a channel")
    with pytest.raises(ValueError):
        normalize_channel("https://example.com/channel")


def test_parse_channel_command_extracts_username():
    assert parse_channel_command("/parser_add @hse_events") == "hse_events"
    assert parse_channel_command("/parser_remove https://t.me/hse_events/") == "hse_events"


def test_parse_channel_command_requires_one_argument():
    with pytest.raises(ValueError):
        parse_channel_command("/parser_add")
    with pytest.raises(ValueError):
        parse_channel_command("/parser_add @one @two")
