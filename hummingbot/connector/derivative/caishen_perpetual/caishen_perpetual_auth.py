# caishen_perpetual_auth.py
import time
from typing import Dict

import eth_account
from eth_account.messages import encode_typed_data
from eth_utils import to_bytes, to_checksum_address, to_hex

# IMPORTANT: Import deepproto.__init__ first to set up sys.path
from hummingbot.connector.derivative.caishen_perpetual.deepproto import __init__ as _deepproto_init  # noqa: F401

from hummingbot.connector.derivative.caishen_perpetual.deepproto.schema.order_pb2 import (
    CancelOrderMessage,
    PlaceOrderMessage,
)
from hummingbot.connector.derivative.caishen_perpetual.deepproto.schema.position_pb2 import (
    SetPositionLeverageMessage,
    SetPositionModeMessage,
)
from hummingbot.connector.derivative.caishen_perpetual.deepproto.schema.spot_order_pb2 import (
    SpotCancelOrderMessage,
    SpotPlaceOrderMessage,
)
from hummingbot.connector.derivative.caishen_perpetual.deepproto.schema.tx_pb2 import TxActionType, TxMessage
from hummingbot.connector.derivative.caishen_perpetual import caishen_perpetual_constants as CONSTANTS
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest


class CaishenPerpetualAuth(AuthBase):
    """
    Caishen DEX authentication — EIP-712 signed TxMessage payloads.

    Perp and spot use separate TxActionType values (e.g. PERP_PLACE_ORDER vs SPOT_PLACE_ORDER).
    """

    def __init__(self, api_key: str, api_secret: str):
        self._api_key = api_key
        self._api_secret = api_secret
        self.wallet = eth_account.Account.from_key(api_secret)

    def sign_inner(self, wallet, data):
        structured_data = encode_typed_data(full_message=data)
        signed = wallet.sign_message(structured_data)
        return signed.signature

    def sign_tx_message(self, tx_message: Dict):
        chain_id = tx_message["chain_id"]
        recent_block_hash = tx_message["recent_block_hash"]
        action = tx_message["action"]
        target_address = tx_message["target_address"]
        data = tx_message["data"]
        action_name = self.get_action_name(action)

        primary_type = f"Action:{action_name}"

        def bytes_to_hex(b: bytes) -> str:
            return to_hex(b)

        def address_to_hex(addr_bytes: bytes) -> str:
            return to_checksum_address(to_hex(addr_bytes))

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                primary_type: [
                    {"name": "chainId", "type": "uint256"},
                    {"name": "recentBlockHash", "type": "string"},
                    {"name": "targetAddress", "type": "address"},
                    {"name": "action", "type": "string"},
                    {"name": "data", "type": "bytes"},
                ],
            },
            "primaryType": primary_type,
            "domain": {
                "name": "Caishen",
                "version": "1",
                "chainId": 421614,
                "verifyingContract": "0x0000000000000000000000000000000000000000",
            },
            "message": {
                "chainId": chain_id,
                "recentBlockHash": bytes_to_hex(recent_block_hash),
                "targetAddress": address_to_hex(target_address),
                "action": action_name,
                "data": bytes_to_hex(data),
            },
        }
        return self.sign_inner(self.wallet, typed_data)

    def prepare_action_data(self, action_type: str, form_data: dict) -> bytes:
        if action_type == "PERP_PLACE_ORDER":
            return self.prepare_perp_place_order_data(form_data)
        if action_type == "SPOT_PLACE_ORDER":
            return self.prepare_spot_place_order_data(form_data)
        if action_type == "PERP_CANCEL_ORDER":
            return self.prepare_perp_cancel_order(form_data)
        if action_type == "SPOT_CANCEL_ORDER":
            return self.prepare_spot_cancel_order(form_data)
        if action_type == "PERP_CANCEL_BATCH_ORDERS":
            raise NotImplementedError("PERP_CANCEL_BATCH_ORDERS is not implemented")
        if action_type == "PERP_SET_POSITION_LEVERAGE":
            return self.prepare_set_position_leverage(form_data)
        if action_type == "PERP_SET_POSITION_MODE":
            return self.prepare_set_position_mode(form_data)
        raise ValueError(f"Unsupported action type: {action_type}")

    def get_tx_action_type(self, action_type: str) -> int:
        return TxActionType.Value(action_type)

    def get_action_name(self, action_value: int) -> str:
        try:
            return TxActionType.Name(action_value)
        except ValueError:
            return "UNKNOWN_TYPE"

    def prepare_set_position_leverage(self, form_data: dict) -> bytes:
        msg = SetPositionLeverageMessage()
        msg.base_token = form_data["base_token"]
        msg.quote_token = form_data["quote_token"]
        msg.leverage = form_data["leverage"]
        return msg.SerializeToString()

    def prepare_set_position_mode(self, form_data: dict) -> bytes:
        msg = SetPositionModeMessage()
        msg.base_token = form_data["base_token"]
        msg.quote_token = form_data["quote_token"]
        msg.mode = form_data["mode"]
        return msg.SerializeToString()

    def prepare_perp_cancel_order(self, form_data: dict) -> bytes:
        msg = CancelOrderMessage()
        msg.order_id = form_data["order_id"]
        if "client_order_id" in form_data:
            msg.client_order_id = to_bytes(hexstr=form_data["client_order_id"])
        return msg.SerializeToString()

    def prepare_spot_cancel_order(self, form_data: dict) -> bytes:
        msg = SpotCancelOrderMessage()
        msg.order_id = form_data["order_id"]
        if "client_order_id" in form_data:
            msg.client_order_id = to_bytes(hexstr=form_data["client_order_id"])
        return msg.SerializeToString()

    def prepare_perp_place_order_data(self, form_data: dict) -> bytes:
        msg = PlaceOrderMessage()
        msg.base_token = form_data["base_token"]
        msg.quote_token = form_data["quote_token"]
        msg.side = form_data["side"]
        msg.mode = form_data.get("mode", 0)
        msg.type = form_data["type"]
        msg.time_in_force = form_data["time_in_force"]
        if "price" in form_data:
            msg.price = form_data["price"]
        if "size" in form_data:
            msg.size = form_data["size"]
        if "position_id" in form_data:
            msg.position_id = form_data["position_id"]
        msg.reduce_only = form_data.get("reduce_only", False)
        if "client_order_id" in form_data:
            msg.client_order_id = to_bytes(hexstr=form_data["client_order_id"])
        if "stp_mode" in form_data:
            msg.stp_mode = form_data["stp_mode"]
        return msg.SerializeToString()

    def prepare_spot_place_order_data(self, form_data: dict) -> bytes:
        msg = SpotPlaceOrderMessage()
        msg.base_token = form_data["base_token"]
        msg.quote_token = form_data["quote_token"]
        msg.side = form_data["side"]
        msg.type = form_data["type"]
        msg.time_in_force = form_data["time_in_force"]
        if "price" in form_data:
            msg.price = form_data["price"]
        if "size" in form_data:
            msg.size = form_data["size"]
        if "quote_size" in form_data:
            msg.quote_size = form_data["quote_size"]
        if "client_order_id" in form_data:
            msg.client_order_id = to_bytes(hexstr=form_data["client_order_id"])
        if "stp_mode" in form_data:
            msg.stp_mode = form_data["stp_mode"]
        return msg.SerializeToString()

    async def make_tx(self, address, block_hash_bytes, action_type, form_data) -> bytes:
        chain_id = CONSTANTS.CHAIN_ID
        data_bytes = self.prepare_action_data(action_type, form_data)
        target_address_bytes = to_bytes(hexstr=address)
        action_value = self.get_tx_action_type(action_type)

        tx_message = {
            "chain_id": chain_id,
            "recent_block_hash": block_hash_bytes,
            "action": action_value,
            "target_address": target_address_bytes,
            "data": data_bytes,
        }
        signature = self.sign_tx_message(tx_message)

        tx_msg = TxMessage()
        tx_msg.chain_id = chain_id
        tx_msg.recent_block_hash = block_hash_bytes
        tx_msg.action = action_value
        tx_msg.target_address = target_address_bytes
        tx_msg.data = data_bytes
        tx_msg.signatures.append(signature)
        return tx_msg.SerializeToString()

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        return request

    @staticmethod
    def _get_timestamp() -> float:
        return time.time()
