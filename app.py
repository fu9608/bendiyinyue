"""内网音乐播放器：Flask + ffmpeg + 共享 PCM 音频流。

工作链路：
- 手机/电脑页面调用 /search 搜索歌曲，再调用 /play 或 /play_song 点播；
- 电脑上的 ffmpeg 拉取音乐 URL，统一输出 s16le / 44100 Hz / 单声道 PCM；
- PCM 写入共享环形缓冲，/radio 为每个客户端维护独立读游标；
- ESP32 直接读取 /radio 的原始 PCM，网页通过 Web Audio 解码试听；
- 点播新歌前会终止旧 ffmpeg 并清空旧缓冲，避免两首歌混在一起。

本项目只面向局域网使用，不要直接暴露到公网。
"""

from __future__ import annotations

import atexit
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Generator

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)

from music_sources import music_service


BASE_DIR = Path(__file__).resolve().parent
LOG = logging.getLogger("music-player")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _resolve_ffmpeg() -> str | None:
    configured = os.environ.get("FFMPEG_BIN", "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return str(path)
        return shutil.which(configured)
    local = BASE_DIR / "ffmpeg.exe"
    if local.is_file():
        return str(local)
    return shutil.which("ffmpeg")


FFMPEG_BIN = _resolve_ffmpeg()
SAMPLE_RATE = _env_int("RADIO_SAMPLE_RATE", 44100)
CHANNELS = _env_int("RADIO_CHANNELS", 1)
SAMPLE_BYTES = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_BYTES
STALL_TIMEOUT = float(os.environ.get("RADIO_STALL_TIMEOUT", "30"))
HOST = os.environ.get("RADIO_HOST", "0.0.0.0")
PORT = _env_int("RADIO_PORT", 5000)
CHUNK_SIZE = 8192
PREBUFFER_BYTES = max(16 * 1024, _env_int("RADIO_PREBUFFER_BYTES", BYTES_PER_SECOND * 2))
BUFFER_MAX_BYTES = max(
    PREBUFFER_BYTES + CHUNK_SIZE,
    _env_int("RADIO_BUFFER_MAX_BYTES", 4 * 1024 * 1024),
)
VOLUME_MAX = 21
MAX_URL_LENGTH = 4096

DEFAULT_ALLOWED_HOSTS = (
    "kuwo.cn",
    "kuwo.com",
    "126.net",
    "127.net",
    "163.com",
    "kugou.com",
    "kgcdn.com",
)
_configured_hosts = [
    item.strip().lower().lstrip(".")
    for item in os.environ.get("RADIO_URL_ALLOWLIST", "").split(",")
    if item.strip()
]
ALLOWED_HOSTS = tuple(dict.fromkeys(DEFAULT_ALLOWED_HOSTS + tuple(_configured_hosts)))
ALLOW_ANY_URL = os.environ.get("RADIO_ALLOW_ANY_URL", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
ALLOW_PRIVATE_URL = os.environ.get("RADIO_ALLOW_PRIVATE_URL", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def clean_text(value: object, max_length: int = 200) -> str:
    text = str(value or "")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_length]


def is_http_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def validate_play_url(value: str) -> tuple[str, str]:
    url = str(value or "").strip()
    if not url:
        return "", "缺少 url 参数"
    if len(url) > MAX_URL_LENGTH:
        return "", "url 过长"
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "", "url 格式不正确"
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "", "只允许 http/https 音频地址"
    if parsed.username or parsed.password:
        return "", "url 不能包含用户名或密码"
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        if ALLOW_PRIVATE_URL:
            return url, ""
        return "", "默认禁止本机/局域网地址；需要时设置 RADIO_ALLOW_PRIVATE_URL=1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        if ALLOW_PRIVATE_URL:
            return url, ""
        return "", "默认禁止私有/本机 IP；需要时设置 RADIO_ALLOW_PRIVATE_URL=1"
    if ALLOW_ANY_URL:
        return url, ""

    for suffix in ALLOWED_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return url, ""
    return (
        "",
        "该域名不在播放白名单中；可通过 RADIO_URL_ALLOWLIST 追加域名，"
        "或设置 RADIO_ALLOW_ANY_URL=1",
    )


class StreamSubscriber:
    """一个 /radio 客户端的读游标；数据本身由所有客户端共享。"""

    __slots__ = ("generation", "offset", "started")

    def __init__(self, generation: int = 0, offset: int = 0, started: bool = False) -> None:
        self.generation = generation
        self.offset = offset
        self.started = started


class SharedStreamBuffer:
    """线程安全、有上限的 PCM 共享缓冲。

    ffmpeg 读取线程只负责 append；每个 /radio 响应线程使用自己的 offset 读取，
    因此一个慢客户端不会拖住其他客户端。缓冲超过上限时从最旧的数据开始丢弃。
    """

    def __init__(self, max_bytes: int, prebuffer_bytes: int) -> None:
        self._cond = threading.Condition()
        self._max_bytes = max(CHUNK_SIZE, int(max_bytes))
        self._prebuffer_bytes = max(0, min(int(prebuffer_bytes), self._max_bytes))
        self._chunks: deque[tuple[int, bytes]] = deque()
        self._base = 0
        self._end = 0
        self._generation = 0
        self._stopped = True
        self._dropped_bytes = 0

    def reset(self, generation: int | None = None) -> int:
        """开始新歌：清空旧 PCM，并通知所有客户端跳到新流。"""
        with self._cond:
            if generation is None:
                self._generation += 1
            else:
                self._generation = int(generation)
            self._chunks.clear()
            self._base = 0
            self._end = 0
            self._stopped = False
            self._dropped_bytes = 0
            self._cond.notify_all()
            return self._generation

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._chunks.clear()
            self._base = 0
            self._end = 0
            self._cond.notify_all()

    def write(self, generation: int, data: bytes) -> None:
        if not data:
            return
        with self._cond:
            if self._stopped or generation != self._generation:
                return
            self._chunks.append((self._end, bytes(data)))
            self._end += len(data)
            while self._chunks and self._end - self._chunks[0][0] > self._max_bytes:
                start, chunk = self._chunks.popleft()
                self._base = start + len(chunk)
                self._dropped_bytes += len(chunk)
            if not self._chunks:
                self._base = self._end
            self._cond.notify_all()

    def _prefill_offset_locked(self) -> int:
        target = max(self._base, self._end - self._prebuffer_bytes)
        for start, chunk in self._chunks:
            if start + len(chunk) > target:
                return start
        return self._end

    def subscribe(self) -> StreamSubscriber | None:
        with self._cond:
            if self._stopped:
                return None
            subscriber = StreamSubscriber(self._generation)
            buffered = self._end - self._base
            if buffered >= self._prebuffer_bytes:
                subscriber.offset = self._prefill_offset_locked()
                subscriber.started = True
            else:
                subscriber.offset = self._base
            return subscriber

    def read(self, subscriber: StreamSubscriber, max_bytes: int = CHUNK_SIZE) -> bytes | None:
        """阻塞读取一段数据；返回 None 表示停止。"""
        if max_bytes <= 0:
            return b""
        with self._cond:
            while True:
                if self._stopped:
                    return None

                if subscriber.generation != self._generation:
                    subscriber.generation = self._generation
                    subscriber.offset = self._base
                    subscriber.started = False

                if subscriber.offset < self._base:
                    subscriber.offset = self._base
                elif subscriber.offset > self._end:
                    subscriber.offset = self._end

                available = self._end - subscriber.offset
                if not subscriber.started:
                    if available >= self._prebuffer_bytes:
                        subscriber.started = True
                    else:
                        self._cond.wait(0.5)
                        continue

                if available > 0:
                    break
                self._cond.wait(0.5)

            out = bytearray()
            cursor = subscriber.offset
            for start, chunk in self._chunks:
                chunk_end = start + len(chunk)
                if chunk_end <= cursor:
                    continue
                begin = cursor - start
                take = min(chunk_end - cursor, max_bytes - len(out))
                if take <= 0:
                    break
                out.extend(chunk[begin : begin + take])
                cursor += take
                if len(out) >= max_bytes:
                    break
            subscriber.offset = cursor
            return bytes(out)

    def stats(self) -> dict:
        with self._cond:
            return {
                "bytes": self._end - self._base,
                "max_bytes": self._max_bytes,
                "prebuffer_bytes": self._prebuffer_bytes,
                "generation": self._generation,
                "dropped_bytes": self._dropped_bytes,
                "stopped": self._stopped,
            }


class MusicManager:
    """管理唯一的 ffmpeg 进程和共享 PCM 缓冲。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._clients: set[StreamSubscriber] = set()
        self._stream_buffer = SharedStreamBuffer(BUFFER_MAX_BYTES, PREBUFFER_BYTES)
        self._source: dict | None = None
        self._manual_stop = True
        self._state = "stopped"
        self._last_error = ""
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._generation = 0
        self._last_data_at = 0.0
        self._process_started_at = 0.0
        self._started_at = 0.0
        self._selected_at = 0.0
        self._bytes_total = 0
        self._volume = max(0, min(VOLUME_MAX, _env_int("RADIO_VOLUME", 15)))
        self._restart_count = 0
        self._last_exit_code: int | None = None
        self._shutdown = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, name="music-watchdog", daemon=True)
        self._watchdog.start()

    @property
    def ffmpeg_available(self) -> bool:
        if not FFMPEG_BIN:
            return False
        return Path(FFMPEG_BIN).is_file() or shutil.which(FFMPEG_BIN) is not None

    def _command(self, source: dict) -> list[str]:
        assert FFMPEG_BIN is not None
        command = [
            FFMPEG_BIN,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            "-rw_timeout",
            "15000000",
        ]
        referer = str(source.get("referer") or "").strip()
        if referer:
            command += ["-referer", referer]
        command += [
            "-user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "-probesize",
            "1000000",
            "-analyzeduration",
            "1000000",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-re",
            "-i",
            source["url"],
            "-vn",
            "-sn",
            "-dn",
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            str(CHANNELS),
            "-flush_packets",
            "1",
            "pipe:1",
        ]
        return command

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes] | None) -> None:
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            LOG.debug("terminate ffmpeg failed", exc_info=True)

    def _start_process_locked(self) -> None:
        source = self._source
        if self._manual_stop or source is None or self._process is not None:
            return
        if not self.ffmpeg_available:
            self._state = "error"
            self._last_error = "未找到 ffmpeg。请把 ffmpeg.exe 放到项目目录，或设置 FFMPEG_BIN。"
            self._manual_stop = True
            self._stream_buffer.stop()
            self._clients.clear()
            return

        self._generation += 1
        generation = self._generation
        self._stream_buffer.reset(generation)
        self._last_error = ""
        self._stderr_tail.clear()
        self._last_data_at = 0.0
        self._process_started_at = time.monotonic()
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
                self._command(source),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=flags,
            )
        except Exception as exc:
            self._state = "error"
            self._last_error = f"启动 ffmpeg 失败：{exc}"
            self._manual_stop = True
            self._stream_buffer.stop()
            self._clients.clear()
            LOG.exception("start ffmpeg failed")
            return

        self._process = process
        self._state = "starting"
        self._started_at = 0.0
        self._bytes_total = 0
        LOG.info(
            "ffmpeg started for %s - %s (generation %s, pid %s)",
            source.get("title") or "未知歌曲",
            source.get("artist") or "未知歌手",
            generation,
            process.pid,
        )
        threading.Thread(
            target=self._read_audio,
            args=(process, generation),
            name="music-reader",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(process, generation),
            name="music-stderr",
            daemon=True,
        ).start()

    def _read_audio(self, process: subprocess.Popen[bytes], generation: int) -> None:
        carry = b""
        try:
            assert process.stdout is not None
            while not self._shutdown.is_set():
                try:
                    chunk = os.read(process.stdout.fileno(), CHUNK_SIZE)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                if generation != self._generation:
                    break

                # s16le 每个采样 2 字节；保留可能落单的 1 字节，避免客户端声道错位。
                chunk = carry + chunk
                usable = len(chunk) - (len(chunk) % SAMPLE_BYTES)
                if usable <= 0:
                    carry = chunk
                    continue
                data = chunk[:usable]
                carry = chunk[usable:]

                with self._lock:
                    if generation != self._generation:
                        break
                    self._last_data_at = time.monotonic()
                    self._bytes_total += len(data)
                    if self._state in ("starting", "buffering"):
                        self._state = "playing"
                        self._started_at = time.time()
                    self._last_error = ""
                    self._stream_buffer.write(generation, data)
        finally:
            try:
                assert process.stdout is not None
                process.stdout.close()
            except Exception:
                pass

        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._terminate(process)
            return_code = process.poll()
            if return_code is None:
                return_code = -1

        with self._lock:
            if generation != self._generation:
                return
            self._process = None
            self._last_exit_code = return_code
            self._manual_stop = True
            self._stream_buffer.stop()
            self._clients.clear()
            if return_code in (0, None):
                self._state = "stopped"
                LOG.info("track finished")
            else:
                self._state = "error"
                tail = self._stderr_tail[-1] if self._stderr_tail else ""
                detail = f"：{tail}" if tail else ""
                self._last_error = f"ffmpeg 已退出（code {return_code}）{detail}"
                LOG.warning("ffmpeg exited with code %s", return_code)

    def _read_stderr(self, process: subprocess.Popen[bytes], generation: int) -> None:
        try:
            assert process.stderr is not None
            for raw_line in process.stderr:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                with self._lock:
                    if generation == self._generation:
                        self._stderr_tail.append(line)
        except Exception:
            LOG.debug("read ffmpeg stderr failed", exc_info=True)
        finally:
            try:
                assert process.stderr is not None
                process.stderr.close()
            except Exception:
                pass

    def _watch(self) -> None:
        while not self._shutdown.wait(1.0):
            kill_process: subprocess.Popen[bytes] | None = None
            with self._lock:
                if self._manual_stop or self._source is None or self._process is None:
                    continue
                now = time.monotonic()
                reference = self._last_data_at or self._process_started_at
                if reference and now - reference > STALL_TIMEOUT:
                    kill_process = self._process
                    self._process = None
                    self._generation += 1
                    self._manual_stop = True
                    self._state = "error"
                    self._last_error = f"超过 {STALL_TIMEOUT:.0f} 秒没有音频数据，已停止"
                    self._stream_buffer.stop()
                    self._clients.clear()
            if kill_process is not None:
                LOG.warning("audio stalled; terminating ffmpeg")
                self._terminate(kill_process)

    def _activate(self, source: dict) -> tuple[bool, str]:
        if not self.ffmpeg_available:
            return False, "未找到 ffmpeg。请把 ffmpeg.exe 放到项目目录，或设置 FFMPEG_BIN。"
        with self._lock:
            old_process = self._process
            self._generation += 1
            self._stream_buffer.reset(self._generation)
            self._process = None
            self._source = source
            self._manual_stop = False
            self._state = "starting"
            self._last_error = ""
            self._last_data_at = 0.0
            self._process_started_at = 0.0
            self._started_at = 0.0
            self._selected_at = time.time()
            self._bytes_total = 0
            self._restart_count = 0
            self._last_exit_code = None
            self._stderr_tail.clear()
        self._terminate(old_process)
        with self._lock:
            if self._source is source and not self._manual_stop:
                self._start_process_locked()
        LOG.info(
            "track selected: %s - %s",
            source.get("title") or "未知歌曲",
            source.get("artist") or "未知歌手",
        )
        return True, ""

    def play(
        self,
        *,
        url: str,
        title: str = "",
        artist: str = "",
        album: str = "",
        cover: str = "",
        source: str = "",
        source_name: str = "",
        song_id: str = "",
        referer: str = "",
    ) -> tuple[bool, str]:
        source_info = {
            "kind": "song",
            "url": url,
            "referer": referer,
            "title": title or "网络音乐",
            "artist": artist or "未知歌手",
            "album": album,
            "cover": cover,
            "source": source,
            "source_name": source_name,
            "song_id": song_id,
        }
        return self._activate(source_info)

    def stop(self) -> None:
        with self._lock:
            self._manual_stop = True
            self._source = None
            self._generation += 1
            old_process = self._process
            self._process = None
            self._state = "stopped"
            self._last_error = ""
            self._stream_buffer.stop()
            self._clients.clear()
        self._terminate(old_process)
        LOG.info("playback stopped")

    def set_volume(self, volume: int) -> int:
        volume = max(0, min(VOLUME_MAX, int(volume)))
        with self._lock:
            self._volume = volume
        LOG.info("ESP32 volume set to %d", volume)
        return volume

    def adjust_volume(self, delta: int) -> int:
        with self._lock:
            return self.set_volume(self._volume + delta)

    def shutdown(self) -> None:
        self._shutdown.set()
        self.stop()

    def subscribe(self) -> StreamSubscriber | None:
        with self._lock:
            if self._manual_stop or self._source is None:
                return None
            subscriber = self._stream_buffer.subscribe()
            if subscriber is not None:
                self._clients.add(subscriber)
            return subscriber

    def unsubscribe(self, subscriber: StreamSubscriber) -> None:
        with self._lock:
            self._clients.discard(subscriber)

    def read_stream(self, subscriber: StreamSubscriber, max_bytes: int = CHUNK_SIZE) -> bytes | None:
        return self._stream_buffer.read(subscriber, max_bytes)

    def is_stopped(self) -> bool:
        with self._lock:
            return self._manual_stop or self._source is None

    def snapshot(self) -> dict:
        with self._lock:
            source = self._source
            now = time.monotonic()
            fresh = bool(self._last_data_at and now - self._last_data_at < 8.0)
            buffer_stats = self._stream_buffer.stats()
            state = self._state
            if self._manual_stop or source is None:
                state = "error" if state == "error" else "stopped"
            elif self._process is None:
                state = "error" if state == "error" else "starting"
            elif fresh:
                state = "playing"
            elif state == "playing":
                state = "buffering"
            return {
                "ok": True,
                "state": state,
                "playing": bool(source is not None and not self._manual_stop),
                "streaming": bool(self._process is not None and fresh),
                "kind": source.get("kind") if source else None,
                "title": source.get("title") if source else None,
                "artist": source.get("artist") if source else None,
                "album": source.get("album") if source else None,
                "cover": source.get("cover") if source else None,
                "song_source": source.get("source") if source else None,
                "song_source_name": source.get("source_name") if source else None,
                "song_id": source.get("song_id") if source else None,
                "source_url": source.get("url") if source else None,
                "listeners": len(self._clients),
                "restarts": self._restart_count,
                "bytes": self._bytes_total,
                "buffer_bytes": buffer_stats["bytes"],
                "buffer_max_bytes": buffer_stats["max_bytes"],
                "prebuffer_bytes": buffer_stats["prebuffer_bytes"],
                "prebuffer_seconds": round(
                    buffer_stats["prebuffer_bytes"] / max(1, BYTES_PER_SECOND), 2
                ),
                "buffer_dropped_bytes": buffer_stats["dropped_bytes"],
                "volume": self._volume,
                "volume_max": VOLUME_MAX,
                "last_error": self._last_error,
                "last_exit_code": self._last_exit_code,
                "ffmpeg_available": self.ffmpeg_available,
                "ffmpeg_bin": FFMPEG_BIN,
                "format": "s16le",
                "audio_codec": "pcm_s16le",
                "sample_rate": SAMPLE_RATE,
                "channels": CHANNELS,
                "bits_per_sample": SAMPLE_BYTES * 8,
                "bytes_per_second": BYTES_PER_SECOND,
                "bitrate": f"{BYTES_PER_SECOND * 8 // 1000}k",
                "uptime": int(time.time() - self._selected_at) if self._selected_at else 0,
                "server_time": int(time.time()),
            }


app = Flask(__name__)
app.json.ensure_ascii = False
app.json.compact = True
manager = MusicManager()
atexit.register(manager.shutdown)


def _wants_json() -> bool:
    return request.args.get("format") == "json" or "application/json" in request.headers.get("Accept", "")


def _param(name: str) -> str:
    value = None
    if request.is_json:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            value = body.get(name)
    if value is None:
        value = request.values.get(name, "")
    return str(value or "")


def _json_no_store(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _play_response(ok: bool, message: str, status: int = 200):
    payload = manager.snapshot()
    payload["ok"] = ok
    if message:
        payload["error"] = message
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        if sock is not None:
            sock.close()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(item[4][0])
    except OSError:
        pass
    return sorted(ip for ip in addresses if not ip.startswith("127.") and not ip.startswith("169.254."))


@app.get("/")
def index() -> str:
    return render_template(
        "index.html",
        status=manager.snapshot(),
        radio_url=f"http://{request.host}/radio",
        stream_url=f"http://{request.host}/stream",
        music_sources=music_service.enabled_sources,
        prebuffer_seconds=round(PREBUFFER_BYTES / max(1, BYTES_PER_SECOND), 2),
    )


@app.get("/search")
def search_music():
    keyword = _param("keyword")
    source = clean_text(_param("source"), 32).lower() or "auto"
    try:
        limit = int(_param("limit") or 12)
    except ValueError:
        return _json_no_store({"ok": False, "error": "limit 必须是整数"}, 400)
    try:
        payload = music_service.search(keyword, limit=limit, source=source)
    except ValueError as exc:
        return _json_no_store({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        LOG.warning("search failed: %s", exc)
        return _json_no_store({"ok": False, "error": f"搜索失败：{exc}"}, 502)

    if not payload.get("songs") and payload.get("errors"):
        payload["ok"] = False
        payload["error"] = "所有音源搜索失败，请稍后重试或切换音源"
        return _json_no_store(payload, 502)
    return _json_no_store(payload)


@app.route("/play", methods=["GET", "POST"])
def play_music():
    url, error = validate_play_url(_param("url"))
    if error:
        return _json_no_store({"ok": False, "error": error}, 400)
    title = clean_text(_param("title"), 200) or "网络音乐"
    artist = clean_text(_param("artist"), 200) or "未知歌手"
    album = clean_text(_param("album"), 200)
    cover = clean_text(_param("cover"), 1000)
    source = clean_text(_param("source"), 32).lower()
    source_name = clean_text(_param("source_name"), 64)
    song_id = clean_text(_param("song_id"), 200)
    referer = _param("referer").strip()
    if referer and not is_http_url(referer):
        referer = ""
    if not referer and source:
        referer = music_service.referer_for(source)
    ok, message = manager.play(
        url=url,
        title=title,
        artist=artist,
        album=album,
        cover=cover,
        source=source,
        source_name=source_name,
        song_id=song_id,
        referer=referer,
    )
    return _play_response(ok, message, 200 if ok else 503)


@app.route("/play_song", methods=["GET", "POST"])
def play_song():
    source = clean_text(_param("source"), 32).lower()
    song_id = clean_text(_param("song_id"), 200)
    album_id = clean_text(_param("album_id"), 200)
    if not source or not song_id:
        return _json_no_store({"ok": False, "error": "缺少 source 或 song_id"}, 400)
    try:
        url = music_service.resolve_by_id(source, song_id, album_id)
    except ValueError as exc:
        return _json_no_store({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        LOG.info("resolve %s/%s failed: %s", source, song_id, exc)
        return _json_no_store({"ok": False, "error": f"获取播放地址失败：{exc}"}, 502)

    ok, message = manager.play(
        url=url,
        title=clean_text(_param("title"), 200) or "网络音乐",
        artist=clean_text(_param("artist"), 200) or "未知歌手",
        album=clean_text(_param("album"), 200),
        cover=clean_text(_param("cover"), 1000),
        source=source,
        source_name=clean_text(_param("source_name"), 64),
        song_id=song_id,
        referer=music_service.referer_for(source),
    )
    return _play_response(ok, message, 200 if ok else 503)


@app.get("/sources")
def sources():
    return _json_no_store({"ok": True, "sources": music_service.enabled_sources})


@app.get("/stop")
def stop_playback():
    manager.stop()
    if _wants_json():
        return _json_no_store(manager.snapshot())
    return redirect(url_for("index"))


def _radio_generator(subscriber: StreamSubscriber) -> Generator[bytes, None, None]:
    try:
        while True:
            chunk = manager.read_stream(subscriber)
            if chunk is None:
                break
            if chunk:
                yield chunk
    finally:
        manager.unsubscribe(subscriber)


@app.get("/radio")
def radio():
    subscriber = manager.subscribe()
    if subscriber is None:
        return Response("playback is stopped\n", status=503, mimetype="text/plain")
    response = Response(stream_with_context(_radio_generator(subscriber)), mimetype="application/octet-stream")
    response.direct_passthrough = True
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["X-Audio-Format"] = f"s16le;rate={SAMPLE_RATE};channels={CHANNELS}"
    response.headers["X-Sample-Rate"] = str(SAMPLE_RATE)
    response.headers["X-Channels"] = str(CHANNELS)
    response.headers["X-Bits-Per-Sample"] = str(SAMPLE_BYTES * 8)
    response.headers["X-Prebuffer-Bytes"] = str(PREBUFFER_BYTES)
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/stream")
def stream():
    """与 /radio 相同，方便播放器或调试工具使用。"""
    return radio()


@app.get("/status")
def status():
    return _json_no_store(manager.snapshot())


@app.get("/volume")
def get_volume():
    return _json_no_store({
        "ok": True,
        "volume": manager.snapshot()["volume"],
        "volume_max": VOLUME_MAX,
    })


@app.get("/getvol")
def get_volume_compat():
    return get_volume()


@app.get("/volume/<int(signed=True):value>")
def set_volume(value: int):
    manager.set_volume(value)
    if _wants_json():
        payload = manager.snapshot()
        payload["ok"] = True
        return _json_no_store(payload)
    return redirect(url_for("index"))


@app.get("/vol_up")
def volume_up():
    payload = manager.snapshot()
    payload["volume"] = manager.adjust_volume(1)
    payload["ok"] = True
    return _json_no_store(payload)


@app.get("/vol_down")
def volume_down():
    payload = manager.snapshot()
    payload["volume"] = manager.adjust_volume(-1)
    payload["ok"] = True
    return _json_no_store(payload)


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/healthz")
def healthz():
    return _json_no_store({"ok": True, "state": manager.snapshot()["state"]})


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    LOG.info("ffmpeg: %s", FFMPEG_BIN or "NOT FOUND")
    if not manager.ffmpeg_available:
        LOG.error("ffmpeg was not found. Put ffmpeg.exe in %s or set FFMPEG_BIN.", BASE_DIR)
    LOG.info(
        "audio: s16le %s Hz %s channel(s), prebuffer %s bytes (%.2fs)",
        SAMPLE_RATE,
        CHANNELS,
        PREBUFFER_BYTES,
        PREBUFFER_BYTES / max(1, BYTES_PER_SECOND),
    )
    LOG.info("music sources: %s", ", ".join(s["name"] for s in music_service.enabled_sources))
    for ip in _local_ipv4_addresses():
        LOG.info(
            "control panel: http://%s:%s/   PCM stream: http://%s:%s/radio (alias /stream)",
            ip,
            PORT,
            ip,
            PORT,
        )
    try:
        app.run(host=HOST, port=PORT, threaded=True, use_reloader=False, debug=False)
    finally:
        manager.shutdown()


if __name__ == "__main__":
    main()
