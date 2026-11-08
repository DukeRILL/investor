"""Монтаж на FFmpeg: движение камеры по фото, переходы, субтитры ASS по реальному звуку."""
import json, re, shutil, subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS, SS = 1080, 1920, 30, 2  # SS: кадр считается вдвое крупнее и сжимается, чтобы движение не дёргалось
XFADE = 0.35
GAP = 0.12        # пауза между репликами
TAIL = 0.7        # финальная пауза
MIN_TOTAL = 25.0  # минимальная длина ролика, с
MAX_PAUSE = 0.3   # более длинные паузы внутри реплики вырезаем: голос Azure иногда дописывает секунды тишины
FONT_NAME = "Montserrat ExtraBold"
FONT_FILE = "/usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf"
TRANSITIONS = ["smoothleft", "fade", "smoothup", "fadeblack", "smoothright"]
ACCENT = "&H0040D6FF&"   # жёлтый в формате ASS (BBGGRR)
TEXTLESS_KINDS = {"chart"}


# ---------- звук ----------
def run(*cmd):
    subprocess.run(list(cmd), check=True, capture_output=True)


def probe(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def token(w: str) -> str:
    return re.sub(r"[^\w]", "", w.lower().replace("ё", "е"))


def _sim(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b, autojunk=False).ratio()


def align(script_words: list[str], heard: list[tuple[str, float, float]], dur: float) -> tuple[list[tuple[str, float, float]], float]:
    """Привязывает слова сценария к услышанным словам с их временем.

    Что распознать не удалось (числа прописью, имена), получает время между соседними привязанными словами.
    Второе значение: доля слов сценария, которые реально услышаны.
    """
    n, m = len(script_words), len(heard)
    toks = [token(w) for w in script_words]
    htoks = [token(h[0]) for h in heard]
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            s = _sim(toks[i], htoks[j])
            take = score[i + 1][j + 1] + (s if s >= 0.6 else -0.5)
            score[i][j] = max(take, score[i + 1][j], score[i][j + 1])
    anchors: dict[int, tuple[float, float]] = {}
    i = j = 0
    while i < n and j < m:
        s = _sim(toks[i], htoks[j])
        if s >= 0.6 and abs(score[i][j] - (score[i + 1][j + 1] + s)) < 1e-9:
            anchors[i] = (heard[j][1], heard[j][2])
            i, j = i + 1, j + 1
        elif score[i][j] == score[i + 1][j]:
            i += 1
        else:
            j += 1
    end_speech = heard[-1][2] if heard else dur
    out: list[tuple[str, float, float]] = []
    i = 0
    while i < n:
        if i in anchors:
            out.append((script_words[i], *anchors[i]))
            i += 1
            continue
        k = i
        while k < n and k not in anchors:
            k += 1
        t0 = out[-1][2] if out else 0.0
        t1 = anchors[k][0] if k < n else end_speech
        t1 = max(t1, t0 + 0.08 * (k - i))
        weights = [max(1, len(toks[x])) + 1 for x in range(i, k)]
        tot, t = sum(weights), t0
        for x, wt in zip(range(i, k), weights):
            d = (t1 - t0) * wt / tot
            out.append((script_words[x], t, t + d))
            t += d
        i = k
    return out, (len(anchors) / n if n else 0.0)


def _heard_score(script: list[str], heard: list[str]) -> float:
    """Сколько слов сценария реально прозвучало по порядку, минус лишние слова. Числа и имена не штрафуются."""
    n, m = len(script), len(heard)
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            hit = best[i + 1][j + 1] + 1 if _sim(script[i], heard[j]) >= 0.65 else -1e9
            best[i][j] = max(hit, best[i + 1][j], best[i][j + 1])
    matched = best[0][0]
    extra = sum(0 if re.search(r"\d", h) else 1 for h in heard) - matched
    return matched - 0.5 * max(0.0, extra)


def select_islands(script: list[str], islands: list[list[str]]) -> list[int]:
    """Выбирает куски озвучки, которые дают сценарий без повторов и бессвязной речи.

    Голос Azure MAI иногда говорит лишнее: тарабарщину, повтор фразы после секунд тишины. Кусков мало, перебор дешёвый.
    """
    toks = [token(w) for w in script]
    isl = [[token(w) for w in x] for x in islands]
    keep_all = [i for i in range(len(isl)) if isl[i]]
    if len(keep_all) > 12:
        return keep_all
    best, best_score = [], float("-inf")
    for mask in range(1, 1 << len(keep_all)):
        chosen = [keep_all[k] for k in range(len(keep_all)) if mask >> k & 1]
        sc = _heard_score(toks, [w for i in chosen for w in isl[i]])
        if sc > best_score + 1e-9:
            best, best_score = chosen, sc
    return best


LAT = dict(zip("abcdefghijklmnopqrstuvwxyz", ["а", "б", "к", "д", "е", "ф", "г", "х", "и", "дж", "к", "л", "м", "н", "о", "п", "к", "р", "с", "т", "у", "в", "в", "кс", "й", "з"]))


def translit(tok: str) -> str:
    """Грубо латиница -> кириллица, чтобы Lucid сравнивался с услышанным «Лусит»."""
    return "".join(LAT.get(ch, ch) for ch in tok)


def add_neighbours(chosen: list[int], spans: list[tuple[int, int]], heard: list[list[str]], rate: int, script: list[str] | None = None,
                   max_gap: float = 0.6) -> list[int]:
    """Возвращает короткие куски рядом с выбранными: так не теряются имена вроде Lucid Motors, которые модель слышит иначе.

    Повтор и тарабарщина идут после долгой тишины и сюда не попадают; кусок, повторяющий уже выбранные слова, тоже не берём.
    """
    keep = set(chosen)
    changed = True
    while changed:
        changed = False
        said = {token(w) for i in keep for w in heard[i]}
        for i in range(len(spans)):
            if i in keep or not heard[i] or len(heard[i]) > 4:
                continue
            near = (i + 1 in keep and spans[i + 1][0] - spans[i][1] <= max_gap * rate) or (i - 1 in keep and spans[i][0] - spans[i - 1][1] <= max_gap * rate)
            missing = [translit(token(w)) for w in (script or []) if token(w) not in said]
            looks_like_missing = any(_sim(translit(token(h)), m) >= 0.5 for h in heard[i] for m in missing)
            if near and looks_like_missing and not any(token(w) in said for w in heard[i]):
                keep.add(i)
                changed = True
    return sorted(keep)


def find_islands(samples, rate: int, floor_db: float = -38.0, min_gap: float = 0.25, min_len: float = 0.12) -> list[tuple[int, int]]:
    """Куски речи между паузами, в отсчётах."""
    import numpy as np
    if not len(samples):
        return []
    hop = max(1, int(rate * 0.01))
    n = len(samples) // hop
    if not n:
        return []
    rms = np.sqrt(np.mean(samples[:n * hop].reshape(n, hop).astype(np.float64) ** 2, axis=1))
    idx = np.flatnonzero(rms > rms.max() * 10 ** (floor_db / 20))
    out: list[list[int]] = []
    for i in idx:
        if out and i - out[-1][1] <= min_gap * 100:
            out[-1][1] = int(i) + 1
        else:
            out.append([int(i), int(i) + 1])
    return [(a * hop, b * hop) for a, b in out if (b - a) >= min_len * 100]


def _write_wav(path: Path, data, rate: int) -> None:
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def clean_voice(src: Path, dst: Path, script: str, tempo: float = 1.0) -> tuple[float, float]:
    """Оставляет только реплику из сценария. Звук только вырезается, синтез не повторяется.

    Возвращает длину реплики и сколько секунд исходной записи выброшено.
    """
    import numpy as np
    rate = 44100
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
                         capture_output=True, check=True).stdout
    data = np.frombuffer(pcm, np.int16)
    spans = find_islands(data, rate)
    piece = dst.with_suffix(".piece.wav")
    heard = []
    for a, b in spans:
        _write_wav(piece, data[a:b], rate)
        heard.append([w for w, _, _ in transcribe(piece)])
    piece.unlink(missing_ok=True)
    chosen = add_neighbours(select_islands(script.split(), heard), spans, heard, rate, script.split())
    pause = np.zeros(int(rate * MAX_PAUSE * 0.7), np.int16)
    parts = []
    for i in chosen:
        if parts:
            parts.append(pause)
        parts.append(data[spans[i][0]:spans[i][1]])
    clean = np.concatenate(parts) if parts else data[:0]
    raw = dst.with_suffix(".raw.wav")
    _write_wav(raw, clean, rate)
    if abs(tempo - 1) > 1e-3:
        run("ffmpeg", "-y", "-v", "error", "-i", str(raw), "-af", f"atempo={tempo:.4f}", "-ar", str(rate), "-ac", "1", str(dst))
        raw.unlink()
    else:
        raw.replace(dst)
    return probe(dst), round((len(data) - len(clean)) / rate, 2)


_model = None


def transcribe(path: Path, hint: str = "") -> list[tuple[str, float, float]]:
    """Слова с временем из настоящего звука. Бесплатно: локальная модель, сеть и API не нужны."""
    global _model
    import numpy as np
    from faster_whisper import WhisperModel
    if _model is None:
        import os
        _model = WhisperModel("small", device="cpu", compute_type="int8", download_root=os.environ.get("MODELS_DIR", "/data/models"))
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    segs, _ = _model.transcribe(audio, language="ru", word_timestamps=True, vad_filter=False, initial_prompt=hint or None)
    return [(w.word.strip(), float(w.start), float(w.end)) for s in segs for w in s.words]


# ---------- движение камеры ----------
def union_safe(faces: list[list[int]], iw: int, ih: int) -> tuple[float, float, float, float] | None:
    """Прямоугольник вокруг лиц с запасом на волосы и подбородок: его нельзя резать."""
    if not faces:
        return None
    x0 = min(f[0] - 0.35 * f[2] for f in faces)
    x1 = max(f[0] + 1.35 * f[2] for f in faces)
    y0 = min(f[1] - 0.55 * f[3] for f in faces)
    y1 = max(f[1] + 1.35 * f[3] for f in faces)
    return max(0.0, x0), max(0.0, y0), min(float(iw), x1), min(float(ih), y1)


def max_window(iw: int, ih: int) -> tuple[float, float]:
    return (ih * W / H, float(ih)) if iw * H >= ih * W else (float(iw), iw * H / W)


def clamp_window(cx: float, cy: float, w: float, h: float, iw: int, ih: int) -> tuple[float, float, float, float]:
    x = min(max(0.0, cx - w / 2), iw - w)
    y = min(max(0.0, cy - h / 2), ih - h)
    return x, y, w, h


def contains(win: tuple[float, float, float, float], box: tuple[float, float, float, float] | None) -> bool:
    if box is None:
        return True
    x, y, w, h = win
    e = 0.5
    return x - e <= box[0] and y - e <= box[1] and x + w + e >= box[2] and y + h + e >= box[3]


def plan_motion(item: dict, effect: str) -> dict:
    """Выбирает раскладку и окна камеры так, чтобы лица оставались целыми в начале и конце движения."""
    iw, ih = item["width"], item["height"]
    faces = item.get("faces") or []
    safe = union_safe(faces, iw, ih)
    mw, mh = max_window(iw, ih)
    wide_faces = safe is not None and safe[2] - safe[0] > mw
    if not faces and item.get("kind") != "generated" or effect == "parallax" or (wide_faces and effect != "face_sweep"):
        # графики, слайды и картинки с текстом целиком, снимок с двумя людьми так, чтобы вошли оба
        return {"layout": "contain", "safe": safe, "effect": "parallax" if effect not in ("zoom_in", "zoom_out") else effect}
    if safe is None:
        safe = (iw * .3, ih * .3, iw * .7, ih * .7)
    cx, cy = (safe[0] + safe[2]) / 2, (safe[1] + safe[3]) / 2
    if effect == "face_sweep" and len(faces) >= 2:
        a, b = sorted(faces)[0], sorted(faces)[-1]
        sa, sb = union_safe([a], iw, ih), union_safe([b], iw, ih)
        if sa[2] - sa[0] <= mw and sb[2] - sb[0] <= mw:
            ya, yb = (sa[1] + sa[3]) / 2, (sb[1] + sb[3]) / 2
            return {"layout": "cover", "effect": "face_sweep", "safe": sa, "safe_end": sb,
                    "start": clamp_window((sa[0] + sa[2]) / 2, ya, mw, mh, iw, ih),
                    "end": clamp_window((sb[0] + sb[2]) / 2, yb, mw, mh, iw, ih)}
        effect = "zoom_in"
    if wide_faces:
        return {"layout": "contain", "safe": safe, "effect": "zoom_in"}
    if effect in ("pan_left", "pan_right"):
        # камера идёт по снимку, лица в окне от начала до конца
        lo = max(0.0, safe[2] - mw)
        hi = min(iw - mw, safe[0])
        y = clamp_window(cx, cy, mw, mh, iw, ih)[1]
        if hi - lo >= mw * 0.08:
            a, b = (hi, lo) if effect == "pan_left" else (lo, hi)
            return {"layout": "cover", "effect": effect, "safe": safe, "start": (a, y, mw, mh), "end": (b, y, mw, mh)}
        effect = "zoom_in"
    # наезд или отъезд: внешнее окно максимальное, внутреннее ещё вмещает лица
    outer = clamp_window(cx, cy, mw, mh, iw, ih)
    scale = max(0.72, (safe[2] - safe[0]) / mw * 1.05, (safe[3] - safe[1]) / mh * 1.05)
    scale = min(scale, 0.92)
    inner = clamp_window(cx, cy, mw * scale, mh * scale, iw, ih)
    if not contains(inner, safe):
        inner = outer
    start, end = (outer, inner) if effect != "zoom_out" else (inner, outer)
    return {"layout": "cover", "effect": effect if effect in ("zoom_in", "zoom_out") else "zoom_in", "safe": safe, "start": start, "end": end}


EASE = "(PP*PP*(3-2*PP))"


def _eased(frames: int, var: str) -> str:
    return EASE.replace("PP", f"({var}/{max(1, frames - 1)})")


def render_cover(src: Path, plan: dict, frames: int, out: Path) -> None:
    (sx, sy, sw, sh), (ex, ey, ew, eh) = plan["start"], plan["end"]
    img = Image.open(src).convert("RGB")
    if abs(sw - ew) < 1e-6:  # чистая панорама: окно не меняет размер
        k = H * SS / sh
        big = out.with_suffix(".png")
        img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS).save(big, compress_level=1)
        e = _eased(frames, "n")
        vf = (f"loop=loop={frames}:size=1:start=0,format=rgb24,"
              f"crop={W * SS}:{H * SS}:x='{sx * k:.2f}+({(ex - sx) * k:.2f})*{e}':y='{sy * k:.2f}+({(ey - sy) * k:.2f})*{e}':exact=1,"
              f"scale={W}:{H}:flags=lanczos,format=yuv420p")
        run("ffmpeg", "-y", "-v", "error", "-i", str(big), "-vf", vf, "-frames:v", str(frames), "-r", str(FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", str(out))
        big.unlink()
        return
    # наезд: режем общее окно и двигаем камеру внутри него через zoompan
    gx0, gy0 = min(sx, ex), min(sy, ey)
    gw = max(sx + sw, ex + ew) - gx0
    gh = gw * H / W
    region = img.crop((round(gx0), round(gy0), round(gx0 + gw), round(gy0 + gh))).resize((W * SS, H * SS), Image.LANCZOS)
    png = out.with_suffix(".png")
    region.save(png, compress_level=1)
    zs, ze = gw / sw, gw / ew
    cxs, cys = (sx + sw / 2 - gx0) / gw, (sy + sh / 2 - gy0) / gh
    cxe, cye = (ex + ew / 2 - gx0) / gw, (ey + eh / 2 - gy0) / gh
    e = _eased(frames, "on")
    vf = (f"zoompan=z='{zs:.5f}+({ze - zs:.5f})*{e}':"
          f"x='iw*({cxs:.5f}+({cxe - cxs:.5f})*{e})-iw/zoom/2':y='ih*({cys:.5f}+({cye - cys:.5f})*{e})-ih/zoom/2':"
          f"d={frames}:s={W * SS}x{H * SS}:fps={FPS},scale={W}:{H}:flags=lanczos,format=yuv420p")
    run("ffmpeg", "-y", "-v", "error", "-i", str(png), "-vf", vf, "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", str(out))
    png.unlink()


def build_layers(src: Path, item: dict, plan: dict, tmp: Path, stem: str) -> tuple[Path, Path]:
    """Два настоящих слоя: размытый фон и снимок целиком с мягкими краями. Лица и текст снимка не режутся."""
    img = Image.open(src).convert("RGB")
    cw, ch = W * SS, H * SS
    safe = plan.get("safe")
    if safe:  # люди: масштаб такой, чтобы все лица вошли по ширине
        k = min((cw * 0.94) / (safe[2] - safe[0]), ch * 0.62 / img.height)
        k = max(k, cw / img.width)
        fx, fy = (safe[0] + safe[2]) / 2, (safe[1] + safe[3]) / 2
        px = cw / 2 - fx * k
        px = min(0.0, max(cw - img.width * k, px))
        py = ch * 0.36 - fy * k
    else:     # графики и картинки с текстом целиком
        k = cw * 0.9 / img.width
        px = (cw - img.width * k) / 2
        py = ch * 0.3 - img.height * k / 2
    fw, fh = round(img.width * k), round(img.height * k)
    fg_img = img.resize((fw, fh), Image.LANCZOS)
    alpha = Image.new("L", (fw, fh), 255)
    d = ImageDraw.Draw(alpha)
    feather = int(min(120, fh * 0.08))
    for y in range(feather):
        v = int(255 * y / feather)
        d.line([(0, y), (fw, y)], fill=v)
        d.line([(0, fh - 1 - y), (fw, fh - 1 - y)], fill=v)
    fg = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    fg_img.putalpha(alpha)
    fg.paste(fg_img, (round(px), round(py)), fg_img)
    bw, bh = W + 140, H + 140
    side = max(bw / img.width, bh / img.height)
    bg = img.resize((round(img.width * side) + 2, round(img.height * side) + 2), Image.BILINEAR)
    bg = bg.crop((0, 0, bw, bh)).filter(ImageFilter.GaussianBlur(45))
    bg = Image.blend(bg, Image.new("RGB", bg.size, (10, 10, 18)), 0.6)
    fgp, bgp = tmp / f"{stem}_fg.png", tmp / f"{stem}_bg.png"
    fg.save(fgp, compress_level=1)
    bg.save(bgp, compress_level=1)
    return fgp, bgp


def render_contain(src: Path, item: dict, plan: dict, frames: int, out: Path, tmp: Path, flip: bool) -> None:
    fgp, bgp = build_layers(src, item, plan, tmp, out.stem)
    e_on, e_n = _eased(frames, "on"), _eased(frames, "n")
    z0, z1 = (1.09, 1.0) if plan["effect"] == "zoom_out" else (1.0, 1.09)
    dx = (130 if flip else 10, 10 if flip else 130)
    fc = (f"[0:v]loop=loop={frames}:size=1:start=0,crop={W}:{H}:x='{dx[0]}+({dx[1] - dx[0]})*{e_n}':y=70:exact=1[bg];"
          f"[1:v]format=rgba,zoompan=z='{z0}+({z1 - z0:.4f})*{e_on}':x='iw/2-iw/zoom/2':y='ih*0.36-ih*0.36/zoom':"
          f"d={frames}:s={W * SS}x{H * SS}:fps={FPS},scale={W}:{H}:flags=lanczos[fg];"
          f"[bg][fg]overlay=0:0:format=auto,format=yuv420p[v]")
    run("ffmpeg", "-y", "-v", "error", "-i", str(bgp), "-i", str(fgp), "-filter_complex", fc, "-map", "[v]",
        "-frames:v", str(frames), "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", str(out))
    fgp.unlink()
    bgp.unlink()


# ---------- текст ----------
def ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def esc(s: str) -> str:
    return s.replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ")


def chunk_words(words: list[tuple[str, float, float]], maxw: int = 960, size: int = 88, max_words: int = 4) -> list[list[tuple[str, float, float]]]:
    try:
        f = ImageFont.truetype(FONT_FILE, size)
        width = f.getlength
    except OSError:
        width = lambda s: len(s) * size * 0.62
    chunks, cur = [], []
    for it in words:
        trial = " ".join(w for w, _, _ in cur + [it])
        if cur and (len(cur) >= max_words or width(trial) > maxw or cur[-1][0][-1] in ".!?"):
            chunks.append(cur)
            cur = []
        cur.append(it)
    if cur:
        chunks.append(cur)
    return chunks


def fmt_count(v: float, target: float) -> str:
    dec = 0 if float(target).is_integer() else len(f"{target:g}".split(".")[1])
    s = f"{v:,.{dec}f}".replace(",", " ")
    return s.replace(".", ",")


def num_size(txt: str, maxw: int = 980) -> int:
    """Самый крупный размер, при котором число входит в кадр."""
    try:
        return min(190, int(190 * maxw / ImageFont.truetype(FONT_FILE, 190).getlength(txt)))
    except OSError:
        return 130


def build_ass(timeline: list[dict], total: float) -> str:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{FONT_NAME},88,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,8,2,5,50,50,0,204
Style: Key,{FONT_NAME},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,6,2,5,70,70,0,204
Style: Num,{FONT_NAME},170,{ACCENT},{ACCENT},&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,9,3,5,40,40,0,204
Style: Cta,{FONT_NAME},62,{ACCENT},{ACCENT},&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,6,2,5,70,70,0,204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev: list[str] = []
    for sc in timeline:
        t0, t1 = sc["start"], sc["end"]
        cta = sc.get("is_cta")
        # субтитры: активное слово подсвечено ровно на время его звучания
        chunks = chunk_words(sc["words"])
        for ci, ch in enumerate(chunks):
            next_start = chunks[ci + 1][0][1] + t0 if ci + 1 < len(chunks) else t1
            for wi, (_, a, b) in enumerate(ch):
                start = t0 + a
                end = (t0 + ch[wi + 1][1]) if wi + 1 < len(ch) else min(next_start, t0 + b + 0.45)
                if end - start < 0.01:
                    continue
                parts = []
                for wj, (w, _, _) in enumerate(ch):
                    parts.append(("{\\c" + ACCENT + "}" + esc(w) + "{\\c&H00FFFFFF&}") if wj == wi else esc(w))
                ev.append(f"Dialogue: 2,{ts(start)},{ts(end)},Sub,,0,0,0,,{{\\pos({W // 2},1560)}}" + " ".join(parts))
        # экранный текст и крупное число, виден с первого кадра сцены
        key = (sc.get("on_screen") or "").strip()
        bn = sc.get("big_number")
        if key and not cta:
            y = 1300 if not bn else 1340
            ev.append(f"Dialogue: 1,{ts(t0)},{ts(t1)},Key,,0,0,0,,{{\\pos({W // 2},{y})\\fscx82\\fscy82\\t(0,180,\\fscx100\\fscy100)}}" + esc(key.upper()))
        if cta:
            ev.append(f"Dialogue: 1,{ts(t0)},{ts(total)},Key,,0,0,0,,{{\\pos({W // 2},1110)\\fad(150,0)}}" + esc(key))
            ev.append(f"Dialogue: 1,{ts(t0 + 0.15)},{ts(total)},Cta,,0,0,0,,{{\\pos({W // 2},1270)\\fad(150,0)}}Школа единорогов")
            ev.append(f"Dialogue: 1,{ts(t0 + 0.3)},{ts(total)},Cta,,0,0,0,,{{\\pos({W // 2},1370)\\fad(150,0)\\fs48\\c&H00FFFFFF&}}@investor_duker")
        if bn:
            try:
                v = float(bn["value"])
            except (KeyError, TypeError, ValueError):
                continue
            pre, suf = bn.get("prefix", "") or "", bn.get("suffix", "") or ""
            hook = t0 < 0.01  # в хуке цифра верная с первого кадра, счётчик только в остальных сценах
            steps = 0 if hook else 12
            for i in range(steps + 1):
                a = t0 + i * 0.45 / max(1, steps)
                b = t0 + (i + 1) * 0.45 / steps if i < steps else t1
                p = 0.4 + 0.6 * (1 - (1 - i / max(1, steps)) ** 3)  # счёт с 40 %, иначе мелькает «$0 млрд»
                txt = f"{pre}{fmt_count(v * p if i < steps else v, v)}{suf}"
                size = num_size(txt)
                pop = "\\fscx85\\fscy85\\t(0,140,\\fscx100\\fscy100)" if hook else ""
                ev.append(f"Dialogue: 1,{ts(a)},{ts(b)},Num,,0,0,0,,{{\\pos({W // 2},1120)\\fs{size}{pop}}}" + esc(txt))
    return head + "\n".join(ev) + "\n"


def dark_overlay(path: Path) -> None:
    """Затемнение снизу, чтобы текст читался на любом снимке, лица остаются светлыми."""
    grad = Image.new("L", (1, H))
    for y in range(H):
        v = 0
        if y > 850:
            v = int(205 * min(1, (y - 850) / 420))
        grad.putpixel((0, y), v)
    rgba = Image.new("RGBA", (W, H), (6, 6, 12, 0))
    rgba.putalpha(grad.resize((W, H)))
    rgba.save(path)


def verify_sync(mp4: Path, manifest: dict) -> dict:
    """Приёмка: заново слушает готовый MP4 и сравнивает начало каждого слова с субтитрами."""
    subs = [w for s in manifest["scenes"] for w in s["words"]]
    heard = transcribe(mp4)
    toks, h = [token(w[0]) for w in subs], [(token(x[0]), x[1]) for x in heard]
    diffs, j = [], 0
    for tok, w in zip(toks, subs):
        for k in range(j, min(j + 6, len(h))):
            if _sim(tok, h[k][0]) >= 0.8:
                diffs.append(abs(w[1] - h[k][1]))
                j = k + 1
                break
    if not diffs:
        return {"checked_words": 0}
    diffs.sort()
    return {"checked_words": len(diffs), "total_words": len(subs), "heard_words": len(heard), "median_ms": int(diffs[len(diffs) // 2] * 1000),
            "p95_ms": int(diffs[int(len(diffs) * .95)] * 1000), "max_ms": int(diffs[-1] * 1000),
            "repeat_suspected": len(heard) > len(subs) * 1.15}


# ---------- сборка ----------
def render(scenes: list[dict], audio_files: list[Path], images: dict[str, dict], out: Path, keep_tmp: bool = False) -> dict:
    """Собирает один ролик. Время берётся только из реального звука, никаких оценок по длине текста."""
    tmp = out.parent / (out.stem + "_tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    clean = [tmp / f"a{i}.wav" for i in range(len(scenes))]
    res = [clean_voice(a, c, sc["voice"]) for a, c, sc in zip(audio_files, clean, scenes)]
    durs = [r[0] for r in res]
    dropped = [r[1] for r in res]
    total_voice = sum(durs) + GAP * (len(scenes) - 1) + TAIL
    tempo = 1.0
    if total_voice > 40:
        tempo = (sum(durs)) / (39.6 - GAP * (len(scenes) - 1) - TAIL)
        if tempo > 1.1:
            raise ValueError(f"озвучка {total_voice:.1f} с, ускорять больше 10 % нельзя")
        durs = [clean_voice(a, c, sc["voice"], tempo)[0] for a, c, sc in zip(audio_files, clean, scenes)]
        total_voice = sum(durs) + GAP * (len(scenes) - 1) + TAIL

    # короткая озвучка: растягиваем паузы между фразами и удерживаем финальный кадр, чтобы уложиться в минимум 25 с
    gap, tail = GAP, TAIL
    need = MIN_TOTAL + 0.4 - (sum(durs) + GAP * (len(scenes) - 1) + TAIL)
    if need > 0 and len(scenes) > 1:
        gap_extra = min(need * 0.5 / (len(scenes) - 1), 0.45)
        gap += gap_extra
        tail += min(need - gap_extra * (len(scenes) - 1), 2.6)

    timeline, t = [], 0.0
    for i, (sc, c, d) in enumerate(zip(scenes, clean, durs)):
        script = sc["voice"].split()
        # без подсказки: подсказка с текстом сценария на проверке выдавала «Редактор субтитров» вместо живой речи
        words, sim = align(script, transcribe(c), d)
        if sim < 0.8:
            alt = align(script, transcribe(c, sc["voice"]), d)
            if alt[1] > sim:
                words, sim = alt
        if sim < 0.5:
            raise ValueError(f"сцена {i + 1}: звук не совпадает с текстом (услышано {sim:.0%} слов)")
        # всё, что звучит после последнего слова сценария, в ролик не идёт
        speech_end = words[-1][2] + 0.18
        if speech_end < d - 0.05:
            cut = tmp / f"cut{i}.wav"
            run("ffmpeg", "-y", "-v", "error", "-i", str(c), "-t", f"{speech_end:.3f}", str(cut))
            cut.replace(c)
            d = probe(c)
        scene_len = d + (gap if i < len(scenes) - 1 else tail)
        timeline.append({**sc, "words": words, "match": round(sim, 2), "start": round(t, 3), "end": round(t + scene_len, 3)})
        t += scene_len
    total = t
    if not 25 <= total <= 40:
        raise ValueError(f"длительность {total:.1f} с вне диапазона 25–40 с")

    # звук: реплики плюс точные паузы, те же границы, что у субтитров
    parts = []
    for i, (c, sc) in enumerate(zip(clean, timeline)):
        p = tmp / f"p{i}.wav"
        run("ffmpeg", "-y", "-v", "error", "-i", str(c), "-af", f"apad=whole_dur={sc['end'] - sc['start']:.4f}",
            "-ar", "44100", "-ac", "1", str(p))
        parts.append(p)
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    voice = tmp / "voice.wav"
    run("ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(voice))

    # видео: у реплики может быть несколько кадров, они делят её время поровну; смена кадра совпадает с границей слов
    cuts: list[dict] = []
    for sc in timeline:
        sh = sc.get("shots") or [{"image_id": sc["image_id"], "effect": sc.get("effect", "zoom_in")}]
        step = (sc["end"] - sc["start"]) / len(sh)
        for j, x in enumerate(sh):
            cuts.append({**x, "start": round(sc["start"] + j * step, 3), "end": round(sc["start"] + (j + 1) * step, 3)})
    clips: list[Path] = []
    plans = []
    for i, cut in enumerate(cuts):
        item = images[cut["image_id"]]
        faces = sorted(item.get("faces") or [])
        if cut.get("focus") is not None and faces:  # крупный план одного человека из общего фото
            item = {**item, "faces": [faces[min(cut["focus"], len(faces) - 1)]]}
        plan = plan_motion(item, cut.get("effect", "zoom_in"))
        plans.append(plan)
        last = i == len(cuts) - 1
        clip_len = cut["end"] - cut["start"] + (0 if last else XFADE)
        frames = int(round(clip_len * FPS))
        c = tmp / f"c{i}.mp4"
        if plan["layout"] == "cover":
            render_cover(Path(item["file"]), plan, frames, c)
        else:
            render_contain(Path(item["file"]), item, plan, frames, c, tmp, flip=bool(i % 2))
        clips.append(c)
    ass = tmp / "subs.ass"
    ass.write_text(build_ass(timeline, total), encoding="utf-8")
    ov = tmp / "overlay.png"
    dark_overlay(ov)

    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(ov), "-i", str(voice)]
    fc, prev = [], "[0:v]"
    for i in range(1, len(clips)):
        off = cuts[i]["start"]
        fc.append(f"{prev}[{i}:v]xfade=transition={TRANSITIONS[(i - 1) % len(TRANSITIONS)]}:duration={XFADE}:offset={off:.3f}[x{i}]")
        prev = f"[x{i}]"
    fc.append(f"[{len(clips)}:v]format=rgba[ov]")
    fc.append(f"{prev}[ov]overlay=0:0:shortest=1,ass={ass}:fontsdir=/usr/share/fonts/truetype/montserrat,fps={FPS},format=yuv420p[v]")
    run("ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(fc), "-map", "[v]", "-map", f"{len(clips) + 1}:a",
        "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(out))
    manifest = {"duration": round(total, 2), "voice_tempo": round(tempo, 3), "dropped_audio_sec": dropped, "width": W, "height": H, "fps": FPS,
                "scenes": [{"line": s.get("line"), "words_heard": s["match"], "start": s["start"], "end": s["end"],
                            "words": [[w, round(a + s["start"], 2), round(b + s["start"], 2)] for w, a, b in s["words"]]} for s in timeline],
                "shots": [{"image_id": c["image_id"], "start": c["start"], "end": c["end"], "effect": p["effect"], "layout": p["layout"]}
                          for c, p in zip(cuts, plans)]}
    out.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    if not keep_tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return manifest
