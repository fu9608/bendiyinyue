"""内网音乐播放器的音乐源适配器。

默认提供两个稳定音源：

- 酷我音乐：``search.kuwo.cn`` 搜索，``antiserver.kuwo.cn`` 解析 MP3 直链；
- 网易云音乐：``music.163.com/api/search/get/web`` 搜索，外链地址解析 MP3。

酷狗作为可选实验音源保留。接口方案参考了 xiaozhi-mcp-music（MIT）以及
LX Music / MusicFree 社区里常见的公开接口实现。这里只用 Python 标准库，
不额外引入 HTTP 依赖。
"""

from __future__ import annotations

import ast
import copy
import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any


LOG = logging.getLogger("music")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


USER_AGENT = os.environ.get(
    "MUSIC_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
).strip()
SOURCE_TIMEOUT = max(3.0, _env_float("MUSIC_SOURCE_TIMEOUT", 12.0))
MAX_SEARCH_LIMIT = 30
DEFAULT_SOURCES = "kuwo,netease"


class SourceError(Exception):
    """单个音源请求失败。"""


def clean_text(value: Any) -> str:
    """清理音源返回文本里的转义、HTML 实体和高亮标签。"""

    if value is None:
        return ""
    text = str(value)
    text = text.replace("\\u0026", "&").replace("\\&", "&")
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _clean_escapes(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean_escapes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_escapes(item) for item in value]
    if isinstance(value, str):
        return clean_text(value)
    return value


def _header(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    try:
        value = headers.get(name)
        if value is not None:
            return str(value)
    except Exception:
        pass
    wanted = name.lower()
    try:
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return str(value)
    except Exception:
        pass
    return ""


def _request(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    timeout: float | None = None,
    max_bytes: int = 4 * 1024 * 1024,
) -> tuple[int, str, Any, bytes]:
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}{'&' if '?' in url else '?'}{query}"

    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    request_headers.update(headers or {})
    request_obj = urllib.request.Request(url, headers=request_headers, method=method)

    try:
        with urllib.request.urlopen(request_obj, timeout=timeout or SOURCE_TIMEOUT) as response:
            body = response.read(max_bytes) if max_bytes > 0 else b""
            return response.status, response.geturl(), response.headers, body
    except urllib.error.HTTPError as exc:
        detail = exc.read(512)
        raise SourceError(f"HTTP {exc.code}: {detail[:160]!r}") from exc
    except Exception as exc:
        raise SourceError(str(exc)) from exc


def _is_http_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(html.unescape(url.strip()))
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _song(
    *,
    source: str,
    source_name: str,
    song_id: str,
    title: str,
    artist: str,
    album: str = "",
    duration: int = 0,
    cover: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    title = clean_text(title)
    song_id = str(song_id or "").strip()
    return {
        "key": f"{source}:{song_id}",
        "source": source,
        "source_name": source_name,
        "song_id": song_id,
        "title": title,
        "artist": clean_text(artist) or "未知歌手",
        "album": clean_text(album),
        "duration": max(0, int(duration or 0)),
        "cover": clean_text(cover),
        "extra": extra or {},
    }


class TTLCache:
    def __init__(self, ttl: float, max_entries: int = 256) -> None:
        self._ttl = max(1.0, float(ttl))
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if now >= expires_at:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            if len(self._data) >= self._max_entries:
                # 简单淘汰：删除最早写入的一批，避免无限增长。
                for old_key in list(self._data)[: max(1, self._max_entries // 8)]:
                    self._data.pop(old_key, None)
            self._data[key] = (time.monotonic() + self._ttl, value)


class BaseSource:
    name = ""
    display_name = ""
    referer = ""

    def search(self, keyword: str, limit: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    def resolve(self, song: dict[str, Any]) -> str:
        raise NotImplementedError


class KuwoSource(BaseSource):
    name = "kuwo"
    display_name = "酷我音乐"
    referer = "https://www.kuwo.cn/"
    search_url = "https://search.kuwo.cn/r.s"
    play_url = "https://antiserver.kuwo.cn/anti.s"

    def search(self, keyword: str, limit: int) -> list[dict[str, Any]]:
        _, _, _, body = _request(
            self.search_url,
            params={
                "all": keyword,
                "ft": "music",
                "itemset": "web_2013",
                "client": "kt",
                "pn": "0",
                "rn": str(limit),
                "rformat": "json",
                "encoding": "utf8",
            },
            headers={"Referer": self.referer},
        )
        text = body.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except Exception:
            try:
                data = ast.literal_eval(text)
            except Exception as exc:
                raise SourceError(f"酷我搜索返回无法解析：{exc}") from exc

        data = _clean_escapes(data)
        items = (data or {}).get("abslist") or []
        songs: list[dict[str, Any]] = []
        for item in items:
            rid = str(item.get("MUSICRID") or item.get("musicrid") or "").strip()
            title = item.get("NAME") or item.get("SONGTITLE") or item.get("SONGNAME") or ""
            if not rid or not clean_text(title):
                continue
            try:
                duration = int(item.get("DURATION") or 0)
            except (TypeError, ValueError):
                duration = 0
            songs.append(
                _song(
                    source=self.name,
                    source_name=self.display_name,
                    song_id=rid,
                    title=title,
                    artist=item.get("ARTIST") or item.get("artist") or "未知歌手",
                    album=item.get("ALBUM") or item.get("album") or "",
                    duration=duration,
                    cover=item.get("PICPATH")
                    or item.get("web_albumpic_short")
                    or item.get("WEB_ALBUM_PIC")
                    or "",
                )
            )
        return songs

    def resolve(self, song: dict[str, Any]) -> str:
        rid = str(song.get("song_id") or "").strip()
        if not rid:
            raise SourceError("酷我歌曲 id 为空")
        for audio_format in ("mp3", "aac"):
            _, _, _, body = _request(
                self.play_url,
                params={
                    "type": "convert_url3",
                    "rid": rid,
                    "format": audio_format,
                    "response": "url",
                },
                headers={"Referer": self.referer},
            )
            text = body.decode("utf-8", "replace").strip()
            url = ""
            if text.startswith("{"):
                try:
                    url = str(json.loads(text).get("url") or "").strip()
                except Exception:
                    url = ""
            else:
                url = text
            url = html.unescape(url)
            if _is_http_url(url) and url.lower() not in ("null", "none", "error"):
                return url
        raise SourceError("酷我未返回播放地址（可能为付费或下架歌曲）")


class NeteaseSource(BaseSource):
    name = "netease"
    display_name = "网易云音乐"
    referer = "https://music.163.com/"
    search_url = "https://music.163.com/api/search/get/web"

    def search(self, keyword: str, limit: int) -> list[dict[str, Any]]:
        _, _, _, body = _request(
            self.search_url,
            params={
                "csrf_token": "",
                "s": keyword,
                "type": "1",
                "offset": "0",
                "limit": str(limit),
            },
            headers={"Referer": self.referer, "Accept": "application/json, text/plain, */*"},
        )
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception as exc:
            raise SourceError(f"网易云搜索返回无法解析：{exc}") from exc
        if data.get("code") not in (200, None):
            raise SourceError(f"网易云搜索返回异常 code={data.get('code')}")

        songs: list[dict[str, Any]] = []
        for item in ((data.get("result") or {}).get("songs") or []):
            sid = str(item.get("id") or "").strip()
            title = item.get("name") or ""
            if not sid or not clean_text(title):
                continue
            artists = item.get("artists") or item.get("ar") or []
            artist = " / ".join(
                clean_text(artist_item.get("name", ""))
                for artist_item in artists
                if artist_item.get("name")
            ) or "未知歌手"
            album_info = item.get("album") or {}
            if not isinstance(album_info, dict):
                album_info = {}
            duration = item.get("duration") or item.get("dt") or 0
            try:
                duration = int(duration)
                if duration > 1000:
                    duration //= 1000
            except (TypeError, ValueError):
                duration = 0
            songs.append(
                _song(
                    source=self.name,
                    source_name=self.display_name,
                    song_id=sid,
                    title=title,
                    artist=artist,
                    album=album_info.get("name", ""),
                    duration=duration,
                    cover=album_info.get("picUrl") or item.get("picUrl") or "",
                )
            )
        return songs

    def resolve(self, song: dict[str, Any]) -> str:
        sid = str(song.get("song_id") or "").strip()
        if not sid:
            raise SourceError("网易云歌曲 id 为空")
        outer_url = (
            "https://music.163.com/song/media/outer/url?id="
            + urllib.parse.quote(sid, safe="")
            + ".mp3"
        )

        final_url = ""
        try:
            status, final_url, headers, _ = _request(
                outer_url,
                headers={"Referer": self.referer},
                method="HEAD",
                max_bytes=0,
            )
            content_type = _header(headers, "Content-Type").lower()
            content_length = _header(headers, "Content-Length")
            too_small = False
            if content_length:
                try:
                    too_small = int(content_length) < 50_000
                except ValueError:
                    too_small = False
            if status != 200 or content_type.startswith("text/html") or too_small:
                final_url = ""
        except SourceError:
            final_url = ""

        if not final_url:
            # 少数 CDN/节点不支持 HEAD，用 Range GET 再确认一次。
            try:
                status, final_url, headers, body = _request(
                    outer_url,
                    headers={"Referer": self.referer, "Range": "bytes=0-1023"},
                    max_bytes=1024,
                )
                content_type = _header(headers, "Content-Type").lower()
                if status not in (200, 206) or content_type.startswith("text/html") or not body:
                    final_url = ""
            except SourceError:
                final_url = ""

        if _is_http_url(final_url):
            return html.unescape(final_url)
        raise SourceError("网易云该歌曲不可用（可能为 VIP 或已下架）")


class KugouSource(BaseSource):
    """实验性音源。版权歌曲经常不返回可用直链，默认不启用。"""

    name = "kugou"
    display_name = "酷狗音乐"
    referer = "https://www.kugou.com/"
    search_url = "https://songsearch.kugou.com/song_search_v2"

    def search(self, keyword: str, limit: int) -> list[dict[str, Any]]:
        _, _, _, body = _request(
            self.search_url,
            params={
                "keyword": keyword,
                "page": "1",
                "pagesize": str(limit),
                "userid": "-1",
                "clientver": "2000",
                "platform": "WebFilter",
                "tag": "em",
                "filter": "2",
                "iscorrection": "1",
                "privilege_filter": "0",
            },
            headers={"Referer": self.referer},
        )
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception as exc:
            raise SourceError(f"酷狗搜索返回无法解析：{exc}") from exc

        songs: list[dict[str, Any]] = []
        for item in (((data or {}).get("data") or {}).get("lists") or []):
            file_hash = str(item.get("FileHash") or "").strip()
            title = item.get("SongName") or ""
            if not file_hash or not clean_text(title):
                continue
            try:
                duration = int(item.get("Duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            songs.append(
                _song(
                    source=self.name,
                    source_name=self.display_name,
                    song_id=file_hash,
                    title=title,
                    artist=item.get("SingerName") or "未知歌手",
                    album=item.get("AlbumName") or "",
                    duration=duration,
                    cover=item.get("ImgUrl") or "",
                    extra={"album_id": item.get("AlbumID") or item.get("AlbumId") or ""},
                )
            )
        return songs

    def resolve(self, song: dict[str, Any]) -> str:
        file_hash = str(song.get("song_id") or "").strip()
        if not file_hash:
            raise SourceError("酷狗歌曲 hash 为空")
        _, _, _, body = _request(
            "https://m.kugou.com/app/i/getSongInfo.php",
            params={"cmd": "playInfo", "hash": file_hash},
            headers={"Referer": "https://m.kugou.com/"},
        )
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception as exc:
            raise SourceError(f"酷狗播放地址解析失败：{exc}") from exc
        url = str((data or {}).get("url") or "").strip()
        if _is_http_url(url):
            return url

        album_id = str((song.get("extra") or {}).get("album_id") or "")
        _, _, _, body = _request(
            "https://wwwapi.kugou.com/yy/index.php",
            params={"r": "play/getdata", "hash": file_hash, "album_id": album_id, "mid": "1"},
            headers={"Referer": "https://www.kugou.com/", "Cookie": "kg_mid=1"},
        )
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            data = {}
        url = str(((data or {}).get("data") or {}).get("play_url") or "").strip()
        if _is_http_url(url):
            return url
        raise SourceError("酷狗未返回播放地址（版权歌曲通常不可用）")


SOURCE_CLASSES: dict[str, type[BaseSource]] = {
    "kuwo": KuwoSource,
    "netease": NeteaseSource,
    "kugou": KugouSource,
}


class MusicService:
    def __init__(self) -> None:
        requested = [
            item.strip().lower()
            for item in os.environ.get("MUSIC_SOURCES", DEFAULT_SOURCES).split(",")
            if item.strip()
        ]
        self._sources: dict[str, BaseSource] = {}
        for name in requested:
            source_class = SOURCE_CLASSES.get(name)
            if source_class is None:
                LOG.warning("忽略未知音乐源：%s", name)
                continue
            self._sources[name] = source_class()
        if not self._sources:
            LOG.warning("MUSIC_SOURCES 没有可用音源，回退到 %s", DEFAULT_SOURCES)
            for name in DEFAULT_SOURCES.split(","):
                self._sources[name] = SOURCE_CLASSES[name]()
        self._search_cache = TTLCache(ttl=120, max_entries=128)
        self._url_cache = TTLCache(ttl=240, max_entries=512)

    @property
    def enabled_sources(self) -> list[dict[str, str]]:
        return [
            {"name": name, "display_name": source.display_name}
            for name, source in self._sources.items()
        ]

    def referer_for(self, source: str) -> str:
        source_impl = self._sources.get(clean_text(source).lower())
        return source_impl.referer if source_impl else ""

    def display_name(self, source: str) -> str:
        source_impl = self._sources.get(clean_text(source).lower())
        return source_impl.display_name if source_impl else ""

    def search(
        self,
        keyword: str,
        limit: int = 12,
        source: str = "auto",
    ) -> dict[str, Any]:
        keyword = clean_text(keyword)
        if not keyword:
            raise ValueError("keyword 不能为空")
        if len(keyword) > 80:
            raise ValueError("keyword 过长")
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 12
        limit = max(1, min(MAX_SEARCH_LIMIT, limit))
        source = clean_text(source or "auto").lower() or "auto"
        if source not in ("auto", "all") and source not in self._sources:
            raise ValueError(f"音源 {source} 未启用")

        cache_key = (keyword.lower(), limit, source)
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

        started = time.monotonic()
        names = list(self._sources) if source in ("auto", "all") else [source]
        per_source = max(1, (limit + len(names) - 1) // len(names))
        results: dict[str, list[dict[str, Any]]] = {}
        errors: list[dict[str, str]] = []

        with ThreadPoolExecutor(max_workers=min(4, len(names))) as pool:
            futures = {
                pool.submit(self._sources[name].search, keyword, per_source): name
                for name in names
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as exc:
                    LOG.warning("音源 %s 搜索失败：%s", name, exc)
                    errors.append({"source": name, "error": str(exc)[:300]})

        # 轮流取各音源结果，避免第一个音源占满整个列表。
        songs: list[dict[str, Any]] = []
        indices = {name: 0 for name in names}
        seen: set[str] = set()
        while len(songs) < limit:
            progressed = False
            for name in names:
                items = results.get(name, [])
                index = indices[name]
                if index >= len(items):
                    continue
                progressed = True
                indices[name] = index + 1
                item = items[index]
                if item["key"] in seen:
                    continue
                seen.add(item["key"])
                songs.append(item)
                if len(songs) >= limit:
                    break
            if not progressed:
                break

        resolve_errors = 0
        if songs:
            with ThreadPoolExecutor(max_workers=min(6, max(1, len(songs)))) as pool:
                futures = {pool.submit(self._resolve_song, item): item for item in songs}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        item["url"] = future.result()
                    except Exception as exc:
                        item["url"] = ""
                        resolve_errors += 1
                        LOG.info("解析 %s 失败：%s", item["key"], exc)
                    item["playable"] = bool(item["url"])
                    item["referer"] = self._sources[item["source"]].referer

        payload = {
            "ok": True,
            "keyword": keyword,
            "sources": names,
            "songs": songs,
            "errors": errors,
            "resolve_errors": resolve_errors,
            "took_ms": int((time.monotonic() - started) * 1000),
        }
        self._search_cache.set(cache_key, copy.deepcopy(payload))
        return payload

    def resolve_by_id(
        self,
        source: str,
        song_id: str,
        album_id: str = "",
    ) -> str:
        source = clean_text(source).lower()
        source_impl = self._sources.get(source)
        if source_impl is None:
            raise ValueError(f"音源 {source} 未启用")
        item = _song(
            source=source,
            source_name=source_impl.display_name,
            song_id=song_id,
            title="",
            artist="",
            extra={"album_id": album_id},
        )
        return self._resolve_song(item)

    def _resolve_song(self, song: dict[str, Any]) -> str:
        source = song["source"]
        source_impl = self._sources.get(source)
        if source_impl is None:
            raise SourceError(f"音源 {source} 不可用")
        album_id = str((song.get("extra") or {}).get("album_id") or "")
        cache_key = (source, song.get("song_id", ""), album_id)
        cached = self._url_cache.get(cache_key)
        if cached:
            return str(cached)
        url = source_impl.resolve(song)
        url = html.unescape(str(url or "").strip())
        if not _is_http_url(url):
            raise SourceError("音源返回了非法的播放地址")
        self._url_cache.set(cache_key, url)
        return url


music_service = MusicService()
