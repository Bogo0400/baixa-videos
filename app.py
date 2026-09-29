"""Baixa — downloader pessoal de vídeos (YouTube, Instagram, TikTok, Facebook).

Roda só na sua máquina: http://127.0.0.1:5055
"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import yt_dlp
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
TMP_DIR = DOWNLOADS_DIR / ".tmp"
STATIC_DIR = BASE_DIR / "static"
COOKIES_FILE = BASE_DIR / "cookies.txt"
HOST, PORT = "127.0.0.1", int(os.environ.get("BAIXA_PORT", "5055"))

PLATFORMS = {
    "youtube": re.compile(r"(^|\.)(youtube\.com|youtu\.be)$"),
    "instagram": re.compile(r"(^|\.)instagram\.com$"),
    "tiktok": re.compile(r"(^|\.)tiktok\.com$"),
    "facebook": re.compile(r"(^|\.)(facebook\.com|fb\.watch|fb\.com)$"),
}

MODES = {"1080", "720", "audio", "thumb"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

app = Flask(__name__, static_folder=None)

jobs: dict[str, dict] = {}
thumb_cache: dict[str, str] = {}
lock = threading.Lock()


# ---------------------------------------------------------------- helpers

def detect_platform(url: str) -> str | None:
    m = re.match(r"^https?://([^/?#:]+)", url.strip(), re.I)
    if not m:
        return None
    host = m.group(1).lower()
    for name, pattern in PLATFORMS.items():
        if pattern.search(host):
            return name
    return None


def base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "playlist_items": "1",
        "windowsfilenames": True,
        "js_runtimes": {"node": {}, "deno": {}},
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
    }
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        opts["ffmpeg_location"] = str(Path(ffmpeg).parent)
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def first_entry(info: dict) -> dict:
    """Posts com várias mídias (carrossel) viram playlist; pegamos a primeira."""
    while info and info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise yt_dlp.utils.DownloadError("Nenhum vídeo encontrado neste link.")
        info = entries[0]
    return info


def short_res(w, h) -> int | None:
    """1080p = menor lado (vale para vídeos verticais também)."""
    dims = [d for d in (w, h) if isinstance(d, (int, float)) and d > 0]
    return int(min(dims)) if dims else None


def best_thumbnail(info: dict) -> str | None:
    thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
    if thumbs:
        def score(t):
            return (t.get("preference") or 0, (t.get("width") or 0) * (t.get("height") or 0))
        return max(thumbs, key=score)["url"]
    return info.get("thumbnail")


def friendly_error(exc: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
    low = msg.lower()
    if "unsupported url" in low:
        return "Esse link não parece ser de um vídeo. Confira se copiou o endereço do vídeo, não do perfil."
    if "ip address is blocked" in low or "not available in your country" in low or "geo" in low:
        return "Esse vídeo está bloqueado para a sua região (o dono restringiu o acesso por país)."
    if "private" in low:
        return "Esse vídeo é privado. Só dá para baixar vídeos públicos."
    if any(k in low for k in ("login", "sign in", "cookies", "rate-limit", "rate limit", "not available")):
        return ("A plataforma pediu login para liberar esse vídeo. Coloque um arquivo cookies.txt na pasta "
                "do projeto (veja o LEIAME) e tente de novo.")
    if "unavailable" in low or "removed" in low or "404" in low:
        return "Esse vídeo não está disponível — pode ter sido apagado ou bloqueado na sua região."
    if "ffmpeg" in low:
        return "O ffmpeg não foi encontrado. Ele é necessário para juntar vídeo e áudio."
    if any(k in low for k in ("timed out", "getaddrinfo", "connection", "network")):
        return "Sem conexão com a plataforma. Verifique sua internet e tente de novo."
    return "Não foi possível processar esse link. Detalhe técnico: " + msg.replace("ERROR: ", "")[:240]


def unique_path(folder: Path, name: str) -> Path:
    target = folder / name
    stem, suffix = target.stem, target.suffix
    n = 2
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


def clean_title(title: str) -> str:
    title = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title or "video")
    title = re.sub(r"\s+", " ", title).strip(" .")
    return (title[:90].rstrip(" .") or "video")


def probe_codecs(path: Path) -> tuple[str | None, str | None]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None, None
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    ).stdout
    video = audio = None
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2:
            name, kind = parts[0], parts[1]
            if kind == "video" and not video:
                video = name
            elif kind == "audio" and not audio:
                audio = name
    return video, audio


def ensure_h264(path: Path, duration, update) -> Path:
    """Algumas plataformas só entregam VP9/AV1 em alta resolução. Convertemos para
    H.264 + AAC para o arquivo abrir em qualquer player, celular ou editor (Premiere etc.)."""
    video, audio = probe_codecs(path)
    if not video or video == "h264":
        return path
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return path
    target = path.with_name(path.stem + ".h264.mp4")
    audio_args = ["-c:a", "copy"] if audio == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           *audio_args, "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(target)]
    update(stage="processing", label="Convertendo para H.264 (compatível)", percent=0, speed=None, eta=None)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            creationflags=NO_WINDOW)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and duration:
            try:
                secs = int(line.split("=", 1)[1]) / 1_000_000
                update(percent=round(min(secs / duration, 1.0) * 100, 1))
            except ValueError:
                pass
    if proc.wait() != 0 or not target.exists():
        target.unlink(missing_ok=True)
        return path  # mantém o original se a conversão falhar
    path.unlink(missing_ok=True)
    return target


# ---------------------------------------------------------------- download job

def run_job(job_id: str, url: str, mode: str):
    job = jobs[job_id]
    work = TMP_DIR / job_id
    work.mkdir(parents=True, exist_ok=True)
    # progresso de cada arquivo baixado (vídeo e áudio vêm separados em alta resolução)
    state = {"parts": 1, "fractions": {}}

    def update(**kw):
        with lock:
            job.update(kw)

    def overall_percent():
        fr = state["fractions"]
        parts = max(state["parts"], len(fr), 1)
        return round(min(sum(fr.values()) / parts, 1.0) * 100, 1)

    def progress_hook(d):
        info = d.get("info_dict") or {}
        key = d.get("filename") or d.get("tmpfilename") or "file"
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                frac = min(d.get("downloaded_bytes", 0) / total, 1.0)
            elif d.get("fragment_count"):
                frac = (d.get("fragment_index") or 0) / d["fragment_count"]
            else:
                frac = 0.0
            state["fractions"][key] = frac
            is_audio = mode == "audio" or (state["parts"] > 1 and info.get("vcodec") == "none")
            update(stage="downloading", label="Baixando áudio" if is_audio else "Baixando vídeo",
                   percent=overall_percent(), speed=d.get("speed"), eta=d.get("eta"))
        elif d["status"] == "finished":
            state["fractions"][key] = 1.0
            update(percent=overall_percent())

    pp_labels = {
        "Merger": "Juntando vídeo e áudio",
        "ExtractAudio": "Convertendo para MP3",
        "ThumbnailsConvertor": "Convertendo capa para JPG",
        "Metadata": "Gravando informações",
        "FixupM3u8": "Ajustando formato",
        "FixupM4a": "Ajustando formato",
        "FixupDuplicateMoov": "Ajustando formato",
    }

    def pp_hook(d):
        if d["status"] == "started":
            update(stage="processing", label=pp_labels.get(d.get("postprocessor"), "Finalizando"),
                   speed=None, eta=None)

    opts = base_opts()
    opts.update({
        "outtmpl": str(work / "%(id)s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
    })

    if mode in ("1080", "720"):
        opts.update({
            "format": "bv*+ba/b",
            # maior resolução até o limite, preferindo H.264 + AAC (abre em qualquer player)
            "format_sort": [f"res:{mode}", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
            "merge_output_format": "mp4",
        })
    elif mode == "audio":
        opts.update({
            "format": "ba/b",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                {"key": "FFmpegMetadata", "add_metadata": True},
            ],
        })
    elif mode == "thumb":
        opts.update({
            "skip_download": True,
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            ],
        })

    try:
        update(stage="starting", label="Preparando", percent=0)
        for attempt in range(3):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    # 1º lê as informações (e escolhe o formato), 2º baixa — assim sabemos
                    # quantas partes serão baixadas e a barra de progresso fica correta.
                    info = first_entry(ydl.extract_info(url, download=False))
                    state["parts"] = len(info.get("requested_formats") or []) or 1
                    ydl.process_info(info)
                break
            except yt_dlp.utils.DownloadError as exc:
                # o YouTube às vezes recusa um link de download (403) — pedimos um novo e tentamos de novo
                if "403" not in str(exc) or attempt == 2:
                    raise
                shutil.rmtree(work, ignore_errors=True)
                work.mkdir(parents=True, exist_ok=True)
                state["fractions"].clear()
                update(stage="starting", label="Tentando de novo", percent=0, speed=None, eta=None)
                time.sleep(2)

        files = [p for p in work.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if mode == "thumb":
            files = [p for p in files if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
        if not files:
            raise RuntimeError("O arquivo final não foi gerado.")
        produced = max(files, key=lambda p: p.stat().st_size)
        if mode in ("1080", "720"):
            produced = ensure_h264(produced, info.get("duration"), update)

        title = clean_title(info.get("title") or info.get("id"))
        res = short_res(info.get("width"), info.get("height"))
        if mode in ("1080", "720"):
            tag = f" [{res}p]" if res else ""
        elif mode == "thumb":
            tag = " [capa]"
        else:
            tag = ""
        final = unique_path(DOWNLOADS_DIR, f"{title}{tag}{produced.suffix.lower()}")
        shutil.move(str(produced), final)

        update(stage="done", label="Pronto", percent=100, speed=None, eta=None,
               filename=final.name, size=final.stat().st_size, resolution=res)
    except Exception as exc:  # noqa: BLE001
        update(stage="error", label="Erro", error=friendly_error(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- routes

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC_DIR, name)


@app.get("/api/health")
def health():
    return jsonify(ok=True, ytdlp=yt_dlp.version.__version__, ffmpeg=bool(shutil.which("ffmpeg")),
                   node=bool(shutil.which("node")), cookies=COOKIES_FILE.exists())


@app.post("/api/info")
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    platform = detect_platform(url)
    if not platform:
        return jsonify(error="Cole um link do YouTube, Instagram, TikTok ou Facebook."), 400
    opts = base_opts()
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = first_entry(ydl.extract_info(url, download=False))
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=friendly_error(exc)), 422

    formats = data.get("formats") or []
    video_res = [short_res(f.get("width"), f.get("height")) for f in formats
                 if f.get("vcodec") not in (None, "none") or f.get("width")]
    video_res = [r for r in video_res if r]
    has_video = bool(video_res) or any(f.get("vcodec") not in (None, "none") for f in formats) \
        or (not formats and data.get("ext") not in ("mp3", "m4a"))
    max_res = max(video_res) if video_res else short_res(data.get("width"), data.get("height"))

    thumb = best_thumbnail(data)
    token = None
    if thumb:
        token = uuid.uuid4().hex[:12]
        thumb_cache[token] = thumb

    return jsonify(
        platform=platform,
        url=data.get("webpage_url") or url,
        title=data.get("title") or data.get("id"),
        uploader=data.get("uploader") or data.get("channel") or data.get("uploader_id"),
        duration=data.get("duration"),
        thumb=f"/api/thumb/{token}" if token else None,
        has_video=has_video,
        max_res=max_res,
    )


@app.get("/api/thumb/<token>")
def thumb(token):
    src = thumb_cache.get(token)
    if not src:
        abort(404)
    try:
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            resp = ydl.urlopen(src)
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "image/jpeg")
    except Exception:  # noqa: BLE001
        abort(502)
    return Response(body, mimetype=ctype, headers={"Cache-Control": "max-age=3600"})


@app.post("/api/download")
def download():
    payload = request.get_json(silent=True) or {}
    url, mode = payload.get("url", "").strip(), str(payload.get("mode", ""))
    if not detect_platform(url):
        return jsonify(error="Cole um link do YouTube, Instagram, TikTok ou Facebook."), 400
    if mode not in MODES:
        return jsonify(error="Formato inválido."), 400
    job_id = uuid.uuid4().hex[:12]
    with lock:
        jobs[job_id] = {"id": job_id, "mode": mode, "stage": "queued", "label": "Na fila",
                        "percent": 0, "created": time.time()}
    threading.Thread(target=run_job, args=(job_id, url, mode), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/job/<job_id>")
def job_status(job_id):
    with lock:
        job = dict(jobs.get(job_id) or {})
    if not job:
        abort(404)
    return jsonify(job)


@app.get("/api/job/<job_id>/file")
def job_file(job_id):
    job = jobs.get(job_id)
    if not job or job.get("stage") != "done":
        abort(404)
    return send_file(DOWNLOADS_DIR / job["filename"], as_attachment=True, download_name=job["filename"])


@app.get("/api/files")
def list_files():
    items = []
    for p in DOWNLOADS_DIR.iterdir():
        if p.is_file() and not p.name.startswith("."):
            st = p.stat()
            items.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify(items[:30])


@app.get("/api/files/<path:name>")
def get_file(name):
    return send_from_directory(DOWNLOADS_DIR, name, as_attachment=True)


def _explorer_windows() -> dict[int, str]:
    """Janelas do Explorador de Arquivos abertas: {hwnd: título}."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value == "CabinetWClass" and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(260)
            user32.GetWindowTextW(hwnd, title, 260)
            found[hwnd] = title.value
        return True

    user32.EnumWindows(cb, 0)
    return found


def _bring_to_front(hwnd: int):
    """O Windows bloqueia programas em segundo plano de trazer janelas para a frente;
    simular a tecla Alt libera a troca de foco (truque padrão da API do Windows)."""
    import ctypes
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)                 # SW_RESTORE (se estiver minimizada)
    user32.keybd_event(0x12, 0, 0, 0)          # Alt pressionado
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 0x0002, 0)     # Alt solto


@app.post("/api/open-folder")
def open_folder():
    if sys.platform != "win32":
        return jsonify(error="Abrir a pasta só funciona no Windows.", path=str(DOWNLOADS_DIR)), 501
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    try:
        before = set(_explorer_windows())
        subprocess.Popen(["explorer", str(DOWNLOADS_DIR)])
        target = None
        for _ in range(30):  # espera até 3 s a janela nova aparecer
            time.sleep(0.1)
            new = [h for h in _explorer_windows() if h not in before]
            if new:
                target = new[0]
                break
        if target is None:  # o Explorer reaproveitou uma janela já aberta da pasta
            target = next((h for h, t in _explorer_windows().items() if t == DOWNLOADS_DIR.name), None)
        if target:
            _bring_to_front(target)
    except Exception:  # noqa: BLE001
        return jsonify(error="Não consegui abrir a pasta.", path=str(DOWNLOADS_DIR)), 500
    return jsonify(ok=True, path=str(DOWNLOADS_DIR))


# ---------------------------------------------------------------- start

def already_running() -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


def main():
    url = f"http://{HOST}:{PORT}"
    if already_running():
        # clicou duas vezes no iniciar.bat: só abre o navegador na janela que já está rodando
        print(f"Baixa já está aberto em {url}")
        webbrowser.open(url)
        return
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    if not shutil.which("ffmpeg"):
        print("AVISO: ffmpeg não encontrado no PATH — 1080p e MP3 não vão funcionar.")
    print(f"\n  Baixa está rodando em {url}\n  Feche esta janela para desligar.\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # janela limpa: sem o banner e os logs técnicos do Flask
    import logging
    import flask.cli
    flask.cli.show_server_banner = lambda *a, **k: None
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
