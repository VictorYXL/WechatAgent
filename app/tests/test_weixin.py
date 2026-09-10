import asyncio
import base64

import httpx
import pytest

from wechat_agent.weixin import (
    WeixinClient, WeixinError, decode_media_key, decrypt_media, encrypt_media,
    media_items, message_text, official_url,
)


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 1000])
def test_protocol_aes_roundtrip_and_key_formats(size):
    key = bytes(range(16))
    content = b"x" * size
    assert decrypt_media(encrypt_media(content, key), key) == content
    for encoded in (key.hex(), base64.b64encode(key).decode(),
                    base64.b64encode(key.hex().encode()).decode()):
        assert decode_media_key(encoded) == key


@pytest.mark.parametrize("url", ["http://ilinkai.weixin.qq.com", "https://weixin.qq.com.evil.test",
                                 "https://evil.test", "https://user:pass@ilinkai.weixin.qq.com"])
def test_tokens_never_follow_untrusted_hosts(url):
    with pytest.raises(WeixinError):
        official_url(url)


def test_text_trigger_does_not_include_voice_transcription():
    message = {"item_list": [{"type": 3, "voice_item": {"text": "do work"}},
                              {"type": 2, "image_item": {}}]}
    assert message_text(message) == ""
    assert len(media_items(message)) == 2


def test_business_error_is_not_delivery_success():
    async def scenario():
        client = WeixinClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ret": -14})
        ))
        try:
            with pytest.raises(WeixinError):
                await client.send_items("user", "context", [], "delivery")
        finally:
            await client.close()
    asyncio.run(scenario())


def test_send_timeout_is_not_swallowed():
    def timeout(request):
        raise httpx.ReadTimeout("synthetic")

    async def scenario():
        client = WeixinClient(transport=httpx.MockTransport(timeout))
        try:
            with pytest.raises(httpx.ReadTimeout):
                await client.send_items("user", "context", [], "delivery")
            assert await client.updates("") == {"msgs": []}
        finally:
            await client.close()
    asyncio.run(scenario())