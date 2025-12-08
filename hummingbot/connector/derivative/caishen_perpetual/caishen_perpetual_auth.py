# caishen_perpetual_auth.py
import base64
import json
import time
from typing import Any, Dict, Tuple

import eth_account
from eth_account.messages import encode_typed_data
from eth_utils import to_hex, to_checksum_address, to_bytes

# IMPORTANT: Import deepproto.__init__ first to set up sys.path
# This must be done before importing any proto files
from hummingbot.connector.derivative.caishen_perpetual.deepproto import __init__ as _deepproto_init

# Now we can safely import proto files
from hummingbot.connector.derivative.caishen_perpetual.deepproto.api.position_pb2 import SetPositionLeverageMessage,SetPositionModeMessage
from hummingbot.connector.derivative.caishen_perpetual.deepproto.api.order_pb2 import PlaceOrderMessage, CancelOrderMessage
from hummingbot.connector.derivative.caishen_perpetual.deepproto.api import tx_pb2
from hummingbot.connector.derivative.caishen_perpetual.deepproto.schema.tx_pb2 import TxMessage


from hummingbot.connector.derivative.caishen_perpetual import caishen_perpetual_constants as CONSTANTS
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest


# EIP-712 Domain 常量（根据你的 DEX 配置调整）
DOMAIN_NAME = "Caishen"  # 对应 Go 代码中的 DomainName
DOMAIN_VERSION = "1"        # 对应 Go 代码中的 Version



class CaishenPerpetualAuth(AuthBase):
    """
    Caishen DEX 认证类 - 实现 EIP-712 签名
    
    基于 TxMessage 结构实现签名：
    - chain_id: uint64
    - recent_block_hash: bytes
    - action: TxActionType
    - target_address: bytes (20 bytes)
    - data: bytes
    - signatures: [][]byte
    """
    
    def __init__(self, api_key: str, api_secret: str):
        self._api_key = api_key  # 钱包地址
        self._api_secret = api_secret  # 私钥
        self.wallet = eth_account.Account.from_key(api_secret)

    def sign_inner(self, wallet, data):
        structured_data = encode_typed_data(full_message=data)
        signed = wallet.sign_message(structured_data)
        return signed.signature
    
    def sign_tx_message(self,tx_message):
        chain_id = tx_message["chain_id"]
        recent_block_hash = tx_message["recent_block_hash"]
        action = tx_message["action"]
        target_address = tx_message["target_address"]
        data = tx_message["data"]
        # 获取 action 的字符串名称
        action_name = self.get_action_name(action)

        # 构建 EIP-712 typed data
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
                "chainId": 42161,  # 固定为 Arbitrum One
                "verifyingContract": "0x0000000000000000000000000000000000000000",
            },
            "message": {
                "chainId": chain_id,  # 交易消息的 chainId
                "recentBlockHash": bytes_to_hex(recent_block_hash),
                "targetAddress": address_to_hex(target_address),
                "action": action_name,
                "data": bytes_to_hex(data),
            },
        }
        return self.sign_inner(self.wallet, typed_data)

    def prepare_action_data(self,action_type: str, form_data: dict) -> bytes:
        """根据 action 类型准备数据"""
        if action_type == "PLACE_ORDER":
            return self.prepare_place_order_data(form_data)
        elif action_type == 'CANCEL_ORDER':
            return self.prepare_cancel_order(form_data)
        elif action_type =="CANCEL_BATCH_ORDERS":
            return self.prepare_cancel_batch_orders(form_data)
        elif action_type =="SET_POSITION_LEVERAGE":
            return self.prepare_set_position_leverage(form_data)
        elif action_type == "SET_POSITION_MODE":
            return self.prepare_set_position_mode(form_data)
        else:
            raise ValueError(f"Unsupported action type: {action_type}")

    # 准备设置杠杆数据
    def prepare_set_position_leverage(self,form_data: dict) -> bytes:
        msg = SetPositionLeverageMessage()
        msg.base_token = form_data["base_token"]
        msg.quote_token = form_data["quote_token"]
        msg.leverage = form_data["leverage"]
        return msg.SerializeToString()
    
    # 准备设置模式数据
    def prepare_set_position_mode(self,form_data: dict) -> bytes:
        msg = SetPositionModeMessage()
        msg.base_token = form_data["base_token"]
        msg.quote_token = form_data["quote_token"]
        msg.mode = form_data["mode"]
        return msg.SerializeToString()
    
    # 准备取消订单数据
    def prepare_cancel_order(self,form_data: dict) -> bytes:
        msg = CancelOrderMessage()
        msg.order_id = form_data["order_id"]
        if "client_order_id" in form_data:
            msg.client_order_id = to_bytes(hexstr=form_data["client_order_id"])
        return msg.SerializeToString()

    # 准备 PlaceOrder 消息数据
    def prepare_place_order_data(self,form_data: dict) -> bytes:
        """准备 PLACE_ORDER 消息数据"""
        # 使用生成的 protobuf 模块

        msg = PlaceOrderMessage()
        msg.base_token = form_data["base_token"]
        msg.quote_token = form_data["quote_token"]
        msg.side = form_data["side"]  # 0 = LONG, 1 = SHORT
        msg.type = form_data["type"]  # 0 = LIMIT, 1 = MARKET, etc.
        msg.time_in_force = form_data["time_in_force"]  # 1 = GTC, etc.
        if "price" in form_data:
            msg.price = form_data["price"]
        if "size" in form_data:
            msg.size = form_data["size"]
        if 'position_id' in form_data:
            msg.position_id = form_data["position_id"]
        msg.reduce_only = form_data.get("reduce_only", False)
        if "client_order_id" in form_data:
            msg.client_order_id = to_bytes(hexstr=form_data["client_order_id"])

        if "stp_mode" in form_data:
            msg.stp_mode = form_data["stp_mode"]

        return msg.SerializeToString()

    async def make_and_submit_tx(self,address,private_key,action_type,form_data):
        block_hash_bytes = await self.get_latest()
        chain_id = CONSTANTS.CHAIN_ID
        data_bytes = self.prepare_action_data(action_type, form_data)

        # 4. 创建 TxMessage
        target_address_bytes = to_bytes(hexstr=address)
        action_value = self.get_tx_action_type(action_type)

        # 创建 TxMessage 字典（用于签名）
        tx_message = {
            "chain_id": chain_id,
            "recent_block_hash": block_hash_bytes,
            "action": action_value,
            "target_address": target_address_bytes,
            "data": data_bytes,
        }
        # 5. 签名
        signature = self.sign_tx_message(private_key, tx_message)

        # 6. 创建完整的 TxMessage protobuf 对象并编码
        tx_msg = TxMessage()
        tx_msg.chain_id = chain_id
        tx_msg.recent_block_hash = block_hash_bytes
        tx_msg.action = action_value
        tx_msg.target_address = target_address_bytes
        tx_msg.data = data_bytes
        tx_msg.signatures.append(signature)
        encoded_message = tx_msg.SerializeToString()

        # 7. 提交交易
        response = self.submit_tx(encoded_message)
        return response

    async def submit_tx(self,message_bytes,print_flag=True):
        # 将 bytes 转换为 base64
        message_base64 = base64.b64encode(message_bytes).decode("utf-8")
        payload={
            "message":message_base64
        }

        result = await self._api_post(
            path_url = CONSTANTS.SUBMIT_TX_URL,
            data=payload,
            is_auth_required=False)
            
        print(result.url)
        if print_flag:
            print(result.text)
        return result.text

    async def get_latest(self,print_flag=True):
        '''
        获取最新的区块
        :param print_flag:
        :return:
        '''
        result = await self._api_get(
            path_url = CONSTANTS.GET_LATEST_BLOCK_URL,
            is_auth_required=False)
        print(result.url)
        if print_flag:
            print(result.text)
        data_json = json.loads(result.text)
        block_hash = data_json['data']['block']['hash']
        block_hash_bytes = to_bytes(hexstr=block_hash)
        return block_hash_bytes

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        return request# pass-through

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        return request  # pass-through

    @staticmethod
    def _get_timestamp() -> float:
        return time.time()

