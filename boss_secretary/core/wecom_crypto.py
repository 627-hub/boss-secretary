"""企业微信回调加解密（官方协议实现，无官方 SDK 依赖）。

协议：
  EncodingAESKey(43位 Base64) + "=" → AESKey(32字节)
  密文 = Base64(AES-CBC(随机16B + msg_len(4B网络序) + 明文XML + receiveid))
  签名 = SHA1(sort(token, timestamp, nonce, 密文))
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from typing import Tuple

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes


class WeComCryptoError(Exception):
    pass


def derive_key(encoding_aes_key: str) -> bytes:
    try:
        return base64.b64decode(encoding_aes_key + "=")
    except Exception as e:
        raise WeComCryptoError(f"EncodingAESKey 非法（应为 43 位 Base64）: {e}") from e


def signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypt])).encode()).hexdigest()


def encrypt_msg(plain_xml: str, aes_key: bytes, receive_id: str
                ) -> str:
    import os
    raw = (get_random_bytes(16)
           + struct.pack(">I", len(plain_xml.encode()))
           + plain_xml.encode()
           + receive_id.encode())
    pad = 32 - len(raw) % 32
    raw += bytes([pad]) * pad
    cipher = AES.new(aes_key, AES.MODE_CBC, iv=aes_key[:16])
    return base64.b64encode(cipher.encrypt(raw)).decode()


def decrypt_msg(encrypt_b64: str, aes_key: bytes, receive_id: str) -> str:
    try:
        cipher = AES.new(aes_key, AES.MODE_CBC, iv=aes_key[:16])
        raw = cipher.decrypt(base64.b64decode(encrypt_b64))
    except Exception as e:
        raise WeComCryptoError(f"AES 解密失败: {e}") from e
    if not raw:
        raise WeComCryptoError("解密结果为空")
    pad = raw[-1]
    if not 1 <= pad <= 32 or pad > len(raw):
        raise WeComCryptoError(f"PKCS#7 填充非法: {pad}")
    raw = raw[:-pad]
    if len(raw) < 20:
        raise WeComCryptoError("解密结果长度不足")
    msg_len = struct.unpack(">I", raw[16:20])[0]
    if 20 + msg_len > len(raw):
        raise WeComCryptoError(f"msg_len 越界: {msg_len} > {len(raw) - 20}")
    try:
        msg = raw[20:20 + msg_len].decode()
        recv = raw[20 + msg_len:].decode()
    except UnicodeDecodeError as e:
        raise WeComCryptoError(f"解密内容编码非法: {e}") from e
    if recv != receive_id:
        raise WeComCryptoError(f"receiveid 校验失败: {recv} != {receive_id}")
    return msg


def verify_url(token: str, aes_key: str, receive_id: str, msg_signature: str,
               timestamp: str, nonce: str, echostr: str) -> str:
    """GET 回调验证：签名校验 + 解密，返回明文 echostr 给企微。"""
    if not hmac.compare_digest(signature(token, timestamp, nonce, echostr), msg_signature):
        raise WeComCryptoError("URL 验证签名不匹配")
    return decrypt_msg(echostr, derive_key(aes_key), receive_id)


def parse_xml(xml: str) -> dict:
    import xml.etree.ElementTree as ET
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise WeComCryptoError("XML 含非法 DTD/ENTITY 声明")
    root = ET.fromstring(xml)
    return {child.tag: (child.text or "") for child in root}


def build_encrypted_xml(plain_xml: str, aes_key: bytes, receive_id: str,
                        token: str, nonce: str, timestamp: str | None = None) -> str:
    ts = timestamp or str(int(time.time()))
    enc = encrypt_msg(plain_xml, aes_key, receive_id)
    sig = signature(token, ts, nonce, enc)
    return (f"<xml><ToUserName><![CDATA[{receive_id}]]></ToUserName>"
            f"<FromUserName><![CDATA[{receive_id}]]></FromUserName>"
            f"<CreateTime>{ts}</CreateTime>"
            f"<MsgType><![CDATA[text]]></MsgType>"
            f"<Content><![CDATA[{enc}]]></Content>"
            f"<MsgSignature><![CDATA[{sig}]]></MsgSignature>"
            f"</xml>")
