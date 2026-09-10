import base64
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse
import uuid

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import httpx


API_BASE = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
MAX_MEDIA_BYTES = 50 * 1024 * 1024
MEDIA_FIELDS = {2: ("image", "image_item", ".jpg"), 3: ("voice", "voice_item", ".silk"),
                4: ("file", "file_item", ".bin"), 5: ("video", "video_item", ".mp4")}


class WeixinError(RuntimeError):
    pass


def official_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if (parsed.scheme != "https" or not hostname.endswith(".weixin.qq.com")
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise WeixinError("Unexpected Weixin endpoint")
    return url


def save_private_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def decode_media_key(value: str) -> bytes:
    try:
        decoded = bytes.fromhex(value)
        if len(decoded) == 16:
            return decoded
    except ValueError:
        pass
    try:
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) == 32:
            decoded = bytes.fromhex(decoded.decode("ascii"))
        if len(decoded) == 16:
            return decoded
    except (ValueError, UnicodeError):
        pass
    raise WeixinError("Invalid media key format")


def encrypt_media(content: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(content) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return cipher.update(padded) + cipher.finalize()


def decrypt_media(content: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = cipher.update(content) + cipher.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def message_text(message: dict) -> str:
    return "\n".join(
        item.get("text_item", {}).get("text", "")
        for item in message.get("item_list", []) if item.get("type") == 1
    ).strip()


def media_items(message: dict) -> list[dict]:
    items = []
    for item in message.get("item_list", []):
        if item.get("type") in MEDIA_FIELDS:
            items.append(item)
        reference = item.get("ref_msg", {}).get("message_item", {})
        if reference.get("type") in MEDIA_FIELDS:
            items.append(reference)
    reference = message.get("ref_msg", {}).get("message_item", {})
    if reference.get("type") in MEDIA_FIELDS:
        items.append(reference)
    return items


class WeixinClient:
    def __init__(self, token: str = "", base_url: str = API_BASE, transport=None):
        self.token = token
        self.base_url = official_url(base_url).rstrip("/")
        self.http = httpx.AsyncClient(timeout=40, follow_redirects=False, transport=transport)

    async def close(self):
        await self.http.aclose()

    async def post(self, endpoint: str, body: dict, timeout: float = 40) -> dict:
        headers = {
            "AuthorizationType": "ilink_bot_token",
            "iLink-App-Id": "bot", "iLink-App-ClientVersion": "131072",
            "X-WECHAT-UIN": base64.b64encode(str(int.from_bytes(os.urandom(4))).encode()).decode(),
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        response = await self.http.post(
            self.base_url + "/ilink/bot/" + endpoint,
            json={**body, "base_info": {"channel_version": "2.0.0"}},
            headers=headers, timeout=timeout,
        )
        response.raise_for_status()
        result = response.json()
        for field in ("ret", "errcode"):
            if result.get(field, 0) != 0:
                raise WeixinError(f"Weixin business error: {result[field]}")
        return result

    async def qr_code(self) -> dict:
        response = await self.http.get(self.base_url + "/ilink/bot/get_bot_qrcode",
                                       params={"bot_type": "3"}, timeout=20)
        response.raise_for_status()
        result = response.json()
        if not result.get("qrcode") or not result.get("qrcode_img_content"):
            raise WeixinError("Missing login QR data")
        return result

    async def confirm_qr(self, code: str) -> dict:
        response = await self.http.get(
            self.base_url + "/ilink/bot/get_qrcode_status", params={"qrcode": code},
            headers={"iLink-App-Id": "bot", "iLink-App-ClientVersion": "131072"},
        )
        response.raise_for_status()
        result = response.json()
        if result.get("status") == "confirmed":
            official_url(result.get("baseurl", API_BASE))
            if not all(result.get(key) for key in ("bot_token", "ilink_bot_id", "ilink_user_id")):
                raise WeixinError("Login response is incomplete")
        return result

    async def updates(self, cursor: str) -> dict:
        try:
            return await self.post("getupdates", {"get_updates_buf": cursor})
        except httpx.ReadTimeout:
            return {"msgs": []}

    async def send_items(self, recipient: str, context: str, items: list, delivery_id: str):
        if not context:
            raise WeixinError("No reply context available")
        await self.post("sendmessage", {"msg": {
            "from_user_id": "", "to_user_id": recipient, "client_id": delivery_id,
            "message_type": 2, "message_state": 2, "item_list": items,
            "context_token": context,
        }})

    async def download(self, item: dict) -> bytes:
        _, field, _ = MEDIA_FIELDS[item["type"]]
        info = item[field]
        media = info.get("media", {})
        query = media.get("encrypt_query_param")
        key = decode_media_key(info.get("aeskey") or media.get("aes_key", ""))
        if not query:
            raise WeixinError("Missing media download reference")
        content = bytearray()
        async with self.http.stream("GET", CDN_BASE + "/download",
                                    params={"encrypted_query_param": query}, timeout=120) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_MEDIA_BYTES + 16:
                    raise WeixinError("Attachment exceeds configured size limit")
        result = decrypt_media(bytes(content), key)
        if len(result) > MAX_MEDIA_BYTES:
            raise WeixinError("Attachment exceeds configured size limit")
        return result

    async def upload(self, path: Path, recipient: str) -> dict:
        if path.stat().st_size > MAX_MEDIA_BYTES:
            raise WeixinError("Attachment exceeds configured size limit")
        content = path.read_bytes()
        key = os.urandom(16)
        encrypted = encrypt_media(content, key)
        file_id = uuid.uuid4().hex
        response = await self.post("getuploadurl", {
            "filekey": file_id, "media_type": 3, "to_user_id": recipient,
            "rawsize": len(content), "rawfilemd5": hashlib.md5(content).hexdigest(),
            "filesize": len(encrypted), "aeskey": key.hex(), "no_need_thumb": True,
        })
        url = response.get("upload_full_url")
        parameters = None
        if not url:
            if not response.get("upload_param"):
                raise WeixinError("Missing upload reference")
            url = CDN_BASE + "/upload"
            parameters = {"encrypted_query_param": response["upload_param"], "filekey": file_id}
        result = await self.http.post(official_url(url), params=parameters, content=encrypted,
                                      headers={"Content-Type": "application/octet-stream"}, timeout=120)
        result.raise_for_status()
        reference = result.headers.get("x-encrypted-param")
        if not reference:
            raise WeixinError("Missing uploaded media reference")
        return {"type": 4, "file_item": {
            "file_name": path.name, "len": str(len(content)),
            "media": {"encrypt_query_param": reference,
                      "aes_key": base64.b64encode(key.hex().encode()).decode(), "encrypt_type": 1},
        }}


async def create_qr(directory: Path, *, terminal: bool = False) -> dict:
    import qrcode

    client = WeixinClient()
    try:
        response = await client.qr_code()
        directory.mkdir(parents=True, exist_ok=True)
        save_private_json(directory / "pending-login.json", {"qrcode": response["qrcode"]})
        image_path = directory / "login.png"
        code = qrcode.QRCode()
        code.add_data(response["qrcode_img_content"])
        code.make(fit=True)
        code.make_image().save(image_path)
        if terminal:
            code.print_ascii(invert=True)
        return {"ok": True, "qr_image": str(image_path.resolve()), "status": "scan_required"}
    finally:
        await client.close()


async def finish_login(directory: Path) -> dict:
    client = WeixinClient()
    try:
        pending = json.loads((directory / "pending-login.json").read_text(encoding="utf-8"))
        response = await client.confirm_qr(pending["qrcode"])
        if response.get("status") != "confirmed":
            return {"ok": False, "status": response.get("status", "unknown")}
        save_private_json(directory / "credentials.json", {
            "token": response["bot_token"], "base_url": response.get("baseurl", API_BASE),
            "bot_id": response["ilink_bot_id"], "user_id": response["ilink_user_id"],
        })
        (directory / "pending-login.json").unlink(missing_ok=True)
        (directory / "login.png").unlink(missing_ok=True)
        return {"ok": True, "status": "logged_in"}
    finally:
        await client.close()