# ESP32-S3 局域网音乐播放器

这是一个面向家庭/办公室内网的个人音乐播放器：

```text
手机浏览器搜索歌曲
        |
        v
Windows Flask 后端调用音乐接口，解析可播放地址
        |
        v
ffmpeg 拉取音频并转成 s16le / 44100 Hz / 单声道 PCM
        |
        v
共享环形缓冲 + 每个客户端独立游标
        |
        v
ESP32-S3 从 /radio 读取 PCM -> I2S -> MAX98357A 等功放
电脑负责搜索、解析音源和转码，手机只负责控制，ESP32-S3 负责播放。项目不包含电台/直播频道，也不依赖 ESP32-audioI2S。

功能
GET /search?keyword=xxx：同时搜索酷我和网易云，返回歌名、歌手、专辑、封面、时长和音频地址。

GET/POST /play?url=xxx：停止旧播放、终止旧 ffmpeg、清空旧缓冲，启动新的 ffmpeg。

GET/POST /play_song?...：按音源和歌曲 ID 在服务端重新解析地址，适合网页按钮调用，避免缓存地址过期。

GET /radio / GET /stream：输出原始 s16le PCM 二进制流，不再是 MP3。

网页控制面板：搜索框、歌曲列表、播放按钮、停止、本机试听、复制流地址、音量滑杆。

保留多客户端共享缓冲、独立读游标、预缓冲、卡流检测和 /status 诊断。

音量 0-21：网页写入 Flask，ESP32 每 2 秒读取 /status 后应用软件音量。

新歌切换会立即中断旧 ffmpeg，并让已连接的播放客户端无缝读取新的音频代次。

文件说明
文件	用途
app.py	Flask 服务、ffmpeg 进程、共享 PCM 缓冲、所有 HTTP 接口
music_sources.py	酷我/网易云搜索与播放地址解析，可选酷狗
templates/index.html	手机/电脑控制面板和 Web Audio 本机试听
esp32/esp32_s3_music/esp32_s3_music.ino	ESP32-S3 PCM + I2S 固件
ffmpeg.exe	Windows ffmpeg，当前为 8.1.2
requirements.txt	Python 依赖，只有 Flask
start.bat	Windows 一键启动
allow_firewall.bat	以管理员身份放行 TCP 5000
一、电脑端启动
1. 环境
Windows 10/11。

Python 3.10+，建议 3.12。

电脑、手机、ESP32-S3 在同一局域网；ESP32-S3 只支持 2.4 GHz Wi-Fi。

项目根目录已有 ffmpeg.exe。如果使用其他位置，可设置 FFMPEG_BIN。

安装依赖：

bat
cd /d C:\path\to\project
python -m pip install -r requirements.txt
启动：

bat
start.bat
或者：

bat
python app.py
启动日志会列出本机地址，例如：

text
control panel: http://<电脑当前IPv4>:5000/   PCM stream: http://<电脑当前IPv4>:5000/radio (alias /stream)
手机打开控制面板：

text
http://电脑当前IPv4:5000/
如果电脑 IP 变化，用 ipconfig 查看 Wi-Fi 网卡的 IPv4，并同步修改 ESP32 固件里的 SERVER_HOST。

2. Windows 防火墙
首次运行 Python 时允许“专用网络”入站访问。如果手机或 ESP32 连不上，右键 allow_firewall.bat，选择“以管理员身份运行”。等价命令：

bat
netsh advfirewall firewall add rule name="ESP32 LAN Music 5000" dir=in action=allow protocol=TCP localport=5000 profile=any
二、使用流程
手机打开控制面板。

输入歌名、歌手或专辑，例如“青花瓷”。

选择“自动选择音源”或指定酷我/网易云，点击“搜索”。

点击歌曲右侧“播放”。后端解析地址并启动 ffmpeg。

/status 显示 state=playing、streaming=true 后，ESP32 会自动连接 /radio 并播放。

点击“本机试听”可以在电脑/手机浏览器里检查同一路 PCM。

点击“停止播放”会终止 ffmpeg、关闭 PCM 缓冲，ESP32 在下次状态轮询后停止并静音。

搜索结果为“不可用”的歌曲通常是音源没有返回可播放直链，换一首或切换音源即可。

三、音频格式与缓冲
固定输出：

text
容器：无（raw PCM）
编码：pcm_s16le
采样率：44100 Hz
声道：1（单声道）
每采样：16 bit
码率：705 kbps（88200 字节/秒）
共享缓冲由 app.py 内的 SharedStreamBuffer 管理：

ffmpeg 只写一份 PCM，所有 /radio 客户端共享。

每个客户端有自己的 offset，慢客户端不会阻塞其他客户端。

新客户端会从最近 RADIO_PREBUFFER_BYTES 字节处开始，减少首次连接卡顿。

新歌播放时缓冲清空并递增 generation；已经连接的客户端会跳到新流，不需要重新连接。

超过 RADIO_BUFFER_MAX_BYTES 时丢弃最旧数据，/status 的 buffer_dropped_bytes 会增长。

四、HTTP API
所有 JSON 接口默认关闭缓存。调试时可加 ?format=json 让重定向接口返回 JSON。

方法	路径	说明
GET	/	网页控制面板
GET	/search?keyword=青花瓷&source=auto&limit=20	搜索歌曲
POST/GET	/play?url=音频地址	直接播放传入的音频 URL
POST/GET	/play_song?source=kuwo&song_id=MUSIC_xxx	服务端按歌曲 ID 重新解析并播放
GET	/sources	已启用音源
GET	/stop?format=json	停止播放并终止 ffmpeg
GET	/radio	原始 PCM 流（ESP32 使用）
GET	/stream	与 /radio 等价
GET	/status	当前歌曲、状态、缓冲、音量、错误诊断
GET	/volume / /getvol	读取音量
GET	/volume/0..21	设置音量
GET	/vol_up / /vol_down	音量加/减 1
GET	/healthz	轻量健康检查
搜索
bash
curl "http://127.0.0.1:5000/search?keyword=%E9%9D%92%E8%8A%B1%E7%93%B7&limit=3"
关键返回字段：

json
{
  "ok": true,
  "keyword": "青花瓷",
  "songs": [
    {
      "key": "kuwo:MUSIC_154706365",
      "source": "kuwo",
      "source_name": "酷我音乐",
      "song_id": "MUSIC_154706365",
      "title": "青花瓷（DJ版）",
      "artist": "DJRE",
      "album": "全网热播中文DJ舞曲",
      "duration": 149,
      "cover": "",
      "url": "https://.../xxx.mp3",
      "playable": true,
      "referer": "https://www.kuwo.cn/"
    }
  ],
  "errors": [],
  "resolve_errors": 0,
  "took_ms": 484
}
点播
网页优先调用 /play_song，因为它会在点击时重新解析地址：

bash
curl -X POST "http://127.0.0.1:5000/play_song" \
  -H "Content-Type: application/json" \
  -d '{"source":"kuwo","song_id":"MUSIC_154706365","title":"青花瓷（DJ版）","artist":"DJRE"}'
也可以直接调用：

bash
curl -X POST "http://127.0.0.1:5000/play?url=https%3A%2F%2Fexample.com%2Fsong.mp3&title=测试&artist=未知"
/play 只接受 http/https，默认允许公网音乐 CDN，但拒绝 localhost、局域网和私有 IP，避免 Flask 被当作任意内网请求代理。确需播放局域网文件时设置 RADIO_ALLOW_PRIVATE_URL=1。

状态和诊断
bash
curl "http://127.0.0.1:5000/status"
常用字段：

state：stopped / starting / buffering / playing / error。

playing、streaming：是否已选择歌曲、ffmpeg 是否持续输出数据。

title、artist、album、song_source_name：当前歌曲。

listeners：当前 /radio 客户端数。

buffer_bytes、buffer_max_bytes、prebuffer_bytes、buffer_dropped_bytes：缓冲情况。

volume、volume_max：ESP32 音量。

last_error、last_exit_code、ffmpeg_bin：ffmpeg 故障诊断。

五、环境变量
Windows CMD 示例：set RADIO_PREBUFFER_BYTES=220500 后再运行 python app.py。PowerShell 使用 $env:RADIO_PREBUFFER_BYTES="220500"。

变量	默认值	说明
RADIO_SAMPLE_RATE	44100	PCM 采样率；改动后 ESP32 固件也要同步
RADIO_CHANNELS	1	PCM 声道数；推荐保持 1
RADIO_PREBUFFER_BYTES	176400	预缓冲字节数，默认 2 秒；约 88200 字节/秒
RADIO_BUFFER_MAX_BYTES	4194304	共享缓冲上限，默认 4 MB
RADIO_STALL_TIMEOUT	30	超过多少秒无数据后停止 ffmpeg
RADIO_VOLUME	15	启动初始音量，0-21
RADIO_HOST	0.0.0.0	Flask 监听地址
RADIO_PORT	5000	Flask 端口；改动后固件也要同步
RADIO_ALLOW_ANY_URL	1	是否允许任意公网 URL；设为 0 后只允许白名单
RADIO_URL_ALLOWLIST	空	追加允许的域名，逗号分隔
RADIO_ALLOW_PRIVATE_URL	0	是否允许 localhost/私有 IP
MUSIC_SOURCES	kuwo,netease	启用的音源，可加 kugou
MUSIC_SOURCE_TIMEOUT	12	单个音乐接口超时秒数
MUSIC_USER_AGENT	Chrome UA	音乐接口请求 UA
FFMPEG_BIN	自动查找	指定 ffmpeg 路径
例如希望缓冲约 2.5 秒：

bat
set RADIO_PREBUFFER_BYTES=220500
python app.py
如果担心本机被当作公网抓取代理，可这样缩小范围：

bat
set RADIO_ALLOW_ANY_URL=0
set RADIO_URL_ALLOWLIST=kuwo.cn,126.net,163.com
python app.py
六、ESP32-S3 固件
固件路径：

text
esp32/esp32_s3_music/esp32_s3_music.ino
1. Arduino 环境
安装 Arduino IDE 2.x。

开发板管理器安装 esp32 by Espressif Systems，使用 3.x 版本（本项目按 3.3.11 编译）。

不需要安装 ESP32-audioI2S，固件直接使用 ESP-IDF 新版 I2S 驱动。

打开 esp32_s3_music.ino。

开发板建议选择 ESP32S3 Dev Module。如果板子是 N16R8，可在 Tools -> PSRAM 选择 OPI PSRAM；本项目不依赖 PSRAM 也能运行。Flash/PSRAM 选项按实际硬件选择。

2. 修改配置
在文件开头修改：

cpp
static const char *WIFI_SSID = "你的Wi-Fi名称";
static const char *WIFI_PASSWORD = "你的Wi-Fi密码";
static const char *SERVER_HOST = "<电脑当前IPv4>";  // 改成电脑当前 IPv4
static const uint16_t SERVER_PORT = 5000;
SERVER_HOST 必须和 Flask 启动日志/ipconfig 一致。电脑重新连 Wi-Fi 后地址可能变化。

3. 接线
ESP32-S3	MAX98357A / I2S DAC
GPIO 17	BCLK / BCK
GPIO 16	LRC / LRCK / WS
GPIO 15	DIN / DOUT
GND	GND
5V 或 3.3V	VIN，按功放板要求供电
MAX98357A 的 SD/MODE 按模块说明配置，不要把它当普通 GPIO 使能脚随意接 GND。常见模块默认输出 (L+R)/2；固件把单声道样本复制到左右声道，避免音量被平均后减半。

4. 固件行为
固定 I2S：i2s_std 标准驱动、I2S_NUM_1、44100 Hz、32-bit slot（16-bit 样本左移 16 位）、立体声帧。

服务端是单声道 PCM；固件把每个采样复制成 L/R 两声道。

每 2 秒请求一次 /status，同步 playing 和 volume；音量用 -36 dB 到 0 dB 曲线在固件内做软件缩放。

/radio 支持 HTTP chunked 和 close-delimited 两种传输；固件自己解析 chunk，不依赖音频库。

新歌切换不需要 ESP32 重连：服务端保持同一 HTTP 流，内部切换到新的 PCM 代次。

服务端停止后，固件断开流并把 I2S 输出清零；再次点播后自动重连。

Wi-Fi 断开、服务端重启、歌曲结束导致断流时，固件会自动重试。

烧录后打开串口监视器，波特率 115200，正常应能看到：

text
ESP32-S3 LAN Music Player
I2S ready: I2S_NUM_1 (i2s_std), 44100 Hz, 32-bit slots / 16-bit audio, mono duplicated L/R, BCLK=17 LRC=16 DIN=15
WiFi connected, IP: 192.168.x.x
Server: playing
PCM connected (chunked)
PCM format: s16le;rate=44100;channels=1
Volume: 15 (Q15 scale 10038)
七、常见问题
搜索没有结果或提示音源失败
音乐接口来自公开网络，可能限流、改参数或临时失效。先切换 auto/酷我/网易云重试；失败详情会写进 /search 返回的 errors 和 Flask 日志。

搜索结果有歌但显示“不可用”
该音源没有返回可播放直链，或返回的地址属于 VIP/版权限制。换一首或换音源，不要反复点击同一首。

/radio 返回 503
503 表示当前没有播放任务。先在网页点击一首歌，确认 /status 的 playing=true。

网页显示 playing，但 ESP32 没声音
用 curl -D - --max-time 5 http://电脑IP:5000/radio -o NUL 检查 PC 本机流是否正常。

手机/电脑打开 http://电脑IP:5000/status，确认 streaming=true。

检查 ESP32 串口是否出现 PCM connected。

先在串口发送 t：测试音不依赖 Wi-Fi、Flask 和歌曲，能区分“网络问题”还是“I2S/功放问题”。

测试音也无声时，用万用表确认 MAX98357A 的 SD/MODE 电压大于 0.16V（建议直接接 3.3V）；接 GND 或悬空会静音。

检查 ESP32 与功放是否共地、VIN 是否 5V、喇叭是否接在功放 +/- 两端。

核对模块丝印：GPIO17→BCLK/BCK，GPIO16→LRC/WS，GPIO15→DIN/DOUT，不要按板子排列顺序猜。

确认固件里的 SERVER_HOST 是电脑当前 IPv4，不是旧地址。

检查 Windows 防火墙是否放行 TCP 5000。

ESP32 反复重连或卡顿
确认 ESP32 连接的是 2.4 GHz Wi-Fi，电脑和路由器没有开启客户端隔离。

调整 RADIO_PREBUFFER_BYTES：网络抖动时增大到 220500（2.5 秒），首次连接慢时降到 132300（1.5 秒）。

查看 /status 的 buffer_dropped_bytes、last_error 和 last_exit_code。

关闭电脑休眠、VPN 和会接管网卡的代理软件。

歌曲播到一半失败
部分音乐 CDN 的直链有时效或限流。使用网页的播放按钮走 /play_song，让服务端在点击时重新解析；直接保存的 /play?url=... 旧地址可能已经过期。

ffmpeg 找不到
确保 ffmpeg.exe 在项目根目录，或设置：

bat
set FFMPEG_BIN=C:\path\to\ffmpeg.exe
八、安全与版权
本项目只用于可信内网，不要直接映射到公网，也不要在公网服务器上开放 /play。

面板没有账号体系；能访问 5000 端口的局域网设备都可以搜索、切歌和改音量。

/play 默认阻止 localhost、私有 IP 和链路本地地址，降低 SSRF 风险；如果不需要公网任意地址，建议设置 RADIO_ALLOW_ANY_URL=0 和域名白名单。

音乐接口、音频地址和版权归各音源及原权利方所有。本项目仅用于个人学习与家庭内网播放，请勿公开转播或商业使用。

九、实现参考
音源适配思路参考了这些公开项目，但本项目使用 Python 标准库自行实现，运行时不需要 Node.js：

MusicFree

xiaozhi-mcp-music

xiaozhi-music-railway

公开接口随时可能变化；如果某个音源长期不可用，应在 music_sources.py 中更新或禁用对应适配器。
