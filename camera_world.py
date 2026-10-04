"""もくもくカメラ: カメラの静止画を mokumoco-world v1 の独自空間種別 "camera" として公開するサンプル。

公開用サーバー(トンネルに渡す)と管理用サーバー(127.0.0.1 だけ)の2つを立て、
撮影ループが interval_sec ごとに ffmpeg で1枚撮ってメモリ上の画像を差し替える。
Python 標準ライブラリと ffmpeg(外部コマンド)だけで動く。
"""
import argparse
import collections
import hashlib
import html
import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
PAUSED_IMAGE = (BASE_DIR / "assets" / "paused.png").read_bytes()
# 版は中身から作る。絵を差し替えたときに、接続側やブラウザが古い絵を使い続けないように
PAUSED_VERSION = "paused-" + hashlib.sha256(PAUSED_IMAGE).hexdigest()[:12]

PROTOCOL = "mokumoco-world"
SPEC_VERSION = "1.0"
DISCOVERY_PATH = "/.well-known/mokumoco-world"
SNAPSHOT_PATH = "/world-api/v1/snapshot"
MESSAGES_MAX = 200  # spec §5: 新しいものから200件まで
JPEG_MAGIC = b"\xff\xd8"
WARMUP_SEC = 2  # カメラを開いた直後は露出が合わず暗いので、少し撮って最後のフレームを使う
FFMPEG_TIMEOUT_SEC = 20
PIXELATE_LEVELS = (1, 2, 4, 8, 16, 32)  # 管理画面のボタンで切り替えるモザイクの段階。1 はモザイク無し

DEFAULT_CONFIG = {
    "title": "もくもくカメラ",
    "port": 8787,
    "admin_port": 8788,
    "interval_sec": 60,
    "size": 640,
    "pixelate": 8,
    "source": "dshow",
    "device": "",
    "ffmpeg": "ffmpeg",
    "shutter_file": "shutter",
}


def load_config():
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return config


def log(text):
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


# ffmpeg の入力指定だけが OS ごとに違う。test はカメラが無い環境(WSL など)での動作確認用
def input_args(config):
    source, device = config["source"], config["device"]
    if source == "dshow":
        return ["-f", "dshow", "-i", f"video={device}"]
    if source == "v4l2":
        return ["-f", "v4l2", "-i", device or "/dev/video0"]
    if source == "test":
        return ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=10"]  # よくあるカメラと同じ16:9
    raise ValueError(f"source が不正です: {source}(dshow / v4l2 / test のどれか)")


# 出す画像の一辺。モザイクをかけるときは、1ブロックを1ピクセルにした小さい画像にする。
# もくもく会チャットはマスの絵をドット絵用の拡大(image-rendering: pixelated)で出すので、
# 見た目は大きい画像にモザイクをかけたものと同じまま、送る量だけが減る
def output_size(config, pixelate):
    return max(1, config["size"] // max(1, int(pixelate)))


# もくもく会チャットのマスは正方形で、絵を縦横いっぱいに引き伸ばして出す。
# なのでカメラの縦横比のまま正方形に収め、余白を黒で埋める
# モザイクありの小さい画像は、色を間引かない JPEG(4:4:4)にする。普通の 4:2:0 だと色が半分の解像度になり、
# 拡大したときに1ドットずつ隣へ色がにじむ。PNG はカメラのノイズで JPEG の数倍に膨らむので使わない
def video_filter(config, pixelate):
    side = output_size(config, pixelate)
    vf = (f"scale={side}:{side}:force_original_aspect_ratio=decrease:flags=area,"
          f"pad={side}:{side}:(ow-iw)/2:(oh-ih)/2:color=black")
    return vf if max(1, int(pixelate)) == 1 else vf + ",format=yuvj444p"


def capture_image(config, pixelate):
    fd, tmp = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        # -update 1 で同じファイルを上書きし続けるので、WARMUP_SEC 後に残るのは最後のフレーム
        cmd = [config["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y",
               *input_args(config), "-t", str(WARMUP_SEC), "-vf", video_filter(config, pixelate),
               "-update", "1", "-q:v", "4" if max(1, int(pixelate)) == 1 else "3",
               # 画像に ffmpeg の版(Lavc...)を書き込ませない。古い版を狙う手がかりを渡さないため
               "-fflags", "+bitexact", "-flags:v", "+bitexact", tmp]
        result = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SEC)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or f"exit {result.returncode}")
        data = Path(tmp).read_bytes()
        if not data.startswith(JPEG_MAGIC):
            raise RuntimeError("ffmpeg の出力が JPEG ではありません")
        return {"data": data, "mime": "image/jpeg", "side": output_size(config, pixelate)}
    finally:
        Path(tmp).unlink(missing_ok=True)


class Camera:
    """撮影した画像・一時停止の状態・チャットのお知らせを持つ。HTTP ハンドラと撮影ループから共有する"""

    def __init__(self, config):
        self.config = config
        self.shutter_path = BASE_DIR / config["shutter_file"]
        self.lock = threading.Lock()
        self.capture_lock = threading.Lock()  # 撮影は同時に1本だけ(自動と手動が重ならないように)
        self.image = None  # {"data", "mime", "side"}
        self.captured_at = None
        self.admin_paused = False
        self.pixelate = max(1, int(config["pixelate"]))  # 管理画面から変えられる。再起動すると設定の値に戻る
        self.next_capture_at = 0.0
        self.messages = collections.deque(maxlen=MESSAGES_MAX)
        self.message_ids = itertools.count(1)
        self.was_paused = self.paused()
        self.post("⏸ 一時停止中です" if self.was_paused else self.start_message())

    def start_message(self):
        return f"📷 配信を開始しました({self.config['interval_sec']}秒ごとに更新)"

    def paused(self):
        return self.admin_paused or self.shutter_path.exists()

    def post(self, text):
        ts = time.time()
        with self.lock:
            self.messages.append({"id": f"sys-{int(ts * 1000)}-{next(self.message_ids)}", "ts": ts,
                                  "author": None, "text": text, "system": True})
        log(text)

    # シャッターファイルは外から置かれるので、変化を見つけたときにお知らせを流す
    def sync_paused(self):
        paused = self.paused()
        if paused == self.was_paused:
            return paused
        self.was_paused = paused
        if paused:
            self.post("⏸ 配信を一時停止しました")
        else:
            self.post("▶ 配信を再開しました")
            self.next_capture_at = 0.0  # 再開したらすぐ撮る
        return paused

    def set_admin_paused(self, paused):
        self.admin_paused = paused
        self.sync_paused()

    # 段階を1つ動かして、すぐ撮り直す(粗くしたときに、細かい画像を公開し続けないように)
    def step_pixelate(self, coarser):
        if coarser:
            candidates = [n for n in PIXELATE_LEVELS if n > self.pixelate]
            self.pixelate = candidates[0] if candidates else self.pixelate
        else:
            candidates = [n for n in PIXELATE_LEVELS if n < self.pixelate]
            self.pixelate = candidates[-1] if candidates else self.pixelate
        log(f"モザイクの粗さを {self.pixelate} にしました")
        self.capture()

    def capture(self):
        with self.capture_lock:
            if self.sync_paused():
                return False
            try:
                image = capture_image(self.config, self.pixelate)
            except Exception as e:  # 失敗しても前の画像を残す
                log(f"撮影に失敗しました: {e}")
                ok = False
            else:
                with self.lock:
                    self.image, self.captured_at = image, time.time()
                ok = True
            # 自動撮影のタイマーは最後に撮った時刻から数え直す(手動更新の直後に自動撮影が重ならない)
            self.next_capture_at = time.time() + self.config["interval_sec"]
            return ok

    def run(self):
        while True:
            if not self.sync_paused() and time.time() >= self.next_capture_at:
                self.capture()
            time.sleep(0.5)

    def public_image(self):
        """公開してよい画像。一時停止中や未撮影のときは None"""
        with self.lock:
            return None if self.paused() else self.image

    def snapshot(self):
        cfg = self.config
        with self.lock:
            paused = self.paused()
            if paused or self.image is None:
                image = {"url": "/paused.png", "version": PAUSED_VERSION, "mime": "image/png"}
            else:
                image = {"url": "/camera", "version": str(int(self.captured_at * 1000)), "mime": self.image["mime"]}
            return {
                "protocol": PROTOCOL,
                "version": SPEC_VERSION,
                "generatedAt": time.time(),
                "space": {
                    # 独自の空間種別。今の接続側は title と image(と messages)だけを使い、残りは無視する(spec §4.1)
                    "type": "camera",
                    "title": cfg["title"],
                    "image": image,
                    "paused": paused,
                    "capturedAt": self.captured_at,
                    "intervalSec": cfg["interval_sec"],
                    "size": [self.image["side"]] * 2 if self.image else None,
                },
                "messages": list(self.messages),
            }


class QuietHandler(BaseHTTPRequestHandler):
    # 何も送らずに居座る接続でスレッドが溜まらないよう、一定時間で切る
    timeout = 10
    security_headers = [("X-Content-Type-Options", "nosniff")]

    def log_message(self, format, *args):
        pass

    # 既定では Server ヘッダーに Python の版が出る。古い版を狙う手がかりを渡さないため名前だけにする
    def version_string(self):
        return "mokumoco-camera"

    def send_body(self, status, body, content_type, extra_headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in [*self.security_headers, *extra_headers]:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data):
        self.send_body(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def not_found(self):
        self.send_body(404, b"not found", "text/plain; charset=utf-8")


def public_handler(camera):
    discovery = {
        "protocol": PROTOCOL,
        "name": camera.config["title"],
        "versions": [{"version": SPEC_VERSION, "snapshot": SNAPSHOT_PATH}],
        "minPollIntervalSec": 5,
        "software": {"name": "mokumoco-world-sample"},
    }

    # 公開用。GET だけで、管理用の機能は一切置かない
    class PublicHandler(QuietHandler):
        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == DISCOVERY_PATH:
                self.send_json(discovery)
            elif path == SNAPSHOT_PATH:
                self.send_json(camera.snapshot())
            elif path == "/paused.png":
                self.send_body(200, PAUSED_IMAGE, "image/png")
            elif path == "/camera" and (image := camera.public_image()) is not None:
                self.send_body(200, image["data"], image["mime"])
            else:
                self.not_found()

    return PublicHandler


ADMIN_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>もくもくカメラ 管理</title>
<style>
body {{ font-family: sans-serif; background: #1d2333; color: #e8e8f0; max-width: 680px; margin: 24px auto; padding: 0 16px; }}
img {{ width: 100%; image-rendering: pixelated; border: 2px solid #3a4460; background: #000; }}
.state {{ font-size: 1.4em; margin: 8px 0; }}
.mosaic {{ margin-top: 16px; }}
form {{ display: inline-block; margin: 8px 8px 0 0; }}
button {{ font-size: 1.1em; padding: 8px 16px; cursor: pointer; }}
button:disabled {{ cursor: not-allowed; opacity: .5; }}
small {{ color: #9aa3bb; }}
</style></head><body>
<h1>{title}</h1>
<div class="state">{state}</div>
<div>最後の撮影: {captured} / <span id="next">{next_text}</span></div>
<div>送っている画像: {image_info}</div>
<img src="/preview?t={stamp}" alt="最後に撮った画像">
<div>
<form method="post" action="/{toggle}"><button>{toggle_label}</button></form>
<form method="post" action="/capture"><button {capture_disabled}>📸 今すぐ更新</button></form>
</div>
<div class="mosaic">モザイク: <b>{mosaic}</b>
<form method="post" action="/mosaic/finer"><button {finer_disabled}>➖ 細かく</button></form>
<form method="post" action="/mosaic/coarser"><button {coarser_disabled}>➕ 粗く</button></form>
</div>
<p><small>シャッターファイル <code>{shutter}</code> を置いている間も一時停止になります。
この画面は 127.0.0.1 でだけ開けます(トンネルには出ません)。</small></p>
<script>
let left = {next_sec};
const el = document.getElementById("next");
if (left !== null) setInterval(() => {{
  left -= 1;
  if (left > 0) el.textContent = "次の自動撮影まで " + left + " 秒";
  else if (left < -3) location.reload();
}}, 1000);
</script>
</body></html>"""


def format_bytes(n):
    if n < 1024:
        return f"{n:.0f}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def admin_handler(camera):
    port = camera.config["admin_port"]
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class AdminHandler(QuietHandler):
        # ほかのサイトに透明な iframe で埋め込まれ、別のボタンに見せかけて「再開」を押させられないようにする
        # (埋め込まれた管理画面からの POST は Origin が自分自身になるので、trusted() では止められない)
        security_headers = [*QuietHandler.security_headers,
                            ("X-Frame-Options", "DENY"),
                            ("Content-Security-Policy", "frame-ancestors 'none'")]

        # 別のサイトや DNS リバインディング経由で、勝手に配信を再開されないようにする
        def trusted(self):
            if self.headers.get("Host") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in allowed_origins

        def do_GET(self):
            if not self.trusted():
                return self.send_body(403, b"forbidden", "text/plain; charset=utf-8")
            path = urllib.parse.urlsplit(self.path).path
            if path == "/":
                self.send_body(200, self.page().encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/preview":
                image = camera.image
                if image is None:
                    self.send_body(200, PAUSED_IMAGE, "image/png")
                else:
                    self.send_body(200, image["data"], image["mime"])
            else:
                self.not_found()

        def do_POST(self):
            if not self.trusted():
                return self.send_body(403, b"forbidden", "text/plain; charset=utf-8")
            path = urllib.parse.urlsplit(self.path).path
            if path == "/pause":
                camera.set_admin_paused(True)
            elif path == "/resume":
                camera.set_admin_paused(False)
            elif path == "/capture":
                camera.capture()  # 撮り終わるまで待ってから画面に戻す
            elif path == "/mosaic/finer":
                camera.step_pixelate(coarser=False)
            elif path == "/mosaic/coarser":
                camera.step_pixelate(coarser=True)
            else:
                return self.not_found()
            self.send_body(303, b"", "text/plain; charset=utf-8", [("Location", "/")])

        def page(self):
            paused = camera.sync_paused()
            if camera.shutter_path.exists():
                state = "⏸ 一時停止中(シャッターファイルあり)"
            elif paused:
                state = "⏸ 一時停止中"
            else:
                state = "🔴 配信中"
            next_sec = None if paused else max(0, round(camera.next_capture_at - time.time()))
            n = camera.pixelate
            image = camera.image
            if image:
                # 目安: 撮るたびに、つないでいるもくもく会1つにつき1回送る
                per_hour = len(image["data"]) * 3600 / camera.config["interval_sec"]
                image_info = (f"{image['side']}x{image['side']} / {format_bytes(len(image['data']))}"
                              f"(もくもく会1つにつき 約 {format_bytes(per_hour)}/時)")
            else:
                image_info = "まだありません"
            captured = time.strftime("%H:%M:%S", time.localtime(camera.captured_at)) if camera.captured_at else "まだありません"
            return ADMIN_PAGE.format(
                title=html.escape(camera.config["title"]),
                state=state,
                captured=captured,
                image_info=image_info,
                next_text="一時停止中は撮影しません" if next_sec is None else f"次の自動撮影まで {next_sec} 秒",
                next_sec="null" if next_sec is None else next_sec,
                stamp=int(time.time()),
                toggle="resume" if camera.admin_paused else "pause",
                toggle_label="▶ 再開" if camera.admin_paused else "⏸ 一時停止",
                capture_disabled="disabled" if paused else "",
                shutter=html.escape(str(camera.shutter_path)),
                mosaic="なし" if n == 1 else f"{n}(一辺 {camera.config['size'] // n} ドット)",
                finer_disabled="disabled" if n <= PIXELATE_LEVELS[0] else "",
                coarser_disabled="disabled" if n >= PIXELATE_LEVELS[-1] else "",
            )

    return AdminHandler


def serve(port, handler):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def list_cameras(config):
    subprocess.run([config["ffmpeg"], "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])


def main():
    parser = argparse.ArgumentParser(description="カメラの静止画を mokumoco-world として公開する")
    parser.add_argument("--list-cameras", action="store_true", help="Windows で使えるカメラの名前を表示する")
    args = parser.parse_args()
    config = load_config()
    if args.list_cameras:
        return list_cameras(config)
    if config["source"] == "dshow" and not config["device"]:
        sys.exit("config.json の device にカメラの名前を書いてください(--list-cameras で一覧が出ます)")

    camera = Camera(config)
    serve(config["port"], public_handler(camera))
    serve(config["admin_port"], admin_handler(camera))
    log(f"公開用: http://127.0.0.1:{config['port']}  (これをトンネルに渡す)")
    log(f"管理用: http://127.0.0.1:{config['admin_port']}  (ブラウザで開く)")
    try:
        camera.run()
    except KeyboardInterrupt:
        log("終了します")


if __name__ == "__main__":
    main()
