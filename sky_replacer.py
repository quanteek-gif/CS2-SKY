# -*- coding: utf-8 -*-
# =============================================================================
#  sky_replacer.py -- замена неба в Counter-Strike 2 (Workshop Tools)
#  Windows / Python 3.10+ / tkinter + Pillow
#
#  Запуск:     python sky_replacer.py
#  Зависимость: pip install Pillow
#
#  ЧТО ДЕЛАЕТ ПРОГРАММА:
#    1. Выбираете папку СВОЕГО аддона .../game/csgo/addons/имя_аддона/
#       (путь запоминается в config.json рядом со скриптом).
#    2. Выбираете картинку неба (PNG/JPG) -- она открывается в предпросмотре.
#    3. Ползунками меняете яркость, контраст, насыщенность, оттенок.
#    4. "Применить": картинка приводится к TEXTURE_WIDTH x TEXTURE_HEIGHT,
#       сохраняется как materials/skybox/ИМЯ.png, рядом создаётся
#       materials/skybox/ИМЯ.vmat. Перед ПЕРВОЙ заменой оригинальные файлы
#       копируются в <аддон>/_sky_backup/ (бэкап делается только один раз).
#    5. "Вернуть оригинал" восстанавливает файлы из этого бэкапа.
#
#  БЕЗОПАСНОСТЬ:
#    Всё пишется ТОЛЬКО внутрь папки выбранного аддона. Функция
#    ensure_inside_addon() проверяет каждый путь перед записью. В папки
#    самой игры (game/csgo/maps, game/csgo/materials и т.д.) ничего не пишется.
#
#  ОКНО НЕ ЗАВИСАЕТ:
#    Чтение и запись файлов идут в фоновом потоке, результат приходит через
#    queue.Queue и разбирается в главном потоке через root.after().
#
#  ПРЕДПОЛОЖЕНИЯ О ФОРМАТЕ CS2 (все вынесены в константы, раздел 1):
#    #1  структура аддона .../game/csgo/addons/имя/materials/...
#    #2  текстура неба -- materials/skybox/ИМЯ.png. Source 2 читает PNG/TGA
#        как исходник текстуры; VTF -- это формат Source 1, он НЕ используется.
#    #3  .vmat -- текстовый файл Source 2 вида "Layer0" { "shader" ... }
#    #4  шейдер неба VMAT_SHADER = "sky.vfx" -- ЕСЛИ НЕ СОВПАДЁТ, возьмите
#        строку shader из любого готового materials/skybox/*.vmat
#        (удобно через Source2Viewer) и подставьте в константу.
#    #5  небо однофейсовое (2D). Для кубического -- см. SKY_SUFFIXES.
#    Если что-то не так -- правьте только константы в начале файла.
# =============================================================================

import json
import os
import queue
import shutil
import threading
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageEnhance, ImageOps, ImageTk
except ImportError:
    raise SystemExit("Не найдена Pillow. Установите командой:  pip install Pillow")


# =============================================================================
# 1. КОНСТАНТЫ -- ВСЕ пути, размеры и параметры ТОЛЬКО здесь
# =============================================================================

APP_TITLE = "CS2 Sky Replacer - замена неба"
WINDOW_WIDTH = 1160
WINDOW_HEIGHT = 740
MIN_WINDOW_WIDTH = 940
MIN_WINDOW_HEIGHT = 620

# ---- config.json (лежит рядом со скриптом, в папку игры не пишется) -------
CONFIG_FILE_NAME = "config.json"
CFG_ADDON_DIR = "addon_dir"
CFG_IMAGE = "last_image"
CFG_SKY_NAME = "sky_name"

# ---- Папка аддона ----------------------------------------------------------
# ПРЕДПОЛОЖЕНИЕ #1: Workshop Tools раскладывает аддоны так:
#     <CS2>/game/csgo/addons/имя_аддона/materials/...
#     <CS2>/game/csgo/addons/имя_аддона/maps/...
DEFAULT_ADDON_DIR = (r"C:\Program Files (x86)\Steam\steamapps\common"
                     r"\Counter-Strike Global Offensive"
                     r"\game\csgo\addons\my_addon")
ADDON_REQUIRED_SEGMENTS = ("game", "csgo", "addons")  # путь обязан содержать эти папки
ADDON_MARKER_DIRS = ("materials", "maps")             # хотя бы одна должна быть
FORBIDDEN_ROOT_NAMES = ("addons", "csgo", "game")     # сам корень -- не аддон

# ---- Что именно заменяем внутри аддона ------------------------------------
SUBDIR_MATERIALS = "materials"
SUBDIR_SKYBOX = "skybox"
SKY_NAME = "cs2_custom_sky"     # имя материала и текстуры БЕЗ расширения
SKY_SUFFIXES = ("",)            # ПРЕДПОЛОЖЕНИЕ #5: 2D-небо, одна текстура.
# Для кубического неба раскомментируйте строку ниже:
# SKY_SUFFIXES = ("_ft", "_bk", "_lf", "_rt", "_dn", "_up")

# ---- Формат и размер текстуры ---------------------------------------------
TEXTURE_WIDTH = 4096           # ширина сохраняемой текстуры
TEXTURE_HEIGHT = 2048          # высота (2:1 -- привычное небо)
TEXTURE_FORMAT = "PNG"         # Source 2 читает PNG/TGA как исходник текстуры
PNG_COMPRESS_LEVEL = 9         # 0..9, 9 -- максимальное сжатие
RESAMPLE = "lanczos"           # "lanczos" | "bicubic" -- алгоритм масштабирования

# ---- .vmat ----------------------------------------------------------------
# ПРЕДПОЛОЖЕНИЕ #3 и #4: текстовый материал Source 2.
VMAT_SHADER = "sky.vfx"            # !!! если не совпадёт -- см. раздел предположений
VMAT_TEXTURE_KEY = "TextureColor1" # ключ входа текстуры в шейдере
VMAT_EXTRA_LINES = ()              # при необходимости: ("F_SOME_FLAG", "1")

# ---- Бэкап (всегда ВНУТРИ аддона) ----------------------------------------
BACKUP_DIR_NAME = "_sky_backup"
BACKUP_INDEX_NAME = "backup_index.json"

# ---- Предпросмотр ---------------------------------------------------------
PREVIEW_MAX_WIDTH = 760
PREVIEW_MAX_HEIGHT = 430
PREVIEW_DEBOUNCE_MS = 40      # перерисовка не чаще, чем раз в 40 мс
QUEUE_POLL_MS = 100           # опрос очереди фонового потока

# ---- Ползунки: (ключ, подпись, мин, макс, шаг, значение по умолчанию) ------
SLIDERS = (
    ("brightness", "Яркость",      0.30, 2.00, 0.01,  1.00),
    ("contrast",   "Контраст",     0.30, 2.00, 0.01,  1.00),
    ("saturation", "Насыщенность", 0.00, 2.00, 0.01,  1.00),
    ("hue",        "Оттенок",   -180.00, 180.00, 1.0, 0.00),
)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")


# =============================================================================
# 2. КОНФИГУРАЦИЯ И ПУТИ
# =============================================================================

def script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    return os.path.join(script_dir(), CONFIG_FILE_NAME)


def load_config():
    path = config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    # пишем РЯДОМ со скриптом, в папку игры -- никогда
    try:
        with open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def path_segments(path):
    return [p.lower() for p in os.path.normpath(path or "").split(os.sep) if p]


def check_addon_dir(path):
    """Проверяет, что path -- именно папка аддона. Возвращает (ok, сообщение)."""
    if not path:
        return False, "Папка аддона не выбрана."
    if not os.path.isdir(path):
        return False, "Папка не существует:\n%s" % path
    segs = path_segments(path)
    for seg in ADDON_REQUIRED_SEGMENTS:
        if seg not in segs:
            return False, ("Путь должен содержать папку '%s':\n%s\n\n"
                           "Нужна папка аддона вида .../game/csgo/addons/имя_аддона"
                           ) % (seg, path)
    base = os.path.basename(os.path.normpath(path)).lower()
    if base in FORBIDDEN_ROOT_NAMES:
        return False, ("Это корень, а не аддон:\n%s\n\n"
                       "Выберите папку ВНУТРИ addons, например addons/my_addon" % path)
    markers = [m for m in ADDON_MARKER_DIRS if os.path.isdir(os.path.join(path, m))]
    if not markers:
        return False, ("В папке нет ни 'materials', ни 'maps' --\n"
                       "похоже, это не папка аддона:\n%s" % path)
    return True, "Аддон принят: %s" % path


def ensure_inside_addon(addon_dir, target):
    """Главная проверка безопасности: target обязан лежать внутри аддона."""
    addon = os.path.normcase(os.path.realpath(addon_dir))
    path = os.path.normcase(os.path.realpath(target))
    try:
        common = os.path.commonpath([addon, path])
    except ValueError:
        raise RuntimeError("Путь на другой диск, запись запрещена: %s" % target)
    if common != addon:
        raise RuntimeError("Отказ: запись вне папки аддона:\n%s" % target)


def join_addon(addon_dir, *parts):
    """Собирает путь внутри аддона и гарантирует, что он внутри аддона."""
    target = os.path.abspath(os.path.join(addon_dir, *parts))
    ensure_inside_addon(addon_dir, target)
    return target


def target_rel_paths(sky_name):
    """Список путей (через /) относительно аддона: текстура и материал."""
    rels = []
    for suffix in SKY_SUFFIXES:
        rels.append("%s/%s/%s%s.png" % (SUBDIR_MATERIALS, SUBDIR_SKYBOX,
                                        sky_name, suffix))
        rels.append("%s/%s/%s%s.vmat" % (SUBDIR_MATERIALS, SUBDIR_SKYBOX,
                                         sky_name, suffix))
    return rels


def rel_to_abs(addon_dir, rel):
    return join_addon(addon_dir, *rel.split("/"))


# =============================================================================
# 3. РАБОТА С КАРТИНКОЙ (Pillow)
# =============================================================================

def open_image(path):
    """Читает картинку и приводит к RGB: EXIF, режим, прозрачность."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img.load()
    if img.mode == "P":
        img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (150, 180, 215))  # прозрачность -> небо
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img


def rotate_hue(img, degrees):
    """Сдвиг оттенка через HSV-канал. PIL не имеет hue rotate -- делаем сами."""
    if abs(degrees) < 0.5:
        return img
    hsv = img.convert("HSV")
    h, s, v = hsv.split()
    shift = int(round(float(degrees) * 256.0 / 360.0)) % 256
    h = h.point(lambda p, sh=shift: (p + sh) % 256)
    return Image.merge("HSV", (h, s, v)).convert("RGB")


def adjust(img, params):
    """Применяет четыре ползунка. Работает и с большим файлом, и с превью."""
    out = img
    b = float(params.get("brightness", 1.0))
    c = float(params.get("contrast", 1.0))
    s = float(params.get("saturation", 1.0))
    h = float(params.get("hue", 0.0))
    if abs(b - 1.0) > 0.001:
        out = ImageEnhance.Brightness(out).enhance(b)
    if abs(c - 1.0) > 0.001:
        out = ImageEnhance.Contrast(out).enhance(c)
    if abs(s - 1.0) > 0.001:
        out = ImageEnhance.Color(out).enhance(s)
    if abs(h) > 0.5:
        out = rotate_hue(out, h)
    return out


def fit_to_size(img, width, height):
    """Обрезает и масштабирует ровно под нужный размер (без искажений)."""
    method = Image.LANCZOS if RESAMPLE == "lanczos" else Image.BICUBIC
    return ImageOps.fit(img.convert("RGB"), (width, height),
                        method=method, centering=(0.5, 0.5))


def build_preview_base(img):
    """Маленькая рабочая копия для предпросмотра: окно не тормозит."""
    base = img.copy()
    base.thumbnail((PREVIEW_MAX_WIDTH, PREVIEW_MAX_HEIGHT), Image.LANCZOS)
    return base


# =============================================================================
# 4. ЗАПИСЬ ФАЙЛОВ В АДДОН (.png + .vmat) И БЭКАП
# =============================================================================

def build_vmat(texture_rel):
    """Текстовый материал Source 2. ПРЕДПОЛОЖЕНИЕ #3 и #4 -- см. шапку файла."""
    lines = [
        "// THIS FILE IS AUTO-GENERATED  (sky_replacer.py)",
        "// ПРЕДПОЛОЖЕНИЕ: шейдер и ключи неба Source 2.",
        "// Если Material Editor ругается -- возьмите строку shader из готового",
        "// materials/skybox/*.vmat (Source2Viewer) и подставьте в VMAT_SHADER.",
        '"Layer0"',
        "{",
        '\t"shader" "%s"' % VMAT_SHADER,
        '\t"%s" "%s"' % (VMAT_TEXTURE_KEY, texture_rel),
    ]
    for key, value in VMAT_EXTRA_LINES:
        lines.append('\t"%s" "%s"' % (key, value))
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def backup_originals(addon_dir, rel_paths):
    """Бэкап делается РОВНО ОДИН РАЗ перед первой заменой."""
    bdir = join_addon(addon_dir, BACKUP_DIR_NAME)
    index_path = os.path.join(bdir, BACKUP_INDEX_NAME)
    if os.path.isfile(index_path):
        return "бэкап уже есть, оригиналы не тронуты"
    os.makedirs(bdir, exist_ok=True)
    index = {"saved": {}, "created": []}
    for rel in rel_paths:
        src = rel_to_abs(addon_dir, rel)
        if os.path.isfile(src):
            stored = rel.replace("/", "__")
            shutil.copy2(src, os.path.join(bdir, stored))
            index["saved"][rel] = stored
        else:
            index["created"].append(rel)
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    return "бэкап создан: %d файл(ов)" % len(index["saved"])


def task_apply(ctx):
    """Полная операция замены. Выполняется В ФОНОВОМ ПОТОКЕ."""
    addon = ctx["addon"]
    rels = target_rel_paths(ctx["sky_name"])

    backup_note = backup_originals(addon, rels)

    fitted = fit_to_size(ctx["image"], TEXTURE_WIDTH, TEXTURE_HEIGHT)
    final = adjust(fitted, ctx["params"])

    written = 0
    for rel in rels:
        abs_path = rel_to_abs(addon, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        if rel.endswith(".png"):
            final.save(abs_path, TEXTURE_FORMAT.upper(),
                       compress_level=PNG_COMPRESS_LEVEL, optimize=True)
        else:
            # путь текстуры в .vmat -- всегда через / и относительно content
            tex_rel = rel[:-5] + ".png"
            with open(abs_path, "w", encoding="utf-8", newline="\r\n") as fh:
                fh.write(build_vmat(tex_rel))
        written += 1

    return ("Готово. Сохранено %d файл(ов) в %s\n%s\n"
            "Размер текстуры: %d x %d, формат: %s\n%s"
            ) % (written, os.path.join(addon, SUBDIR_MATERIALS, SUBDIR_SKYBOX),
                 ctx["sky_name"], TEXTURE_WIDTH, TEXTURE_HEIGHT,
                 TEXTURE_FORMAT, backup_note)


def task_restore(ctx):
    """Возвращает прежние файлы из бэкапа. Тоже выполняется в потоке."""
    addon = ctx["addon"]
    bdir = join_addon(addon, BACKUP_DIR_NAME)
    index_path = os.path.join(bdir, BACKUP_INDEX_NAME)
    if not os.path.isfile(index_path):
        raise RuntimeError("Бэкап не найден:\n%s\n\nЗамена ещё не делалась." % index_path)
    with open(index_path, "r", encoding="utf-8") as fh:
        index = json.load(fh)

    restored = 0
    for rel, stored in index.get("saved", {}).items():
        src = os.path.join(bdir, stored)
        if not os.path.isfile(src):
            continue
        dst = rel_to_abs(addon, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        restored += 1

    removed = 0
    for rel in index.get("created", []):
        dst = rel_to_abs(addon, rel)
        if os.path.isfile(dst):
            os.remove(dst)
            removed += 1

    return ("Оригинал возвращён: восстановлено %d, удалено %d\n%s"
            ) % (restored, removed, os.path.join(addon, BACKUP_DIR_NAME))


# =============================================================================
# 5. ИНТЕРФЕЙС (tkinter)
# =============================================================================

class SkyApp:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.addon_var = tk.StringVar(value=self.cfg.get(CFG_ADDON_DIR, ""))
        self.sky_var = tk.StringVar(value=self.cfg.get(CFG_SKY_NAME, SKY_NAME))
        self.image_path = self.cfg.get(CFG_IMAGE, "")
        self.source = None          # полноразмерная картинка-оригинал
        self.preview_base = None    # маленькая копия для предпросмотра
        self.photo = None           # ссылка на PhotoImage, чтобы GC не съел
        self.queue = queue.Queue()
        self.busy = False
        self.preview_job = None
        self.sliders = {}
        self._build()
        self.root.after(QUEUE_POLL_MS, self._poll_queue)
        if self.image_path and os.path.isfile(self.image_path):
            self.load_image(self.image_path)
        else:
            self.set_status("Готово. Выберите папку аддона и картинку неба.", "info")

    # ---------- построение окна --------------------------------------------
    def _build(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TButton", padding=(14, 8), font=("Segoe UI", 10))
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 15))
        style.configure("Path.TLabel", font=("Consolas", 9), foreground="#12457F")
        style.configure("Status.TLabel", font=("Consolas", 10))
        style.configure("Big.TButton", font=("Segoe UI Semibold", 11), padding=(18, 10))

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        # --- строка пути аддона ---
        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text="Папка аддона:", style="Title.TLabel").pack(side="left")
        self.addon_entry = ttk.Entry(top, textvariable=self.addon_var)
        self.addon_entry.pack(side="left", fill="x", expand=True, padx=10)
        ttk.Button(top, text="Обзор...", command=self.choose_addon).pack(side="left")
        ttk.Button(top, text="Открыть", command=self.open_addon).pack(side="left", padx=(6, 0))
        ttk.Label(top, text="Имя неба:").pack(side="left", padx=(18, 6))
        self.sky_entry = ttk.Entry(top, textvariable=self.sky_var, width=22)
        self.sky_entry.pack(side="left")

        # --- центр: предпросмотр + управление ---
        mid = ttk.Frame(outer)
        mid.pack(fill="both", expand=True, pady=(12, 6))

        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)
        ttk.Button(left, text="Выбрать картинку неба...",
                   command=self.choose_image).pack(anchor="w")
        self.image_label = ttk.Label(left, text="картинка не выбрана",
                                     style="Path.TLabel")
        self.image_label.pack(anchor="w", pady=(6, 6))

        box = ttk.Frame(left, relief="sunken", padding=2)
        box.pack(fill="both", expand=True)
        self.preview_label = ttk.Label(box, anchor="center",
                                       text="здесь будет предпросмотр неба")
        self.preview_label.pack(fill="both", expand=True)

        right = ttk.Frame(mid, padding=(18, 0, 0, 0))
        right.pack(side="left", fill="y")
        ttk.Label(right, text="Цвет неба", style="Title.TLabel").pack(anchor="w")
        ttk.Label(right, text="Ползунки работают вживую, без сохранения.",
                  foreground="#6E675B").pack(anchor="w", pady=(2, 10))

        for key, label, lo, hi, step, default in SLIDERS:
            self._add_slider(right, key, label, lo, hi, step, default)

        ttk.Button(right, text="Сброс ползунков",
                   command=self.reset_sliders).pack(anchor="w", pady=(14, 0))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=16)

        self.apply_btn = ttk.Button(right, text="Применить", style="Big.TButton",
                                    command=lambda: self.start_task("apply"))
        self.apply_btn.pack(fill="x")
        self.restore_btn = ttk.Button(right, text="Вернуть оригинал",
                                      style="Big.TButton",
                                      command=lambda: self.start_task("restore"))
        self.restore_btn.pack(fill="x", pady=(8, 0))
        ttk.Label(right, text="Бэкап делается один раз,\n"
                              "перед первой заменой.",
                  foreground="#6E675B", justify="left").pack(anchor="w", pady=(10, 0))

        # --- строка статуса ---
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(8, 0))
        self.status_var = tk.StringVar(value="...")
        self.status_label = ttk.Label(bar, textvariable=self.status_var,
                                      style="Status.TLabel", relief="sunken",
                                      anchor="w", padding=(8, 5))
        self.status_label.pack(fill="x")

    def _add_slider(self, parent, key, label, lo, hi, step, default):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=5)
        ttk.Label(row, text=label, width=13).pack(side="left")
        var = tk.DoubleVar(value=default)
        scale = ttk.Scale(row, from_=lo, to=hi, variable=var,
                          command=lambda _v, k=key: self._on_slider(k))
        scale.pack(side="left", fill="x", expand=True, padx=8)
        readout = ttk.Label(row, text=self._fmt(key, default), width=8,
                            anchor="e", style="Path.TLabel")
        readout.pack(side="right")
        self.sliders[key] = {"var": var, "label": readout,
                             "step": step, "lo": lo, "hi": hi, "default": default}

    # ---------- статус и блокировка кнопок ---------------------------------
    def set_status(self, text, kind="info"):
        colors = {"info": "#14120E", "ok": "#12457F",
                  "warn": "#D9611B", "error": "#B3261E", "wait": "#6E675B"}
        self.status_var.set(text.replace("\n", "   |   "))
        self.status_label.configure(foreground=colors.get(kind, "#14120E"))

    def set_busy(self, flag):
        self.busy = flag
        state = "disabled" if flag else "normal"
        for w in (self.apply_btn, self.restore_btn):
            w.configure(state=state)
        for entry in (self.addon_entry, self.sky_entry):
            entry.configure(state=state)

    # ---------- выбор папки и картинки -------------------------------------
    def choose_addon(self):
        start = self.addon_var.get() or DEFAULT_ADDON_DIR
        if not os.path.isdir(start):
            start = os.path.dirname(start)
        path = filedialog.askdirectory(title="Папка аддона CS2",
                                       initialdir=start if os.path.isdir(start) else "/")
        if not path:
            return
        ok, msg = check_addon_dir(path)
        if not ok:
            messagebox.showerror(APP_TITLE, msg)
            self.set_status("Папка аддона не принята.", "error")
            return
        self.addon_var.set(path)
        self.cfg[CFG_ADDON_DIR] = path
        save_config(self.cfg)
        self.set_status(msg, "ok")

    def open_addon(self):
        path = self.addon_var.get().strip()
        ok, msg = check_addon_dir(path)
        if not ok:
            messagebox.showerror(APP_TITLE, msg)
            return
        try:
            os.startfile(path)   # только открыть проводник, ничего не пишем
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "Не удалось открыть папку:\n%s" % exc)

    def choose_image(self):
        start = os.path.dirname(self.image_path) if self.image_path else script_dir()
        path = filedialog.askopenfilename(
            title="Картинка неба",
            initialdir=start if os.path.isdir(start) else script_dir(),
            filetypes=[("Картинки", " ".join("*" + e for e in IMAGE_EXTENSIONS)),
                       ("Все файлы", "*.*")])
        if path:
            self.load_image(path)

    def load_image(self, path):
        try:
            src = open_image(path)
        except Exception as exc:
            messagebox.showerror(APP_TITLE,
                                 "Не удалось открыть картинку:\n%s\n\n%s" % (path, exc))
            self.set_status("Ошибка чтения картинки.", "error")
            return
        self.source = src
        self.preview_base = build_preview_base(src)
        self.image_path = path
        self.image_label.configure(text=path)
        self.cfg[CFG_IMAGE] = path
        save_config(self.cfg)
        self._render_preview()
        self.set_status("Картинка загружена: %d x %d" % src.size, "ok")

    # ---------- ползунки и предпросмотр ------------------------------------
    def _fmt(self, key, value):
        if key == "hue":
            return "%+.0f deg" % value
        return "%.2f" % value

    def current_params(self):
        return {key: item["var"].get() for key, item in self.sliders.items()}

    def _on_slider(self, key):
        item = self.sliders[key]
        item["label"].configure(text=self._fmt(key, item["var"].get()))
        self.schedule_preview()

    def reset_sliders(self):
        for key, item in self.sliders.items():
            item["var"].set(item["default"])
            item["label"].configure(text=self._fmt(key, item["default"]))
        self.schedule_preview()

    def schedule_preview(self):
        if self.preview_job is not None:
            return
        self.preview_job = self.root.after(PREVIEW_DEBOUNCE_MS, self._render_preview)

    def _render_preview(self):
        self.preview_job = None
        if self.preview_base is None:
            return
        try:
            img = adjust(self.preview_base, self.current_params())
            self.photo = ImageTk.PhotoImage(img)   # держим ссылку сами
            self.preview_label.configure(image=self.photo, text="")
        except Exception as exc:
            self.set_status("Предпросмотр: %s" % exc, "error")

    # ---------- запуск операции в фоновом потоке ---------------------------
    def _collect_ctx(self, task):
        addon = self.addon_var.get().strip()
        ok, msg = check_addon_dir(addon)
        if not ok:
            raise RuntimeError(msg)
        if task == "apply":
            if self.source is None:
                raise RuntimeError("Сначала выберите картинку неба.")
            sky = self.sky_var.get().strip() or SKY_NAME
            bad = set('\\/:*?"<>|')
            if any(ch in sky for ch in bad):
                raise RuntimeError("Недопустимое имя неба: %s\nРазрешены буквы, цифры, _ и -" % sky)
        else:
            sky = self.sky_var.get().strip() or SKY_NAME
        return {"addon": addon, "sky_name": sky,
                "image": self.source, "params": self.current_params()}

    def start_task(self, task):
        if self.busy:
            self.set_status("Подождите: операция ещё выполняется.", "warn")
            return
        try:
            ctx = self._collect_ctx(task)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            self.set_status(str(exc).splitlines()[0], "error")
            return
        self.set_busy(True)
        self.set_status("Работаю... файлы пишутся в фоновом потоке", "wait")
        threading.Thread(target=self._thread_main, args=(task, ctx),
                         daemon=True).start()

    def _thread_main(self, task, ctx):
        # ВНИМАНИЕ: здесь нельзя трогать виджеты tkinter
        try:
            if task == "apply":
                message = task_apply(ctx)
            else:
                message = task_restore(ctx)
            self.queue.put(("ok", message))
        except Exception:
            self.queue.put(("err", traceback.format_exc()))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                self.set_busy(False)
                if kind == "ok":
                    self.set_status(payload, "ok")
                else:
                    lines = [ln for ln in payload.strip().splitlines() if ln.strip()]
                    short = lines[-1] if lines else "Неизвестная ошибка"
                    messagebox.showerror(APP_TITLE,
                                         "%s\n\nПодробности:\n%s" % (short, payload))
                    self.set_status(short, "error")
        except queue.Empty:
            pass
        self.root.after(QUEUE_POLL_MS, self._poll_queue)


# =============================================================================
# 6. ТОЧКА ВХОДА
# =============================================================================

def main():
    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("%dx%d" % (WINDOW_WIDTH, WINDOW_HEIGHT))
    root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
    SkyApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
