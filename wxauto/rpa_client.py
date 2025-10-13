import json
import threading
import time
import uuid
import tempfile
import base64
import os
import hashlib
import mimetypes
from typing import Optional, Dict, Any, List

import requests
from websocket import WebSocketApp

from .logger import wxlog
from .wx import WeChat
from .param import WxParam


# 允许的图片 MIME 列表与大小限制（与协议 5 章一致）
ALLOWED_IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024


class RpaInvalidParams(Exception):
    """参数不合法异常，用于返回 INVALID_PARAMS/rejected。"""
    def __init__(self, message: str):
        super().__init__(message)
        self.code = "INVALID_PARAMS"


class RpaMediaError(Exception):
    """媒体处理异常，用于返回 MEDIA_FETCH_FAIL/failed。"""
    def __init__(self, message: str):
        super().__init__(message)
        self.code = "MEDIA_FETCH_FAIL"


class RpaUnsupportedMime(Exception):
    """不支持的 MIME 类型异常，用于返回 UNSUPPORTED_MIME/rejected。"""
    def __init__(self, message: str):
        super().__init__(message)
        self.code = "UNSUPPORTED_MIME"


class RpaWsClient:
    """WebSocket 客户端，负责与服务端长连接、自动重连、心跳与消息收发。

    - 连接端点示例：ws://host:8080/ws/rpa?deviceId=xxx
    - 鉴权：可选 Bearer Token，通过 headers 传入。
    - 自动重连：指数退避，最大 30s。
    - 心跳：底层使用 websocket-client 的 ping_interval。
    """

    def __init__(
        self,
        url: str,
        device_id: str,
        token: Optional[str] = None,
        on_message=None,
        on_open=None,
        on_close=None,
        on_error=None,
        ping_interval: int = 20,
        ping_timeout: int = 10,
    ) -> None:
        self.url = url
        self.device_id = device_id
        self.token = token
        self.on_message_cb = on_message
        self.on_open_cb = on_open
        self.on_close_cb = on_close
        self.on_error_cb = on_error
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self._ws: Optional[WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = False

    def _headers(self) -> Dict[str, str]:
        """构造 WebSocket 连接使用的请求头。

        - 携带设备标识 `X-Device-Id`
        - 可选鉴权头 `Authorization: Bearer <token>`
        """
        headers = {
            "X-Device-Id": self.device_id,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _on_open(self, ws: WebSocketApp):
        """WebSocket 打开回调：标记已连接并触发上层 on_open。"""
        self._connected = True
        wxlog.debug("WS 已连接")
        if self.on_open_cb:
            try:
                self.on_open_cb()
            except Exception as e:
                wxlog.debug(f"on_open 回调异常: {e}")

    def _on_message(self, ws: WebSocketApp, message: str):
        """WebSocket 消息回调：将原始文本消息交给上层处理。"""
        if self.on_message_cb:
            try:
                self.on_message_cb(message)
            except Exception as e:
                wxlog.debug(f"on_message 回调异常: {e}")

    def _on_close(self, ws: WebSocketApp, status_code, msg):
        """WebSocket 关闭回调：标记断开并触发上层 on_close。"""
        self._connected = False
        wxlog.debug(f"WS 连接关闭: {status_code} {msg}")
        if self.on_close_cb:
            try:
                self.on_close_cb(status_code, msg)
            except Exception as e:
                wxlog.debug(f"on_close 回调异常: {e}")

    def _on_error(self, ws: WebSocketApp, error):
        """WebSocket 错误回调：标记断开并触发上层 on_error。"""
        self._connected = False
        wxlog.debug(f"WS 连接错误: {error}")
        if self.on_error_cb:
            try:
                self.on_error_cb(error)
            except Exception as e:
                wxlog.debug(f"on_error 回调异常: {e}")

    def start(self):
        """启动 WebSocket 长连接线程。"""
        self._stop_event.clear()
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._ws_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._ws_thread.start()

    def stop(self):
        """停止 WebSocket 连接与线程。"""
        self._stop_event.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._ws_thread:
            self._ws_thread.join(timeout=3)

    def send_json(self, data: Dict[str, Any]):
        """发送 JSON 文本帧。"""
        try:
            if self._ws and self._connected:
                self._ws.send(json.dumps(data, ensure_ascii=False))
        except Exception as e:
            wxlog.debug(f"WS 发送失败: {e}")

    def _run_loop(self):
        """运行 WS 循环：建立连接、维持心跳、断线指数退避重连。"""
        backoff = 1
        while not self._stop_event.is_set():
            try:
                self._ws = WebSocketApp(
                    self.url,
                    header=[f"{k}: {v}" for k, v in self._headers().items()],
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                self._ws.run_forever(
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                )
            except Exception as e:
                wxlog.debug(f"WS 运行异常: {e}")

            if self._stop_event.is_set():
                break

            # 指数退避重连
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


class WeChatRpaAgent:
    """RPA 代理，负责：
    - 轮询微信新消息并按协议上报 event
    - 接收服务端 command 并在本地执行（发文本/图片/文件）
    - 上报执行结果 command_result

    使用方式：
    - 传入已初始化的 `WeChat` 实例与 WS 基址/设备ID 启动。
    """

    def __init__(
        self,
        wx: WeChat,
        server_ws_base: str,
        device_id: str,
        token: Optional[str] = None,
    ) -> None:
        """构造 RPA 代理。

        参数：
        - wx: 已初始化的 `WeChat` 主窗口实例
        - server_ws_base: WS 基础地址（不含 deviceId 查询参数）
        - device_id: 设备标识
        - token: 可选 Bearer Token
        """
        self.wx = wx
        # 拼装协议端点 /ws/rpa?deviceId=xxx
        sep = '&' if '?' in server_ws_base else '?'
        self.url = f"{server_ws_base}{sep}deviceId={device_id}"
        self.device_id = device_id
        self.token = token

        self.ws = RpaWsClient(
            url=self.url,
            device_id=device_id,
            token=token,
            on_message=self._on_ws_message,
            on_open=self._on_ws_open,
        )

        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ========== WS 回调与 Envelope 构建 ==========

    def _envelope(self, typ: str, payload: Dict[str, Any], trace_id: str = None) -> Dict[str, Any]:
        """构造协议顶层 Envelope，支持透传 traceId。"""
        return {
            "type": typ,
            "traceId": (trace_id or str(uuid.uuid4())),
            "deviceId": self.device_id,
            "timestamp": int(time.time() * 1000),
            "payload": payload,
        }

    def _send_ack(self, for_type: str, for_id: str, trace_id: str = None):
        """上报 ack，表明已收到对应消息（通常为 command）。"""
        self.ws.send_json(self._envelope("ack", {"forType": for_type, "forId": for_id}, trace_id))

    def _send_error(self, for_type: str, for_id: str, code: str, message: str):
        """上报错误消息（解析/早期校验失败）。"""
        self.ws.send_json(self._envelope("error", {"forType": for_type, "forId": for_id, "code": code, "message": message}))

    def _send_status(self, running: bool, version: str = None, last_error: str = None):
        """上报运行状态，包含运行标记、版本与最近错误。"""
        self.ws.send_json(self._envelope("status", {"running": running, "version": version or "v1", "lastError": last_error}))

    def _send_event_wechat_message(self, msg_obj):
        """将本地解析的微信消息包装成 event 并上报。"""
        chat = msg_obj.parent.get_info()
        data = {
            "messageId": msg_obj.id,
            "from": getattr(msg_obj, "sender", msg_obj.attr),
            "chatId": chat.get("chat_name"),  # 以显示名作为 chatId（MVP）
            "msgType": msg_obj.type,
            "content": msg_obj.content,
            "raw": msg_obj.info,
        }
        payload = {"eventType": "wechat_message", "data": data}
        self.ws.send_json(self._envelope("event", payload))

    def _send_command_result(self, command_id: str, status: str, result: Dict[str, Any] = None, error: Dict[str, Any] = None, trace_id: str = None):
        """上报指令执行结果，包括 success/failed/timeout/rejected。"""
        payload = {
            "commandId": command_id,
            "status": status,
            "result": result or {},
            "error": error,
        }
        self.ws.send_json(self._envelope("command_result", payload, trace_id))

    def _on_ws_open(self):
        """WS 打开时立即上报运行状态。"""
        wxlog.debug("WS 打开，发送运行状态")
        self._send_status(True)

    def _on_ws_message(self, message: str):
        """处理服务端下发的 command，并在本地执行。"""
        try:
            data = json.loads(message)
        except Exception:
            wxlog.debug("WS 收到非 JSON，忽略")
            return

        if data.get("type") != "command":
            return

        payload = data.get("payload") or {}
        trace_id = data.get("traceId")
        command_id = payload.get("commandId") or ""
        action = payload.get("action")
        params = payload.get("params") or {}

        # 即刻 ack
        if command_id:
            self._send_ack("command", command_id, trace_id)

        try:
            if action == "send_text":
                result = self._handle_send_text(params)
            elif action == "send_image":
                result = self._handle_send_media(params, kind="image")
            elif action == "send_file":
                result = self._handle_send_media(params, kind="file")
            else:
                raise RpaInvalidParams("不支持的动作")
            self._send_command_result(command_id, "success", result={"messageId": result or None}, trace_id=trace_id)
        except RpaInvalidParams as e:
            self._send_command_result(command_id, "rejected", error={"code": e.code, "message": str(e)}, trace_id=trace_id)
        except RpaUnsupportedMime as e:
            self._send_command_result(command_id, "rejected", error={"code": e.code, "message": str(e)}, trace_id=trace_id)
        except RpaMediaError as e:
            self._send_command_result(command_id, "failed", error={"code": e.code, "message": str(e)}, trace_id=trace_id)
        except Exception as e:
            wxlog.debug(f"执行指令异常: {e}")
            self._send_command_result(command_id, "failed", error={"code": "RPA_EXEC_ERROR", "message": str(e)}, trace_id=trace_id)

    # ========== 指令处理 ==========

    def _switch_to(self, to: str, exact: bool = False):
        """切换聊天窗口，失败抛出异常。"""
        resp = self.wx.ChatWith(to, exact=exact)
        if not resp:
            raise RuntimeError(f"未找到会话: {to}")

    def _handle_send_text(self, params: Dict[str, Any]) -> Optional[str]:
        """处理发送文本指令。
        params: { to, text, atList?, replyTo? }
        返回 messageId（若可获取）。
        """
        to = params.get("to")
        text = params.get("text")
        at_list = params.get("atList")
        reply_to = params.get("replyTo")
        if not to or not text:
            raise RpaInvalidParams("缺少必要参数 to/text")

        self._switch_to(to)

        if reply_to:
            # 尝试引用或回复指定消息（MVP：当前聊天可见范围内）
            msg = self.wx.GetMessageById(reply_to)
            if msg and hasattr(msg, "reply"):
                r = msg.reply(text=text, at=at_list)
                if not r:
                    raise RuntimeError(r["message"])  # WxResponse
                return None

        r = self.wx.SendMsg(text, at=at_list)
        if not r:
            raise RuntimeError(r["message"])  # WxResponse
        return None

    def _handle_send_media(self, params: Dict[str, Any], kind: str) -> Optional[str]:
        """处理发送图片/文件，包含 MIME 白名单、大小与校验和校验。
        params: { to, media:{ source:url|base64, url, headers, base64, filename, mimeType, sizeBytes, checksum }, caption? }
        """
        to = params.get("to")
        media = params.get("media") or {}
        if not to or not media:
            raise RpaInvalidParams("缺少必要参数 to/media")

        source = media.get("source")
        filename = media.get("filename") or ("pic.jpg" if kind == "image" else "file.bin")

        # 拉取/解码媒体字节
        content_bytes = b""
        detected_mime = None
        if source == "url":
            url = media.get("url")
            headers = media.get("headers") or {}
            if not url:
                raise RpaInvalidParams("media.url 为空")
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            content_bytes = resp.content
            ct = resp.headers.get("Content-Type", "").split(";")[0].strip()
            detected_mime = ct or None
        elif source == "base64":
            b64 = media.get("base64")
            if not b64:
                raise RpaInvalidParams("media.base64 为空")
            data_mime = None
            if b64.startswith("data:"):
                # data:image/png;base64,xxxx
                try:
                    header, payload = b64.split(",", 1)
                    data_mime = header.split(":", 1)[1].split(";", 1)[0]
                    b64 = payload
                except Exception:
                    pass
            content_bytes = base64.b64decode(b64)
            detected_mime = data_mime
        else:
            raise RpaInvalidParams("不支持的 media.source")

        # MIME 判定与白名单
        mime = media.get("mimeType") or detected_mime or mimetypes.guess_type(filename)[0]
        if kind == "image":
            if not mime or mime.lower() not in ALLOWED_IMAGE_MIME:
                raise RpaUnsupportedMime(f"超出图片 MIME 白名单: {mime}")

        # 大小限制
        size_bytes = len(content_bytes)
        max_bytes = MAX_IMAGE_BYTES if kind == "image" else MAX_FILE_BYTES
        if size_bytes > max_bytes:
            raise RpaInvalidParams(f"媒体大小超出上限: {size_bytes}>{max_bytes}")

        # 校验和
        checksum = media.get("checksum") or {}
        algo = (checksum.get("algo") or "").lower()
        value = checksum.get("value")
        if algo and value:
            if algo == "md5":
                digest = hashlib.md5(content_bytes).hexdigest()
            elif algo == "sha256":
                digest = hashlib.sha256(content_bytes).hexdigest()
            else:
                raise RpaInvalidParams("不支持的校验算法，仅支持 md5/sha256")
            if digest.lower() != str(value).lower():
                raise RpaMediaError("媒体完整性校验失败")

        # 写入临时文件并发送
        tmp_dir = tempfile.gettempdir()
        tmp_path = os.path.join(tmp_dir, f"wxauto_{int(time.time()*1000)}_{filename}")
        with open(tmp_path, "wb") as f:
            f.write(content_bytes)

        self._switch_to(to)
        r = self.wx.SendFiles(tmp_path)
        if not r:
            raise RuntimeError(r["message"])  # WxResponse

        caption = params.get("caption")
        if caption:
            self.wx.SendMsg(caption)
        return None

    # ========== 轮询上报 ==========

    def _poll_new_messages(self):
        """后台轮询微信新消息，转换为协议 event 上报。"""
        interval = max(1, WxParam.LISTEN_INTERVAL)
        while not self._stop_event.is_set():
            try:
                new_pack = self.wx.GetNextNewMessage()
                if new_pack and (msgs := new_pack.get("msg")):
                    for m in msgs:
                        self._send_event_wechat_message(m)
            except Exception as e:
                wxlog.debug(f"轮询消息异常: {e}")
            time.sleep(interval)

    # ========== 生命周期 ==========

    def start(self):
        """启动 WS 与消息轮询。"""
        self._stop_event.clear()
        self.ws.start()
        if not self._poll_thread or not self._poll_thread.is_alive():
            self._poll_thread = threading.Thread(target=self._poll_new_messages, daemon=True)
            self._poll_thread.start()

    def stop(self):
        """停止 WS 与消息轮询。"""
        self._stop_event.set()
        self.ws.stop()
        if self._poll_thread:
            self._poll_thread.join(timeout=3)


def start_rpa_bridge(wx: WeChat, server_ws_url: str, device_id: str, token: Optional[str] = None) -> WeChatRpaAgent:
    """简化入口：创建并启动 WeChat RPA 代理。

    参数：
    - wx: 已初始化并显示的 `WeChat` 主窗口实例
    - server_ws_url: 完整 WS 地址，如 ws://host:8080/ws/rpa
    - device_id: 设备标识
    - token: 可选 Bearer Token

    返回：已启动的 `WeChatRpaAgent` 实例
    """
    agent = WeChatRpaAgent(wx=wx, server_ws_base=server_ws_url, device_id=device_id, token=token)
    agent.start()
    return agent
