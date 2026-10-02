"""Contract tests for the live trading path.

`execution/live.py` is the only file that can spend money, and it talks to an
SDK we do not control. These tests assert that every symbol and signature it
depends on still exists, WITHOUT connecting to anything or signing anything
that leaves the machine.

They exist because the failure they catch is the worst kind: an SDK upgrade
renames an enum, nothing breaks until the first real order, and you discover it
at the exact moment you cared about latency.

Skipped automatically when py-clob-client is not installed, so paper-only
setups still get a green suite.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("py_clob_client", reason="live extras not installed")

from py_clob_client.client import ClobClient  # noqa: E402
from py_clob_client.clob_types import (  # noqa: E402
    ApiCreds, AssetType, BalanceAllowanceParams, OrderArgs, OrderType, TradeParams,
)
from py_clob_client.order_builder.constants import BUY, SELL  # noqa: E402

from polylag.execution.live import LiveBroker  # noqa: E402

# A throwaway key used only to construct local objects. It holds nothing, is
# never funded, and nothing signed with it is ever transmitted.
BURNER_KEY = "0x" + "11" * 32
BURNER_ADDRESS = "0x000000000000000000000000000000000000dEaD"


def test_order_types_we_configure_all_exist():
    """config.yaml validates against these three names."""
    for name in ("FAK", "FOK", "GTC"):
        assert hasattr(OrderType, name), f"OrderType.{name} disappeared"


def test_side_constants_match_our_string_literals():
    """live.py maps its internal "BUY"/"SELL" onto these."""
    assert BUY == "BUY"
    assert SELL == "SELL"


def test_order_args_accepts_the_fields_we_send():
    args = OrderArgs(price=0.5, size=10, side=BUY, token_id="123")
    assert args.price == 0.5 and args.size == 10


def test_balance_and_trade_params_shapes():
    BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    TradeParams(maker_address=BURNER_ADDRESS)
    ApiCreds(api_key="k", api_secret="s", api_passphrase="p")


def test_client_exposes_every_method_live_py_calls():
    for method in (
        "create_order", "post_order", "create_or_derive_api_creds",
        "set_api_creds", "get_balance_allowance", "get_trades",
        "cancel_all", "get_address",
    ):
        assert hasattr(ClobClient, method), f"ClobClient.{method} disappeared"


def test_post_order_still_takes_an_order_type():
    params = inspect.signature(ClobClient.post_order).parameters
    assert "orderType" in params, "post_order no longer accepts an order type"


def test_constructor_accepts_our_keyword_arguments():
    """Builds the client locally -- no network call, no funds, nothing sent."""
    client = ClobClient(
        "https://clob.polymarket.com",
        key=BURNER_KEY,
        chain_id=137,
        signature_type=1,
        funder=BURNER_ADDRESS,
    )
    assert client is not None


def test_live_broker_refuses_to_trade_before_connecting(cfg):
    """The guard that stops a half-initialised broker from sending orders."""
    import asyncio

    from polylag.models import MarketRef, OrderIntent

    broker = LiveBroker(cfg)
    market = MarketRef("c", "q", "s", "YES_T", "NO_T")
    intent = OrderIntent(market, "YES_T", "BUY", 10, 0.5, "test")
    result = asyncio.run(broker.buy(intent))
    assert result.status == "rejected"
    assert "not connected" in result.message


def test_live_mode_requires_credentials(cfg):
    """Missing keys must fail loudly at startup, not at the first order."""
    from dataclasses import replace

    from polylag.config import ConfigError

    naked = replace(cfg, private_key=None, funder_address=None)
    with pytest.raises(ConfigError, match="POLYMARKET_PRIVATE_KEY"):
        naked.require_live_credentials()
