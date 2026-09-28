#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
豆包朗读 — PopClip 扩展主程序。

选中文本 -> 火山引擎豆包语音合成 (WebSocket V3) -> afplay 播放。

* 仅使用 Python 标准库，无需 pip 安装任何依赖。
* API Key 获取顺序（按优先级）：
    ① PopClip 扩展设置中的「API Key」选项（推荐，开箱即用）；
    ② macOS 钥匙串（service = "popclip-doubaotts", account "api_key"）；
    ③ 旧版控制台：account "app_id"  -> X-Api-App-Id
                    account "access_key" -> X-Api-Access-Key
* PopClip 选项经环境变量传入：POPCLIP_OPTION_APIKEY / POPCLIP_OPTION_VOICE / POPCLIP_OPTION_SPEED。
* 点击 PopClip 的 spinner 可取消：脚本捕获 SIGTERM/SIGINT，杀掉 afplay 并清理临时文件。
"""

import hashlib
import json
import os
import re
import signal
import socket
import ssl
import struct
import subprocess
import sys
import gzip
import tempfile
import time
import traceback
import uuid
from base64 import b64encode
from hashlib import sha1
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# 调试日志：记录每一步到文件，方便定位问题。
# 日志路径：~/Library/Logs/popclip-doubaotts/debug.log
# 注意：绝不记录 API Key / Access Key 明文。
# ---------------------------------------------------------------------------

_DEBUG_LOG = os.path.expanduser("~/Library/Logs/popclip-doubaotts/debug.log")


def _dlog(msg):
    # type: (str) -> None
    try:
        d = os.path.dirname(_DEBUG_LOG)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

KEYCHAIN_SERVICE = "popclip-doubaotts"

# 单次请求文本上限（字符）。文档未声明通用上限；4000 为保守值，
# 覆盖一两页文章的常见选中长度，兼顾长文本超时风险。
MAX_TEXT_CHARS = 4000

CACHE_DIR = os.path.expanduser("~/Library/Caches/popclip-doubaotts")

# WebSocket / 协议参数（待文档确认后填写）
WS_URL = ""          # 例如 wss://openspeech.bytedance.com/...
PROTOCOL = {}        # type: dict


# ---------------------------------------------------------------------------
# 钥匙串
# ---------------------------------------------------------------------------

def keychain(account):
    # type: (str) -> str
    p = subprocess.run(
        ["security", "find-generic-password",
         "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
        capture_output=True, text=True)
    if p.returncode != 0:
        _dlog("keychain: MISSING account=%r stderr=%r" % (account, p.stderr.strip()[:200]))
        raise RuntimeError(
            "未配置 API Key：请在 PopClip 扩展设置中填写「API Key」，"
            "或在钥匙串中配置 '%s'（service=%s），命令：\n"
            "security add-generic-password -s %s -a %s -w '<值>'"
            % (account, KEYCHAIN_SERVICE, KEYCHAIN_SERVICE, account))
    _dlog("keychain: found account=%r (len=%d)" % (account, len(p.stdout.strip())))
    return p.stdout.strip()


# ---------------------------------------------------------------------------
# 文本切分
# ---------------------------------------------------------------------------

_SENT_END = re.compile(r"(.+?[。！？；…\n])(?=\s|$)", re.S)


def split_text(text, limit):
    # type: (str, int) -> List[str]
    """按段落累积，超长段落再按句子切分，保证每块 <= limit 字符。"""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks = []          # type: List[str]
    current = []         # type: List[str]
    current_len = 0

    def flush():
        if current:
            chunks.append("".join(current).strip())
            del current[:]

    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        pieces = [para] if len(para) <= limit else _split_sentences(para, limit)
        for piece in pieces:
            if current_len + len(piece) + 1 > limit and current:
                flush()
                current_len = 0
            current.append(piece + "\n")
            current_len += len(piece) + 1
    flush()
    return [c for c in chunks if c]


def _split_sentences(para, limit):
    # type: (str, int) -> List[str]
    parts = []   # type: List[str]
    buf = []     # type: List[str]
    buf_len = 0
    for m in _SENT_END.finditer(para):
        s = m.group(1)
        if buf_len + len(s) > limit and buf:
            parts.append("".join(buf))
            buf, buf_len = [], 0
        buf.append(s)
        buf_len += len(s)
    tail_start = sum(len(p) for p in parts) + buf_len
    tail = para[tail_start:].strip()
    if buf:
        parts.append("".join(buf))
    if tail:
        # 兜底：硬切
        parts.extend(tail[i:i + limit] for i in range(0, len(tail), limit))
    return parts


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

def cache_key(text, voice, speed):
    # type: (str, str, str) -> str
    h = hashlib.sha256()
    h.update(b"doubaotts-v1\x00")
    h.update(voice.encode("utf-8")); h.update(b"\x00")
    h.update(speed.encode("utf-8")); h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def cache_get(key):
    # type: (str) -> Optional[str]
    path = os.path.join(CACHE_DIR, key + ".mp3")
    return path if os.path.isfile(path) else None


def cache_put(key, src_path):
    # type: (str, str) -> None
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        dst = os.path.join(CACHE_DIR, key + ".mp3")
        with open(src_path, "rb") as fsrc, open(dst, "wb") as fdst:
            fdst.write(fsrc.read())
    except OSError:
        pass  # 缓存失败不影响主流程


# ---------------------------------------------------------------------------
# WebSocket 客户端（标准库实现）
# ---------------------------------------------------------------------------

class WSClient(object):
    def __init__(self, url, headers, timeout=30):
        # type: (str, dict, int) -> None
        assert url.startswith("wss://") or url.startswith("ws://")
        secure = url.startswith("wss://")
        rest = url[len("wss://") if secure else len("ws://"):]
        host_port, _, path = rest.partition("/")
        path = "/" + path
        if ":" in host_port:
            host, port_s = host_port.rsplit(":", 1)
            port = int(port_s)
        else:
            host = host_port
            port = 443 if secure else 80
        key = b64encode(os.urandom(16)).decode("ascii")
        req_lines = [
            "GET %s HTTP/1.1" % path,
            "Host: %s" % host_port,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in headers.items():
            req_lines.append("%s: %s" % (k, v))
        req = "\r\n".join(req_lines) + "\r\n\r\n"

        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if secure:
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=host)
            sock.settimeout(timeout)
            sock.sendall(req.encode("latin-1"))
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    raise RuntimeError("WebSocket 握手失败：连接被关闭")
                resp += chunk
            head = resp.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            if " 101 " not in head.split("\r\n", 1)[0]:
                raise RuntimeError("WebSocket 握手失败：%s" % head.split("\r\n", 1)[0])
            accept = sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii"))
            if b64encode(accept.digest()).decode("ascii") not in head:
                raise RuntimeError("WebSocket 握手失败：Sec-WebSocket-Accept 不匹配")
        except Exception:
            sock.close()
            raise
        self.sock = sock
        self._buf = b""

    def _recv_exact(self, n):
        # type: (int) -> bytes
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 连接被对端关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_binary(self, payload):
        # type: (bytes) -> None
        # 客户端帧必须 mask
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x82, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x82, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x82, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv_message(self):
        # type: () -> Tuple[int, bytes]
        """返回 (opcode, payload)。自动处理 ping/pong/close。"""
        while True:
            b1, b2 = struct.unpack("!BB", self._recv_exact(2))
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            if masked:
                mask = self._recv_exact(4)
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping -> pong
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise RuntimeError("WebSocket 被服务端关闭")
            if opcode == 0x0:
                raise RuntimeError("不支持分片帧")
            if not fin:
                raise RuntimeError("不支持分片帧")
            return opcode, payload

    def _send_control(self, opcode, payload):
        # type: (int, bytes) -> None
        mask = os.urandom(4)
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | len(payload))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def close(self):
        # type: () -> None
        try:
            self._send_control(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 豆包 TTS 协议（WebSocket 单向流式 V3）
# 来源：火山引擎文档《WebSocket 单向流式-V3》（2026-05-25）
# ---------------------------------------------------------------------------

WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream"

# 鉴权（按优先级，见 _auth_headers）：
#   ① 扩展设置选项 POPCLIP_OPTION_APIKEY  -> X-Api-Key
#   ② 钥匙串 account "api_key"            -> X-Api-Key
#   ③ 旧版：钥匙串 account "app_id"       -> X-Api-App-Id
#           钥匙串 account "access_key"    -> X-Api-Access-Key
# 资源 ID 决定模型版本与计费；默认 seed-tts-2.0，可用环境变量 DOUBAO_RESOURCE_ID 覆盖。
DEFAULT_RESOURCE_ID = "seed-tts-2.0"

# 默认音色为爽快思思 2.0（seed-tts-2.0 资源下的对应版本）；
# 注意 1.0 的 zh_female_shuangkuaisisi_moon_bigtts 与 seed-tts-2.0 不匹配，服务端会拒绝。
# 留空选项时用它。
DEFAULT_VOICE = "zh_female_shuangkuaisisi_uranus_bigtts"

# 文档示例原文如此（单向流接口仍用该 namespace）。
NAMESPACE = "BidirectionalTTS"

# 事件编号（文档 2.3 节「event 定义」事件表，共 6 个）：
#   上行：FinishConnection = 2（结束连接；发送文本阶段不需要上行 event 帧，
#         SendText 的 JSON 顶层不带 event 字段）
#   下行：SessionFinished = 152，TTSSentenceStart = 350，TTSSentenceEnd = 351，
#         TTSResponse = 352，ConnectionFinished = 52
EVENT_FINISH_CONNECTION = 2     # 上行：结束连接
EVENT_CONNECTION_FINISHED = 52  # 下行：连接已结束
EVENT_TTS_RESPONSE = 352        # 下行：音频帧
EVENT_SESSION_FINISHED = 152    # 下行：一次合成结束
EVENT_SENTENCE_START = 350      # 下行：句开始
EVENT_SENTENCE_END = 351        # 下行：句结束

STATUS_OK = 20000000


def _build_frame(msg_type, flags, serialization, payload, event=None):
    # type: (int, int, int, bytes, object) -> bytes
    """组装 V3 二进制帧：4 字节头 [+4 字节 event] + uint32 长度 + payload。"""
    header = bytes([0x11, ((msg_type & 0xF) << 4) | (flags & 0xF),
                    ((serialization & 0xF) << 4), 0x00])
    if event is not None:
        header += struct.pack("!I", event)
    return header + struct.pack("!I", len(payload)) + payload


def _parse_frame(data):
    # type: (bytes) -> tuple
    """解析服务端下行帧。
    返回 ("audio", event, session_id, audio_bytes)
    或   ("event", event, payload_json_or_bytes)
    或   ("error", None, payload)。"""
    if len(data) < 8:
        raise RuntimeError("收到畸形帧（过短）")
    b0, b1, b2 = data[0], data[1], data[2]
    if b0 != 0x11:
        raise RuntimeError("未知协议版本：0x%02x" % b0)
    msg_type = (b1 >> 4) & 0xF
    flags = b1 & 0xF
    serialization = (b2 >> 4) & 0xF
    compression = b2 & 0xF
    offset = 4
    event = None
    if flags & 0b0100:
        event = struct.unpack("!I", data[4:8])[0]
        offset = 8
    if msg_type == 0xB:  # Audio-only response
        sid_len = struct.unpack("!I", data[offset:offset + 4])[0]
        offset += 4
        session_id = data[offset:offset + sid_len]
        offset += sid_len
        audio_len = struct.unpack("!I", data[offset:offset + 4])[0]
        offset += 4
        audio = data[offset:offset + audio_len]
        if len(audio) != audio_len:
            raise RuntimeError("音频帧截断")
        return ("audio", event, session_id, audio)
    payload_len = struct.unpack("!I", data[offset:offset + 4])[0]
    offset += 4
    payload = data[offset:offset + payload_len]
    if compression == 0b0001:
        payload = gzip.decompress(payload)
    if serialization == 0b0001:
        try:
            payload = json.loads(payload.decode("utf-8"))
        except Exception:
            pass
    if msg_type == 0xF:
        return ("error", None, payload)
    return ("event", event, payload)


def _speed_to_rate(speed):
    # type: (str) -> int
    """PopClip 选项语速倍率（如 "1.5"）-> speech_rate [-50, 100]。"""
    try:
        mult = float(speed)
    except (TypeError, ValueError):
        return 0
    rate = int(round((mult - 1.0) * 100))
    return max(-50, min(100, rate))


def _auth_headers():
    # type: () -> Tuple[dict, str]
    """返回 (headers, 方案名)。优先扩展设置中的 API Key，其次钥匙串，最后回落旧版 AppId。"""
    api_key = os.environ.get("POPCLIP_OPTION_APIKEY", "").strip()
    if api_key:
        _dlog("auth: using API Key from PopClip option (len=%d)" % len(api_key))
        source = "option"
    else:
        try:
            api_key = keychain("api_key")
            source = "keychain"
        except RuntimeError:
            api_key = None
            source = ""
    if api_key:
        headers = {
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": os.environ.get("DOUBAO_RESOURCE_ID", DEFAULT_RESOURCE_ID),
            "X-Api-Request-Id": uuid.uuid4().hex,
        }
        return headers, "api-key:" + source
    # 旧版控制台
    app_id = keychain("app_id")
    access_key = keychain("access_key")
    headers = {
        "X-Api-App-Id": app_id,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": os.environ.get("DOUBAO_RESOURCE_ID", DEFAULT_RESOURCE_ID),
        "X-Api-Request-Id": uuid.uuid4().hex,
    }
    return headers, "app-id"


def synthesize(text, voice, speed):
    # type: (str, str, str) -> bytes
    """合成一段文本，返回完整音频字节（mp3）。"""
    if not voice:
        voice = DEFAULT_VOICE
    headers, scheme = _auth_headers()
    _dlog("synthesize: auth scheme=%s resource=%s" % (
        scheme, headers.get("X-Api-Resource-Id", "?")))
    # 注意：按文档，SendText 阶段不需要上行 event 帧，JSON 顶层不带 event 字段。
    req = {
        "user": {"uid": "popclip-doubaotts"},
        "namespace": NAMESPACE,
        "req_params": {
            "text": text,
            "speaker": voice,
            "audio_params": {
                "format": "mp3",
                "sample_rate": 24000,
                "speech_rate": _speed_to_rate(speed),
            },
        },
    }
    payload = json.dumps(req, ensure_ascii=False).encode("utf-8")
    _dlog("synthesize: voice=%r speed=%r text_len=%d payload=%d bytes" % (
        voice, speed, len(text), len(payload)))
    ws = WSClient(WS_URL, headers, timeout=30)
    _dlog("synthesize: websocket handshake OK")
    try:
        ws.send_binary(_build_frame(0x1, 0x0, 0x1, payload))
        _dlog("synthesize: SendText frame sent")
        audio_parts = []  # type: List[bytes]
        n_frames = 0
        while True:
            opcode, data = ws.recv_message()
            if opcode != 0x2:  # 只处理 binary 帧
                continue
            kind = _parse_frame(data)
            if kind[0] == "error":
                _dlog("synthesize: server error frame: %r" % (kind[2],))
                raise RuntimeError("豆包返回错误：%s" % (kind[2],))
            if kind[0] == "audio":
                audio_parts.append(kind[3])
                n_frames += 1
            elif kind[0] == "event" and kind[1] == EVENT_SESSION_FINISHED:
                meta = kind[2]
                _dlog("synthesize: SessionFinished meta=%r audio_frames=%d" % (meta, n_frames))
                if isinstance(meta, dict) and meta.get("status_code") != STATUS_OK:
                    raise RuntimeError("合成失败：%s" % (meta,))
                break
        if not audio_parts:
            raise RuntimeError("未收到任何音频数据")
        total = sum(len(p) for p in audio_parts)
        _dlog("synthesize: done audio_frames=%d total_bytes=%d" % (n_frames, total))
        try:
            ws.send_binary(_build_frame(0x1, 0b0100, 0x1, b"{}",
                                        event=EVENT_FINISH_CONNECTION))
        except Exception:
            pass
        return b"".join(audio_parts)
    finally:
        ws.close()


# ---------------------------------------------------------------------------
# 播放与取消
# ---------------------------------------------------------------------------

_afplay_proc = None  # type: Optional[subprocess.Popen]
_tmp_paths = []      # type: List[str]


def _cleanup(signum=None, frame=None):
    global _afplay_proc
    if _afplay_proc is not None and _afplay_proc.poll() is None:
        try:
            _afplay_proc.terminate()
            _afplay_proc.wait(timeout=3)
        except Exception:
            try:
                _afplay_proc.kill()
            except Exception:
                pass
    for p in _tmp_paths:
        try:
            os.unlink(p)
        except OSError:
            pass
    if signum is not None:
        sys.exit(128 + signum)


def play_file(path):
    # type: (str) -> None
    global _afplay_proc
    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)
    _dlog("play: afplay start path=%r" % (path,))
    _afplay_proc = subprocess.Popen(["afplay", path])
    rc = _afplay_proc.wait()
    _afplay_proc = None
    _dlog("play: afplay finished rc=%d" % rc)
    if rc != 0:
        raise RuntimeError("afplay 播放失败（exit=%d）" % rc)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    # type: () -> int
    _dlog("=== run start: python=%s ===" % sys.version.split()[0])
    text = sys.stdin.read()
    if not text.strip():
        _dlog("run: empty stdin, nothing to do")
        return 0
    voice = os.environ.get("POPCLIP_OPTION_VOICE", "").strip()
    speed = os.environ.get("POPCLIP_OPTION_SPEED", "").strip() or "1.0"
    _dlog("run: text_len=%d voice=%r speed=%r" % (len(text), voice, speed))

    key = cache_key(text, voice, speed)
    hit = cache_get(key)
    if hit:
        _dlog("run: cache HIT key=%s... path=%r" % (key[:12], hit))
        play_file(hit)
        return 0
    _dlog("run: cache MISS key=%s..." % key[:12])

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="doubaotts-", suffix=".mp3")
    os.close(tmp_fd)
    _tmp_paths.append(tmp_path)

    try:
        with open(tmp_path, "wb") as out:
            chunks = split_text(text, MAX_TEXT_CHARS)
            _dlog("run: split into %d chunk(s)" % len(chunks))
            for i, chunk in enumerate(chunks):
                audio = synthesize(chunk, voice, speed)
                out.write(audio)
                out.flush()
                _dlog("run: chunk %d/%d audio=%d bytes" % (i + 1, len(chunks), len(audio)))
        cache_put(key, tmp_path)
        play_file(tmp_path)
    except Exception as e:
        _dlog("run: FAILED: %s\n%s" % (e, traceback.format_exc()))
        sys.stderr.write("豆包朗读失败：%s\n" % e)
        return 1
    finally:
        _cleanup()
    _dlog("run: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
