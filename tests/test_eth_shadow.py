from __future__ import annotations

import pytest

from polymarket_bot.eth_shadow import BOT_ID, _parser


def test_eth_shadow_cli_has_no_execution_or_credential_surface(tmp_path) -> None:
    parser = _parser()
    arguments = parser.parse_args(["--audit", str(tmp_path / "eth.jsonl")])
    option_strings = {
        option for action in parser._actions for option in action.option_strings
    }

    assert BOT_ID == "eth-5m-chainlink-v3-shadow"
    assert arguments.quantity == 5
    assert "--submit" not in option_strings
    assert "--wallet" not in option_strings
    assert "--authorization-id" not in option_strings
    assert "--relayer-api-key-address" not in option_strings
    with pytest.raises(SystemExit):
        parser.parse_args(["--audit", str(tmp_path / "eth.jsonl"), "--submit"])
