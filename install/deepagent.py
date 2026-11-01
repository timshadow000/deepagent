import webview
import threading
import json
import os
import re
import sys
import time
import uuid
import base64
import html as htmllib
import zipfile
import platform
import subprocess
import shutil
import signal
import webbrowser
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from openai import OpenAI

try:
    from PIL import Image  
except Exception:
    Image = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.expanduser("~/.deepseek_desktop")
CHATS_DIR = os.path.join(APP_DIR, "chats")
UPLOADS_DIR = os.path.join(APP_DIR, "uploads")
MODELS_CACHE = os.path.join(APP_DIR, "models_cache.json")
CONFIG_FILE = os.path.expanduser("~/.deepseek_webview_config.json")
DEFAULT_WORKDIR = os.path.join(os.path.expanduser("~"), "deepseek_workspace")
LOGO_URL = "https://trace-logos.ru/assets/logos/search-previews/deepseek.webp"
UA = "Mozilla/5.0 (DeepSeekDesktop/3.0)"
for _d in (CHATS_DIR, UPLOADS_DIR):
    os.makedirs(_d, exist_ok=True)

PROVIDERS = ("DeepSeek", "OpenRouter")
DEFAULT_SYSTEM = "Ты — DeepSeek. Отвечай красиво, структурированно, используй Markdown."
MAX_ATT_CHARS = 60000

def M(mid, name=None, vision=False, tools=True, free=False):
    return {"id": mid, "name": name or mid, "vision": vision, "tools": tools, "free": free}

CURATED = {
    "DeepSeek": [
        M("deepseek-chat", "DeepSeek V3 · chat"),
        M("deepseek-reasoner", "DeepSeek R1 · reasoner", tools=False),
    ],
    "OpenRouter": [
        M("deepseek/deepseek-chat", "DeepSeek V3 · chat"),
        M("deepseek/deepseek-r1", "DeepSeek R1", tools=False),
        M("openai/gpt-4o", "GPT-4o", vision=True),
        M("google/gemini-2.0-flash-001", "Gemini 2.0 Flash", vision=True),
        M("anthropic/claude-3.5-sonnet", "Claude 3.5 Sonnet", vision=True),
        M("qwen/qwen2.5-vl-72b-instruct", "Qwen2.5-VL 72B", vision=True, tools=False),
    ],
}

def _clip(s, n=12000):
    s = str(s)
    if len(s) <= n:
        return s
    h = n * 2 // 3
    t = n - h
    return s[:h] + f"\n…[обрезано {len(s) - n} симв.]…\n" + s[-t:]

def _decode(b):
    for enc in ("utf-8", "cp866" if os.name == "nt" else "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            pass
    return b.decode("utf-8", errors="replace")

def _kill(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

def html_to_text(h):
    h = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = htmllib.unescape(h)
    h = re.sub(r"[ \t\r\f\v]+", " ", h)
    h = re.sub(r"\n\s*\n+", "\n\n", h)
    return h.strip()

def fetch_logo_file():
    """Скачивает логотип по ссылке и кэширует в ~/.deepseek_desktop/logo.webp"""
    try:
        req = urllib.request.Request(LOGO_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        if len(data) > 8 and (data[:4] == b"RIFF" or data[:4] == b"\x89PNG" or data[:2] == b"\xff\xd8"):
            with open(os.path.join(APP_DIR, "logo.webp"), "wb") as f:
                f.write(data)
            return True
    except Exception:
        pass
    return False

LOGO_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}

def load_logo():
    for base in (SCRIPT_DIR, APP_DIR):
        for ext, mime in LOGO_MIME.items():
            p = os.path.join(base, "logo" + ext)
            if os.path.isfile(p):
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                    if data[:4] == b"\x89PNG":
                        mime = "image/png"
                    elif data[:4] == b"RIFF":
                        mime = "image/webp"
                    elif data[:2] == b"\xff\xd8":
                        mime = "image/jpeg"
                    return f"data:{mime};base64," + base64.b64encode(data).decode()
                except Exception:
                    pass
    return ""

def validate_key(provider, key):
    url = ("https://api.deepseek.com/models" if provider == "DeepSeek"
           else "https://openrouter.ai/api/v1/auth/key")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True, ""
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"Неверный API-ключ ({provider})."
        return False, f"Сервер {provider} ответил ошибкой {e.code}."
    except Exception as e:
        return False, "Не удалось подключиться: " + str(e)

def fetch_openrouter_models():
    req = urllib.request.Request("https://openrouter.ai/api/v1/models", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)["data"]
    out = []
    for m in data:
        arch = m.get("architecture") or {}
        if "text" not in (arch.get("output_modalities") or ["text"]):
            continue
        pr = m.get("pricing") or {}
        free = str(pr.get("prompt", "1")) in ("0", "0.0") and str(pr.get("completion", "1")) in ("0", "0.0")
        out.append({
            "id": m["id"], "name": m.get("name") or m["id"],
            "vision": "image" in (arch.get("input_modalities") or []),
            "tools": "tools" in (m.get("supported_parameters") or []),
            "free": free,
        })
    out.sort(key=lambda x: (not x["id"].startswith("deepseek/"), x["name"].lower()))
    return out

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}

def _xml_text(xml):
    xml = re.sub(r"</w:p>|</a:p>", "\n", xml)
    xml = xml.replace("<w:tab/>", "\t")
    xml = re.sub(r"<[^>]+>", "", xml)
    return htmllib.unescape(xml)

def _docx_text(path):
    with zipfile.ZipFile(path) as z:
        return _xml_text(z.read("word/document.xml").decode("utf-8", "replace"))

def _pptx_text(path):
    with zipfile.ZipFile(path) as z:
        names = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                       key=lambda n: int(re.findall(r"\d+", n)[-1]))
        out = []
        for i, n in enumerate(names, 1):
            xml = z.read(n).decode("utf-8", "replace")
            texts = re.findall(r"<a:t>(.*?)</a:t>", xml, re.S)
            out.append(f"--- Слайд {i} ---\n" + "\n".join(htmllib.unescape(t) for t in texts))
        return "\n\n".join(out)

def _xlsx_text(path):
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"=== Лист: {ws.title} ===")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 500:
                    out.append("…")
                    break
                out.append("\t".join("" if v is None else str(v) for v in row))
        return "\n".join(out)
    except ImportError:
        with zipfile.ZipFile(path) as z:
            if "xl/sharedStrings.xml" in z.namelist():
                xml = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                return "(openpyxl не установлен — показаны только строки)\n" + \
                    "\n".join(htmllib.unescape(t) for t in re.findall(r"<t[^>]*>(.*?)</t>", xml, re.S))
        return ""

def _pdf_text(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            raise RuntimeError("для чтения PDF установите библиотеку: pip install pypdf")
    r = PdfReader(path)
    out = []
    for i, p in enumerate(r.pages[:80], 1):
        out.append(f"--- Стр. {i} ---\n" + (p.extract_text() or ""))
    return "\n".join(out)

def extract_file_text(path):
    """-> (kind, text, note); kind: 'text' | 'binary'"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".docx":
            return "text", _docx_text(path), ""
        if ext == ".pptx":
            return "text", _pptx_text(path), ""
        if ext in (".xlsx", ".xlsm"):
            return "text", _xlsx_text(path), ""
        if ext == ".pdf":
            return "text", _pdf_text(path), ""
        if ext in (".zip", ".jar", ".apk", ".whl"):
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
            return "text", f"Архив, файлов: {len(names)}\n" + "\n".join(names[:300]), ""
    except Exception as e:
        return "binary", "", f"не удалось извлечь текст ({e})"
    with open(path, "rb") as f:
        raw = f.read(3_000_000)
    if b"\x00" in raw[:8192]:
        head = " ".join(f"{b:02x}" for b in raw[:48])
        return "binary", "", f"бинарный файл, первые байты: {head}"
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return "text", raw.decode(enc), ""
        except Exception:
            pass
    return "text", raw.decode("utf-8", errors="replace"), ""

def _tool(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}

TOOLS = [
    _tool("run_command", "Выполнить команду в оболочке ОС пользователя (рабочая папка — workspace). Возвращает код возврата и вывод.",
          {"command": {"type": "string", "description": "Команда"},
           "timeout": {"type": "integer", "description": "Таймаут в секундах (по умолчанию 60, максимум 600)"}},
          ["command"]),
    _tool("read_file", "Прочитать файл (текст, docx, pptx, xlsx, pdf). Путь абсолютный или относительно workspace.",
          {"path": {"type": "string"}, "offset": {"type": "integer", "description": "С какой строки (с 1)"},
           "limit": {"type": "integer", "description": "Сколько строк"}}, ["path"]),
    _tool("write_file", "Создать или перезаписать текстовый файл (папки создаются автоматически).",
          {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _tool("edit_file", "Заменить фрагмент текста в файле. old_text должен встречаться ровно один раз (или укажите replace_all).",
          {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
           "replace_all": {"type": "boolean"}}, ["path", "old_text", "new_text"]),
    _tool("list_dir", "Показать содержимое папки.", {"path": {"type": "string", "description": "По умолчанию workspace"}}, []),
    _tool("search_files", "Найти текст (регулярное выражение) в файлах папки.",
          {"pattern": {"type": "string"}, "path": {"type": "string"},
           "glob": {"type": "string", "description": "Маска имён, например *.py"}}, ["pattern"]),
    _tool("fetch_url", "Скачать веб-страницу и вернуть её текст.", {"url": {"type": "string"}}, ["url"]),
    _tool("web_search", "Поиск в интернете (DuckDuckGo). Возвращает заголовки, ссылки и сниппеты.",
          {"query": {"type": "string"}}, ["query"]),
]
DANGEROUS = {"run_command", "write_file", "edit_file"}
FORCE_RE = re.compile(
    r"(rm\s+-[a-z]*r[a-z]*\s+(/|~|\*|\$HOME)|\bmkfs|dd\s+if=|:\(\)\s*\{|\bshutdown\b|\breboot\b|"
    r"format\s+[a-z]:|del\s+/[sfq]|\brd\s+/s|Remove-Item.*-Recurse.*-Force|chmod\s+-R\s+777\s+/|"
    r"(curl|wget)[^|]*\|\s*(ba)?sh)", re.I)

class Api:
    def __init__(self):
        self.window = None
        self.config = {
            "keys": {p: "" for p in PROVIDERS},
            "provider": "DeepSeek", "model": "deepseek-chat",
            "system_prompt": DEFAULT_SYSTEM, "temperature": 0.7,
            "agent": False, "approval": "ask", "workdir": DEFAULT_WORKDIR, "max_steps": 25,
            "custom_models": {p: [] for p in PROVIDERS},
        }
        self.stop_flag = False
        self.busy = False
        self.pending = {}
        self.approvals = {}
        self.session_auto = False
        self._img_cache = {}
        self.models_ts = 0
        self.logo = load_logo()
        self._load_config()
        self._init_models()
        self.chat = self._blank_chat()

    
    def _load_config(self):
        data = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        keys = dict(self.config["keys"])
        if isinstance(data.get("keys"), dict):
            keys.update({k: v for k, v in data["keys"].items() if k in PROVIDERS and isinstance(v, str)})
        elif data.get("api_key"):
            prov = data.get("provider") if data.get("provider") in PROVIDERS else "DeepSeek"
            keys[prov] = data["api_key"]
        for k, v in data.items():
            if k in self.config and k not in ("keys", "custom_models"):
                self.config[k] = v
        if isinstance(data.get("custom_models"), dict):
            for p in PROVIDERS:
                self.config["custom_models"][p] = list(data["custom_models"].get(p, []))
        self.config["keys"] = keys
        if self.config["provider"] not in PROVIDERS:
            self.config["provider"] = "DeepSeek"
        if not keys.get(self.config["provider"]):
            for p in PROVIDERS:
                if keys.get(p):
                    self.config["provider"] = p
                    break

    def _save_config(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=4, ensure_ascii=False)

    def _init_models(self):
        self.models = {p: [dict(m) for m in CURATED[p]] for p in PROVIDERS}
        if os.path.exists(MODELS_CACHE):
            try:
                with open(MODELS_CACHE, "r", encoding="utf-8") as f:
                    c = json.load(f)
                if c.get("models"):
                    self.models["OpenRouter"] = c["models"]
                    self.models_ts = c.get("ts", 0)
            except Exception:
                pass
        for p in PROVIDERS:
            for mid in self.config["custom_models"].get(p, []):
                self._ensure_model(p, mid, persist=False)
        prov = self.config["provider"]
        if not self.config["model"] or not any(m["id"] == self.config["model"] for m in self.models[prov]):
            if self.config["model"] and prov == "OpenRouter":
                self._ensure_model(prov, self.config["model"])
            else:
                self.config["model"] = self.models[prov][0]["id"]

    def _ensure_model(self, provider, mid, persist=True):
        if not any(m["id"] == mid for m in self.models[provider]):
            self.models[provider].append({"id": mid, "name": mid, "vision": None, "tools": None,
                                          "free": False, "custom": True})
            if persist and mid not in self.config["custom_models"][provider]:
                self.config["custom_models"][provider].append(mid)

    def _model_info(self):
        for m in self.models[self.config["provider"]]:
            if m["id"] == self.config["model"]:
                return m
        return {}

    def workdir(self):
        w = self.config.get("workdir") or DEFAULT_WORKDIR
        try:
            os.makedirs(w, exist_ok=True)
        except Exception:
            w = DEFAULT_WORKDIR
            os.makedirs(w, exist_ok=True)
        return w

    def _core_state(self):
        keys = self.config["keys"]
        return {
            "has_key": any(keys.values()),
            "key_hints": {p: ("•••• " + keys[p][-4:]) if keys.get(p) else "" for p in PROVIDERS},
            "provider": self.config["provider"], "model": self.config["model"],
            "system_prompt": self.config.get("system_prompt", DEFAULT_SYSTEM),
            "temperature": self.config.get("temperature", 0.7),
            "agent": bool(self.config.get("agent")), "approval": self.config.get("approval", "ask"),
            "workdir": self.workdir(), "max_steps": self.config.get("max_steps", 25),
            "models": self.models,
            "models_age": int(time.time() - self.models_ts) if self.models_ts else None,
            "os": platform.system(),
        }

    def get_state(self):
        s = self._core_state()
        s.update({"logo": self.logo, "chats": self._list_chats(), "active": self.chat["id"]})
        return s

    def fetch_logo(self):
        if not self.logo:
            fetch_logo_file()
            self.logo = load_logo()
        return self.logo

    
    def login(self, provider, key):
        key = (key or "").strip()
        if provider not in PROVIDERS:
            return {"ok": False, "error": "Неизвестный провайдер."}
        if not key:
            return {"ok": False, "error": "Введите API-ключ."}
        ok, err = validate_key(provider, key)
        if not ok:
            return {"ok": False, "error": err}
        self.config["keys"][provider] = key
        self.config["provider"] = provider
        if not any(m["id"] == self.config["model"] for m in self.models[provider]):
            self.config["model"] = self.models[provider][0]["id"]
        self._save_config()
        return {"ok": True}

    def logout(self):
        self._stop_and_wait()
        self.config["keys"] = {p: "" for p in PROVIDERS}
        self._save_config()
        self.chat = self._blank_chat()
        return {"ok": True}

    def save_settings(self, keys, system_prompt, temperature, workdir, approval, max_steps):
        keys = keys or {}
        for p in PROVIDERS:
            k = (keys.get(p) or "").strip()
            if k:
                ok, err = validate_key(p, k)
                if not ok:
                    return {"ok": False, "error": err}
                self.config["keys"][p] = k
        if not self.config["keys"].get(self.config["provider"]):
            for p in PROVIDERS:
                if self.config["keys"].get(p):
                    self.config["provider"] = p
                    self.config["model"] = self.models[p][0]["id"]
                    break
        try:
            temperature = max(0.0, min(2.0, float(temperature)))
        except Exception:
            temperature = 0.7
        try:
            max_steps = max(1, min(60, int(max_steps)))
        except Exception:
            max_steps = 25
        workdir = (workdir or "").strip()
        if workdir:
            try:
                os.makedirs(os.path.expanduser(workdir), exist_ok=True)
                self.config["workdir"] = os.path.expanduser(workdir)
            except Exception as e:
                return {"ok": False, "error": "Папка недоступна: " + str(e)}
        self.config.update({
            "system_prompt": (system_prompt or "").strip() or DEFAULT_SYSTEM,
            "temperature": temperature,
            "approval": approval if approval in ("ask", "auto") else "ask",
            "max_steps": max_steps,
        })
        self._save_config()
        return {"ok": True, "state": self._core_state()}

    def set_model(self, provider, model):
        model = (model or "").strip()
        if provider not in PROVIDERS or not model or len(model) > 200:
            return {"ok": False, "error": "bad"}
        if not self.config["keys"].get(provider):
            return {"ok": False, "error": "no_key"}
        self._ensure_model(provider, model)
        self.config["provider"] = provider
        self.config["model"] = model
        self._save_config()
        return {"ok": True, "state": self._core_state()}

    def set_agent(self, on):
        self.config["agent"] = bool(on)
        self._save_config()
        return {"agent": self.config["agent"]}

    def set_approval(self, mode):
        self.config["approval"] = "auto" if mode == "auto" else "ask"
        self._save_config()
        return {"approval": self.config["approval"]}

    def refresh_models(self):
        try:
            lst = fetch_openrouter_models()
            if not lst:
                return {"ok": False, "error": "Пустой список"}
            self.models["OpenRouter"] = lst
            self.models_ts = time.time()
            for mid in self.config["custom_models"]["OpenRouter"]:
                self._ensure_model("OpenRouter", mid, persist=False)
            if self.config["provider"] == "OpenRouter":
                self._ensure_model("OpenRouter", self.config["model"], persist=False)
            with open(MODELS_CACHE, "w", encoding="utf-8") as f:
                json.dump({"ts": self.models_ts, "models": lst}, f, ensure_ascii=False)
            return {"ok": True, "models": self.models, "age": 0}
        except Exception as e:
            return {"ok": False, "error": "Не удалось обновить список: " + str(e)}

    def pick_workdir(self):
        try:
            kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
            res = self.window.create_file_dialog(kind, directory=self.workdir())
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if res:
            self.config["workdir"] = res[0] if isinstance(res, (list, tuple)) else res
            self._save_config()
        return {"ok": True, "workdir": self.workdir()}

    def open_workdir(self):
        p = self.workdir()
        try:
            if os.name == "nt":
                os.startfile(p)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
        except Exception:
            pass
        return {"ok": True}

    def open_url(self, url):
        if str(url).startswith("https://"):
            webbrowser.open(url)
        return {"ok": True}

    
    @staticmethod
    def _blank_chat():
        now = time.time()
        return {"id": uuid.uuid4().hex[:12], "title": "Новый чат", "created": now, "updated": now, "messages": []}

    @staticmethod
    def _valid_id(cid):
        return bool(re.fullmatch(r"[A-Za-z0-9\-]{4,40}", cid or ""))

    def _chat_path(self, cid):
        return os.path.join(CHATS_DIR, cid + ".json")

    def _save_chat(self, chat):
        if not chat["messages"]:
            return
        chat["updated"] = time.time()
        tmp = self._chat_path(chat["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(chat, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._chat_path(chat["id"]))

    def _list_chats(self):
        items = []
        for name in os.listdir(CHATS_DIR):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(CHATS_DIR, name), "r", encoding="utf-8") as f:
                    c = json.load(f)
                items.append({"id": c["id"], "title": c.get("title", "Чат"), "updated": c.get("updated", 0)})
            except Exception:
                continue
        items.sort(key=lambda x: x["updated"], reverse=True)
        return items

    def _stop_and_wait(self):
        if self.busy:
            self.stop_flag = True
            for _ in range(100):
                if not self.busy:
                    break
                time.sleep(0.05)

    def new_chat(self):
        self._stop_and_wait()
        self.chat = self._blank_chat()
        self.pending.clear()
        return {"ok": True, "chats": self._list_chats(), "active": self.chat["id"]}

    def open_chat(self, cid):
        if not self._valid_id(cid) or not os.path.exists(self._chat_path(cid)):
            return {"ok": False}
        self._stop_and_wait()
        try:
            with open(self._chat_path(cid), "r", encoding="utf-8") as f:
                self.chat = json.load(f)
        except Exception:
            return {"ok": False}
        return {"ok": True, "messages": self.chat["messages"], "chats": self._list_chats(), "active": self.chat["id"]}

    def rename_chat(self, cid, title):
        if not self._valid_id(cid) or not os.path.exists(self._chat_path(cid)):
            return {"ok": False}
        title = (title or "").strip()[:80] or "Чат"
        with open(self._chat_path(cid), "r", encoding="utf-8") as f:
            c = json.load(f)
        c["title"] = title
        with open(self._chat_path(cid), "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=1)
        if self.chat["id"] == cid:
            self.chat["title"] = title
        return {"ok": True, "chats": self._list_chats(), "active": self.chat["id"]}

    def delete_chat(self, cid):
        if not self._valid_id(cid):
            return {"ok": False}
        reset = False
        if self.chat["id"] == cid:
            self._stop_and_wait()
            self.chat = self._blank_chat()
            reset = True
        try:
            os.remove(self._chat_path(cid))
        except FileNotFoundError:
            pass
        shutil.rmtree(os.path.join(UPLOADS_DIR, cid), ignore_errors=True)
        return {"ok": True, "reset": reset, "chats": self._list_chats(), "active": self.chat["id"]}

    def delete_all_chats(self):
        self._stop_and_wait()
        for name in os.listdir(CHATS_DIR):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(CHATS_DIR, name))
                except Exception:
                    pass
        shutil.rmtree(UPLOADS_DIR, ignore_errors=True)
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        self.chat = self._blank_chat()
        return {"ok": True, "chats": [], "active": self.chat["id"]}

    
    def _image_data_uri(self, path):
        key = (path, os.path.getmtime(path))
        if key in self._img_cache:
            return self._img_cache[key]
        ext = os.path.splitext(path)[1].lower()
        if Image is not None:
            import io
            im = Image.open(path)
            try:
                im.seek(0)
            except Exception:
                pass
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.thumbnail((1568, 1568))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=86)
            uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        else:
            with open(path, "rb") as f:
                raw = f.read()
            if len(raw) > 6_000_000:
                raise ValueError("Изображение больше 6 МБ. Установите Pillow (pip install pillow) для автосжатия.")
            uri = f"data:{IMG_MIME.get(ext, 'image/png')};base64," + base64.b64encode(raw).decode()
        self._img_cache[key] = uri
        return uri

    def _thumb(self, path):
        try:
            if Image is not None:
                import io
                im = Image.open(path)
                try:
                    im.seek(0)
                except Exception:
                    pass
                im = im.convert("RGB")
                im.thumbnail((180, 180))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=80)
                return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            if os.path.getsize(path) < 900_000:
                with open(path, "rb") as f:
                    return f"data:{IMG_MIME.get(os.path.splitext(path)[1].lower(), 'image/png')};base64," + \
                        base64.b64encode(f.read()).decode()
        except Exception:
            pass
        return ""

    def get_image(self, path):
        if os.path.splitext(str(path))[1].lower() in IMG_EXT and os.path.isfile(path):
            return self._thumb(path)
        return ""

    def _process_path(self, path):
        name = os.path.basename(path)
        size = os.path.getsize(path)
        ext = os.path.splitext(path)[1].lower()
        att = {"id": uuid.uuid4().hex[:10], "name": name, "path": path, "size": size,
               "kind": "binary", "text": "", "note": "", "thumb": ""}
        if ext in IMG_EXT:
            self._image_data_uri(path)  
            att["kind"] = "image"
            att["thumb"] = self._thumb(path)
        else:
            kind, text, note = extract_file_text(path)
            att["kind"] = kind
            att["note"] = note
            if len(text) > MAX_ATT_CHARS:
                text = text[:MAX_ATT_CHARS] + f"\n…[файл обрезан, всего {len(text)} симв.; полный текст доступен агенту через read_file]"
            att["text"] = text
        self.pending[att["id"]] = att
        return {k: att[k] for k in ("id", "name", "path", "size", "kind", "thumb")}

    def attach_files(self):
        try:
            kind = webview.FileDialog.OPEN if hasattr(webview, "FileDialog") else webview.OPEN_DIALOG
            res = self.window.create_file_dialog(kind, allow_multiple=True)
        except Exception as e:
            return {"ok": False, "files": [], "errors": [str(e)]}
        files, errors = [], []
        for p in (res or []):
            try:
                files.append(self._process_path(p))
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")
        return {"ok": True, "files": files, "errors": errors}

    def add_upload(self, name, mime, b64):
        try:
            safe = re.sub(r"[^\w.\- ()]", "_", os.path.basename(name or "file"))[:120] or "file"
            d = os.path.join(UPLOADS_DIR, self.chat["id"])
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"{int(time.time() * 1000) % 10**9}_{safe}")
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
            return {"ok": True, "file": self._process_path(path)}
        except Exception as e:
            return {"ok": False, "error": f"{name}: {e}"}

    def remove_attachment(self, att_id):
        self.pending.pop(att_id, None)
        return {"ok": True}

    
    def stop(self):
        self.stop_flag = True
        return {"status": "ok"}

    def approve(self, call_id, decision):
        a = self.approvals.get(call_id)
        if a:
            a["d"] = decision
            a["ev"].set()
        return {"ok": True}

    def send_message(self, text, att_ids):
        if self.busy:
            return {"status": "busy", "error": "Дождитесь окончания ответа или нажмите «Стоп»."}
        text = (text or "").strip()
        atts = [self.pending[i] for i in (att_ids or []) if i in self.pending]
        if not text and not atts:
            return {"status": "error", "error": "Пустое сообщение."}
        prov = self.config["provider"]
        if not self.config["keys"].get(prov):
            return {"status": "error", "error": f"Нет API-ключа для {prov}. Добавьте его в настройках."}
        info = self._model_info()
        if any(a["kind"] == "image" for a in atts) and info.get("vision") is False:
            return {"status": "error", "error": f"Модель «{info.get('name')}» не видит изображения. Выберите модель с меткой VISION."}
        if self.config["agent"] and info.get("tools") is False:
            return {"status": "error", "error": f"Модель «{info.get('name')}» не умеет вызывать инструменты. Выберите модель с меткой TOOLS или выключите агента."}

        parts = [text]
        for a in atts:
            if a["kind"] == "text":
                parts.append(f"📎 Файл «{a['name']}» ({a['path']}):\n```\n{a['text']}\n```")
            elif a["kind"] == "binary":
                parts.append(f"📎 Файл «{a['name']}» ({a['path']}), {a['size']} байт — {a['note'] or 'бинарный'}")
            else:
                parts.append(f"📎 Изображение «{a['name']}» ({a['path']})")
        msg = {"role": "user", "content": "\n\n".join(p for p in parts if p), "display": text,
               "attachments": [{"name": a["name"], "kind": a["kind"], "path": a["path"], "size": a["size"]} for a in atts]}
        for a in atts:
            self.pending.pop(a["id"], None)

        self.busy = True
        self.stop_flag = False
        self.session_auto = False
        threading.Thread(target=self._run_turn, args=(msg,), daemon=True).start()
        return {"status": "started"}

    def _js(self, fn, *args):
        try:
            payload = ",".join(json.dumps(a, ensure_ascii=False) for a in args)
            self.window.evaluate_js(f"{fn}({payload})")
        except Exception:
            pass

    def _agent_prompt(self):
        return (
            "\n\nРЕЖИМ АГЕНТА. Ты автономный ассистент с доступом к компьютеру пользователя через инструменты. "
            f"ОС: {platform.system()} {platform.release()}. Рабочая папка (workspace): {self.workdir()}. "
            f"Сегодня: {datetime.now().strftime('%Y-%m-%d %H:%M')}.\n"
            "Действуй самостоятельно: разбей задачу на шаги, вызывай инструменты, проверяй результат "
            "(запускай код, перечитывай файлы) и продолжай, пока задача не выполнена. Команды пиши под текущую ОС. "
            "Не придумывай результаты — опирайся только на вывод инструментов. Опасные действия система "
            "подтверждает у пользователя сама, поэтому не спрашивай разрешения на очевидные шаги. "
            "Если ввод пользователя содержит вложенные файлы, их текст и пути уже приложены к сообщению; "
            "изображения ты видишь напрямую. В конце дай краткий итог: что сделано и где лежат файлы."
        )

    def _api_messages(self, chat, agent):
        info = self._model_info()
        allow_img = info.get("vision") is not False
        system = self.config.get("system_prompt", DEFAULT_SYSTEM) + (self._agent_prompt() if agent else "")
        out = [{"role": "system", "content": system}]
        msgs = chat["messages"]
        img_idx = [i for i, m in enumerate(msgs)
                   if m["role"] == "user" and any(a["kind"] == "image" for a in m.get("attachments", []))]
        keep = set(img_idx[-4:])
        for i, m in enumerate(msgs):
            r = m["role"]
            if r == "user":
                imgs = [a for a in m.get("attachments", []) if a["kind"] == "image"]
                if imgs and allow_img and i in keep:
                    parts = [{"type": "text", "text": m["content"] or "Посмотри на изображение."}]
                    for a in imgs:
                        try:
                            parts.append({"type": "image_url", "image_url": {"url": self._image_data_uri(a["path"])}})
                        except Exception:
                            parts[0]["text"] += f"\n[изображение «{a['name']}» недоступно]"
                    out.append({"role": "user", "content": parts})
                else:
                    c = m["content"]
                    if imgs:
                        c += "\n" + "\n".join(f"[изображение «{a['name']}» не передано модели]" for a in imgs)
                    out.append({"role": "user", "content": c})
            elif r == "assistant":
                if agent:
                    d = {"role": "assistant", "content": m.get("content") or ""}
                    if m.get("tool_calls"):
                        d["tool_calls"] = m["tool_calls"]
                    out.append(d)
                else:
                    c = m.get("content") or ""
                    for tc in m.get("tool_calls") or []:
                        c += f"\n[использован инструмент: {tc['function']['name']}]"
                    if c.strip():
                        out.append({"role": "assistant", "content": c})
            elif r == "tool" and agent:
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out

    def _repair(self, chat):
        msgs = chat["messages"]
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i]["role"] == "assistant" and msgs[i].get("tool_calls"):
                ids = {tc["id"] for tc in msgs[i]["tool_calls"]}
                got = {m.get("tool_call_id") for m in msgs[i + 1:] if m["role"] == "tool"}
                if not ids <= got:
                    del msgs[i:]
                break
        if msgs and msgs[-1]["role"] == "user":
            msgs.pop()

    def _stream(self, client, model, messages, use_tools):
        kwargs = {}
        if "reasoner" not in model and "r1" not in model.lower():
            kwargs["temperature"] = self.config.get("temperature", 0.7)
        if use_tools:
            kwargs["tools"] = TOOLS
        resp = client.chat.completions.create(model=model, messages=messages, stream=True, **kwargs)
        text, calls, stopped = "", {}, False
        for chunk in resp:
            if self.stop_flag:
                stopped = True
                break
            if not chunk.choices:
                continue
            d = chunk.choices[0].delta
            think = getattr(d, "reasoning_content", None) or getattr(d, "reasoning", None)
            if think:
                self._js("updateThink", think)
            if d.content:
                text += d.content
                self._js("updateBotBubble", d.content)
            for tc in (getattr(d, "tool_calls", None) or []):
                i = tc.index if tc.index is not None else 0
                c = calls.setdefault(i, {"id": "", "name": "", "args": ""})
                if tc.id:
                    c["id"] = tc.id
                if tc.function:
                    if tc.function.name and not c["name"]:
                        c["name"] = tc.function.name
                    if tc.function.arguments:
                        c["args"] += tc.function.arguments
        try:
            resp.close()
        except Exception:
            pass
        out = []
        for i in sorted(calls):
            c = calls[i]
            if c["name"]:
                out.append({"id": c["id"] or "call_" + uuid.uuid4().hex[:10], "name": c["name"], "args": c["args"]})
        return text, out, stopped

    def _run_turn(self, user_msg):
        chat = self.chat
        chat["messages"].append(user_msg)
        if len(chat["messages"]) == 1:
            t = " ".join((user_msg.get("display") or user_msg["content"]).split())
            chat["title"] = t[:48] or "Новый чат"
        provider = self.config["provider"]
        model = self.config["model"]
        agent = bool(self.config["agent"])
        try:
            self._js("setBusy", True)
            self._js("beginTurn")
            base_url = "https://api.deepseek.com" if provider == "DeepSeek" else "https://openrouter.ai/api/v1"
            headers = ({"HTTP-Referer": "https://localhost", "X-Title": "DeepSeek Desktop"}
                       if provider == "OpenRouter" else {})
            client = OpenAI(api_key=self.config["keys"][provider], base_url=base_url, default_headers=headers)
            max_steps = int(self.config.get("max_steps", 25)) if agent else 1
            limit_hit = False
            for step in range(max_steps):
                if self.stop_flag:
                    break
                text, calls, stopped = self._stream(client, model, self._api_messages(chat, agent), agent)
                amsg = {"role": "assistant", "content": text or None}
                if calls:
                    amsg["tool_calls"] = [{"id": c["id"], "type": "function",
                                           "function": {"name": c["name"], "arguments": c["args"] or "{}"}} for c in calls]
                if text or calls:
                    chat["messages"].append(amsg)
                if not calls:
                    break
                for c in calls:
                    if self.stop_flag:
                        res = "Ошибка: отменено пользователем."
                    else:
                        res = self._run_tool(c)
                    chat["messages"].append({"role": "tool", "tool_call_id": c["id"], "content": res})
                self._save_chat(chat)
                if step == max_steps - 1 and not self.stop_flag:
                    limit_hit = True
            if limit_hit:
                note = "\n\n⚠ Достигнут лимит шагов агента. Напишите «продолжай», чтобы он продолжил."
                chat["messages"].append({"role": "assistant", "content": note.strip()})
                self._js("updateBotBubble", note)
            self._save_chat(chat)
            self._js("finishTurn", self.stop_flag)
            self._js("refreshChats", self._list_chats(), self.chat["id"])
        except Exception as e:
            msg = str(e)
            if "401" in msg:
                msg = "❌ **Ошибка 401:** неверный API-ключ. Проверьте его в настройках."
            elif "402" in msg:
                msg = "❌ **Ошибка 402:** недостаточно средств на балансе."
            elif "404" in msg:
                msg = "❌ **Ошибка 404:** модель не найдена."
            elif "429" in msg:
                msg = "❌ **Ошибка 429:** слишком много запросов, подождите немного."
            else:
                msg = "❌ **Ошибка:** " + msg
            self._repair(chat)
            self._save_chat(chat)
            self._js("finishTurn", True)
            self._js("addBotMessage", msg)
            self._js("refreshChats", self._list_chats(), self.chat["id"])
        finally:
            self.busy = False
            self._js("setBusy", False)

    
    def _run_tool(self, call):
        cid, name = call["id"], call["name"]
        try:
            args = json.loads(call["args"] or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except Exception:
            self._js("toolStart", cid, name, {}, False, False)
            res = "Ошибка: аргументы инструмента — некорректный JSON."
            self._js("toolDone", cid, res, False)
            return res

        forced = name == "run_command" and bool(FORCE_RE.search(str(args.get("command", ""))))
        need = forced or (name in DANGEROUS and self.config["approval"] == "ask" and not self.session_auto)
        self._js("toolStart", cid, name, args, need, forced)
        if need:
            ev = threading.Event()
            self.approvals[cid] = {"ev": ev, "d": None}
            while not ev.wait(0.2):
                if self.stop_flag:
                    self.approvals[cid]["d"] = "deny"
                    break
            decision = self.approvals.pop(cid)["d"]
            if decision == "all":
                self.session_auto = True
            if decision == "deny":
                res = "Ошибка: пользователь отклонил выполнение."
                self._js("toolDone", cid, res, False)
                return res
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            res = f"Ошибка: неизвестный инструмент {name}."
            self._js("toolDone", cid, res, False)
            return res
        try:
            ok, out = fn(args)
        except Exception as e:
            ok, out = False, f"{type(e).__name__}: {e}"
        out = _clip(out, 14000)
        res = out if ok else "Ошибка: " + out
        self._js("toolDone", cid, res, ok)
        return res

    def _resolve(self, p):
        p = os.path.expanduser(str(p or "."))
        if not os.path.isabs(p):
            p = os.path.join(self.workdir(), p)
        return os.path.normpath(p)

    def t_run_command(self, a):
        cmd = str(a.get("command", "")).strip()
        if not cmd:
            return False, "пустая команда"
        try:
            timeout = max(1, min(int(a.get("timeout", 60)), 600))
        except Exception:
            timeout = 60
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        kw = {"start_new_session": True} if os.name != "nt" else {}
        proc = subprocess.Popen(cmd, shell=True, cwd=self.workdir(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, **kw)
        t0 = time.time()
        while True:
            try:
                raw, _ = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if self.stop_flag:
                    _kill(proc)
                    return False, "прервано пользователем"
                if time.time() - t0 > timeout:
                    _kill(proc)
                    try:
                        raw, _ = proc.communicate(timeout=3)
                    except Exception:
                        raw = b""
                    return False, f"таймаут {timeout} с\n" + _decode(raw or b"")
        return proc.returncode == 0, f"код возврата: {proc.returncode}\n" + _decode(raw or b"")

    def t_read_file(self, a):
        p = self._resolve(a.get("path"))
        if not os.path.isfile(p):
            return False, f"файл не найден: {p}"
        if os.path.splitext(p)[1].lower() in IMG_EXT:
            return False, "это изображение — приложите его к чату, чтобы модель его увидела"
        kind, text, note = extract_file_text(p)
        if kind == "binary":
            return False, note or "бинарный файл"
        lines = text.splitlines()
        off = max(1, int(a.get("offset") or 1))
        lim = int(a.get("limit") or 0)
        sel = lines[off - 1: off - 1 + lim] if lim else lines[off - 1:]
        body = "\n".join(sel)
        head = f"[{p}] строк всего: {len(lines)}\n"
        return True, head + _clip(body, 30000)

    def t_write_file(self, a):
        p = self._resolve(a.get("path"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        content = str(a.get("content", ""))
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return True, f"записано {len(content.encode('utf-8'))} байт → {p}"

    def t_edit_file(self, a):
        p = self._resolve(a.get("path"))
        if not os.path.isfile(p):
            return False, f"файл не найден: {p}"
        with open(p, "r", encoding="utf-8") as f:
            s = f.read()
        old, new = str(a.get("old_text", "")), str(a.get("new_text", ""))
        n = s.count(old)
        if not old or n == 0:
            return False, "old_text не найден в файле"
        if n > 1 and not a.get("replace_all"):
            return False, f"old_text встречается {n} раз — уточните фрагмент или укажите replace_all"
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(s.replace(old, new) if a.get("replace_all") else s.replace(old, new, 1))
        return True, f"заменено вхождений: {n if a.get('replace_all') else 1} в {p}"

    def t_list_dir(self, a):
        p = self._resolve(a.get("path") or ".")
        if not os.path.isdir(p):
            return False, f"папка не найдена: {p}"
        items = sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = [f"[{p}]"]
        for e in items[:300]:
            if e.is_dir():
                lines.append(f"[DIR]  {e.name}")
            else:
                try:
                    lines.append(f"       {e.name}  ({e.stat().st_size} Б)")
                except Exception:
                    lines.append(f"       {e.name}")
        if len(items) > 300:
            lines.append(f"…и ещё {len(items) - 300}")
        return True, "\n".join(lines)

    def t_search_files(self, a):
        import fnmatch
        try:
            rx = re.compile(str(a.get("pattern", "")), re.I)
        except re.error as e:
            return False, f"некорректное регулярное выражение: {e}"
        root = self._resolve(a.get("path") or ".")
        mask = a.get("glob") or "*"
        res = []
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv"}
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d not in skip]
            for f in fn:
                if not fnmatch.fnmatch(f, mask):
                    continue
                fp = os.path.join(dp, f)
                try:
                    if os.path.getsize(fp) > 2_000_000:
                        continue
                    with open(fp, "r", encoding="utf-8", errors="strict") as fh:
                        for i, line in enumerate(fh, 1):
                            if rx.search(line):
                                res.append(f"{fp}:{i}: {line.strip()[:200]}")
                                if len(res) >= 200:
                                    return True, "\n".join(res) + "\n…(лимит 200 совпадений)"
                except Exception:
                    continue
        return True, "\n".join(res) if res else "совпадений нет"

    def t_fetch_url(self, a):
        url = str(a.get("url", ""))
        if not re.match(r"https?://", url, re.I):
            return False, "разрешены только http/https ссылки"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru,en;q=0.8"})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read(3_000_000)
            ctype = r.headers.get("Content-Type", "")
        txt = raw.decode("utf-8", errors="replace")
        if "html" in ctype.lower() or txt.lstrip().lower().startswith(("<!doctype", "<html")):
            txt = html_to_text(txt)
        return True, _clip(txt, 15000)

    def t_web_search(self, a):
        q = str(a.get("query", "")).strip()
        if not q:
            return False, "пустой запрос"
        req = urllib.request.Request("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q),
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode("utf-8", errors="replace")
        out = []
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
                             page, re.S):
            href, title, snip = m.groups()
            if "uddg=" in href:
                href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
            out.append(f"{len(out) + 1}. {html_to_text(title)}\n   {href}\n   {html_to_text(snip)}")
            if len(out) >= 8:
                break
        return (True, "\n".join(out)) if out else (False, "ничего не найдено (поисковик мог отклонить запрос)")

HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>DeepSeek</title>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/tokyo-night-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
:root{--bg:#0B0D14;--panel:rgba(255,255,255,.035);--panel-hover:rgba(255,255,255,.065);--stroke:rgba(255,255,255,.08);--stroke-strong:rgba(255,255,255,.16);--brand:#4D6BFE;--brand-2:#8B5CF6;--brand-3:#22D3EE;--grad:linear-gradient(135deg,#4D6BFE 0%,#8B5CF6 100%);--text:#E8EAF2;--muted:#8B90A5;--danger:#F87171;--ok:#34D399;--warn:#FBBF24;--radius:16px}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;display:flex;overflow:hidden;-webkit-font-smoothing:antialiased}
button{font-family:inherit}
.aurora{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}
.aurora i{position:absolute;border-radius:50%;filter:blur(110px);opacity:.28;animation:float 22s ease-in-out infinite}
.aurora i:nth-child(1){width:560px;height:560px;background:#4D6BFE;top:-180px;left:10%}
.aurora i:nth-child(2){width:480px;height:480px;background:#8B5CF6;bottom:-160px;right:5%;animation-delay:-7s}
.aurora i:nth-child(3){width:360px;height:360px;background:#22D3EE;top:40%;left:55%;opacity:.14;animation-delay:-13s}
@keyframes float{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(60px,-40px) scale(1.1)}66%{transform:translate(-50px,50px) scale(.92)}}
.grain{position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.5;background-image:radial-gradient(rgba(255,255,255,.05) 1px,transparent 1px);background-size:28px 28px;mask-image:radial-gradient(ellipse at center,#000 30%,transparent 75%);-webkit-mask-image:radial-gradient(ellipse at center,#000 30%,transparent 75%)}

.logo-tile{display:grid;place-items:center;background:#4D6BFE;overflow:hidden;flex-shrink:0}
.logo-tile img{width:100%;height:100%;object-fit:contain;padding:11%;display:block}
.logo-tile.fallback{background:var(--grad)}
.logo-tile.fallback svg{width:55%;height:55%;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

/* сайдбар */
#sidebar{position:relative;z-index:2;width:288px;flex-shrink:0;padding:20px 14px 14px;display:flex;flex-direction:column;background:rgba(12,14,22,.72);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border-right:1px solid var(--stroke);transition:margin-left .35s cubic-bezier(.4,0,.2,1),opacity .3s}
body.collapsed #sidebar{margin-left:-288px;opacity:0}
.brand{display:flex;align-items:center;gap:12px;margin:0 6px 18px}
.brand .logo-tile{width:40px;height:40px;border-radius:12px;box-shadow:0 8px 28px rgba(77,107,254,.4)}
.brand-name{font-size:19px;font-weight:700;letter-spacing:-.02em}
.brand-name small{display:block;font-size:11px;font-weight:500;color:var(--muted);letter-spacing:0;margin-top:1px}
.btn-new{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:12px;border-radius:12px;border:0;cursor:pointer;background:var(--grad);color:#fff;font:600 14px 'Inter';box-shadow:0 8px 24px rgba(77,107,254,.35);transition:transform .15s,box-shadow .2s,filter .2s;margin-bottom:14px}
.btn-new:hover{transform:translateY(-1px);filter:brightness(1.08);box-shadow:0 12px 30px rgba(77,107,254,.5)}
.btn-new:active{transform:scale(.98)}
.btn-new svg{width:16px;height:16px;stroke:#fff;fill:none;stroke-width:2.4;stroke-linecap:round}
#chats{flex:1;overflow-y:auto;margin:0 -6px;padding:0 6px}
#chats::-webkit-scrollbar{width:6px}#chats::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:6px}
.group{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:14px 8px 6px}
.empty{color:var(--muted);font-size:13px;text-align:center;padding:28px 10px;line-height:1.5}
.chat-item{display:flex;align-items:center;gap:4px;padding:9px 8px 9px 12px;border-radius:11px;cursor:pointer;margin-bottom:2px;font-size:13.5px;color:#C3C7DA;border:1px solid transparent;transition:background .18s,border-color .18s}
.chat-item:hover{background:var(--panel-hover)}
.chat-item.active{background:rgba(77,107,254,.14);border-color:rgba(77,107,254,.35);color:#fff}
.chat-item .ct{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-item input{flex:1;min-width:0;background:rgba(0,0,0,.3);border:1px solid var(--brand);border-radius:7px;color:#fff;font:500 13.5px 'Inter';padding:3px 7px;outline:none}
.ci-actions{display:none;gap:2px}
.chat-item:hover .ci-actions,.chat-item.active .ci-actions{display:flex}
.mini{width:26px;height:26px;border-radius:7px;border:0;background:transparent;color:var(--muted);cursor:pointer;display:grid;place-items:center;transition:.15s}
.mini:hover{background:rgba(255,255,255,.1);color:#fff}
.mini.armed{background:rgba(248,113,113,.2);color:var(--danger)}
.mini svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.side-foot{margin-top:10px;padding-top:12px;border-top:1px solid var(--stroke);display:flex;align-items:center;gap:10px}
.user{flex:1;min-width:0;display:flex;align-items:center;gap:10px;padding:6px;border-radius:12px;cursor:pointer;transition:.2s}
.user:hover{background:var(--panel-hover)}
.user-ava{width:34px;height:34px;border-radius:10px;background:var(--grad);display:grid;place-items:center;font-weight:700;font-size:14px;flex-shrink:0}
.user-info{min-width:0;line-height:1.25}
.user-info b{display:block;font-size:13px;font-weight:600}
.user-info span{display:block;font-size:11.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ok);flex-shrink:0;animation:pulse 2.2s infinite}
body.busy .dot{background:var(--brand-3);animation-duration:.8s}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.6)}70%,100%{box-shadow:0 0 0 8px rgba(52,211,153,0)}}

/* основная часть */
#main{position:relative;z-index:1;flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{height:56px;flex-shrink:0;display:flex;align-items:center;gap:10px;padding:0 16px;border-bottom:1px solid var(--stroke);background:rgba(11,13,20,.55);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}
.icon-btn{width:36px;height:36px;border-radius:10px;border:0;background:transparent;color:var(--muted);cursor:pointer;display:grid;place-items:center;transition:.2s;flex-shrink:0}
.icon-btn:hover,.icon-btn.on{background:var(--panel-hover);color:var(--text)}
.icon-btn svg{width:19px;height:19px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.top-title{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.chip-model{margin-left:auto;font:500 12px 'JetBrains Mono',monospace;color:var(--muted);padding:6px 12px;border-radius:999px;border:1px solid var(--stroke);background:var(--panel);cursor:pointer;transition:.2s;white-space:nowrap;max-width:260px;overflow:hidden;text-overflow:ellipsis}
.chip-model:hover{border-color:var(--stroke-strong);color:var(--text)}
#scroll{flex:1;overflow-y:auto}
#scroll::-webkit-scrollbar{width:10px}
#scroll::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:10px;border:3px solid transparent;background-clip:padding-box}
#chat{max-width:820px;margin:0 auto;padding:32px 24px 240px;display:flex;flex-direction:column;gap:28px}

#welcome{position:absolute;top:56px;left:0;right:0;bottom:0;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:0 24px 150px;transition:opacity .4s,transform .4s}
#welcome.hide{opacity:0;transform:translateY(-14px);pointer-events:none}
.hero-mark{width:84px;height:84px;border-radius:24px;margin-bottom:26px;box-shadow:0 20px 60px rgba(77,107,254,.5);animation:bob 5s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-8px)}}
.hero-title{font-size:38px;font-weight:700;letter-spacing:-.03em;text-align:center;margin-bottom:10px;background:linear-gradient(180deg,#fff 30%,#9AA3C7);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero-sub{color:var(--muted);font-size:15.5px;text-align:center;margin-bottom:34px}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;width:100%;max-width:660px}
.card{text-align:left;padding:16px 18px;border-radius:var(--radius);cursor:pointer;color:var(--text);background:var(--panel);border:1px solid var(--stroke);font:500 13.5px/1.45 'Inter';transition:.22s;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;inset:0;background:var(--grad);opacity:0;transition:opacity .25s}
.card:hover{transform:translateY(-3px);border-color:rgba(139,92,246,.5);box-shadow:0 14px 34px rgba(77,107,254,.18)}
.card:hover::before{opacity:.09}
.card>*{position:relative}
.card b{display:block;font-size:12px;color:var(--brand-3);margin-bottom:5px;font-weight:600;letter-spacing:.02em}
.card span{color:#C6CADC}

/* сообщения */
.msg{display:flex;gap:14px;animation:rise .35s cubic-bezier(.2,.8,.2,1)}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.msg.user{justify-content:flex-end}
.u-wrap{max-width:78%;display:flex;flex-direction:column;align-items:flex-end;gap:8px}
.u-atts{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end}
.u-atts img{max-width:200px;max-height:160px;border-radius:14px;border:1px solid var(--stroke-strong);object-fit:cover;display:block}
.u-file{display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:12px;background:var(--panel-hover);border:1px solid var(--stroke);font-size:12.5px;max-width:240px}
.u-file i{font:700 9.5px 'JetBrains Mono',monospace;font-style:normal;padding:4px 6px;border-radius:6px;background:rgba(77,107,254,.18);color:#A5B4FC}
.u-file span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.msg.user .bubble{padding:13px 18px;border-radius:20px 20px 6px 20px;background:var(--grad);color:#fff;font-size:15.5px;line-height:1.6;white-space:pre-wrap;word-break:break-word;box-shadow:0 10px 30px rgba(77,107,254,.3)}
.msg .avatar{width:34px;height:34px;border-radius:11px;margin-top:2px}
.bot-body{flex:1;min-width:0}
.bot-content{font-size:15.5px;line-height:1.75;color:#DADDEB;word-break:break-word;margin-bottom:14px}
.bot-content>*:first-child{margin-top:0}.bot-content>*:last-child{margin-bottom:0}
.bot-content p{margin:0 0 14px}
.bot-content h1,.bot-content h2,.bot-content h3,.bot-content h4{color:#fff;margin:24px 0 10px;letter-spacing:-.015em;line-height:1.3}
.bot-content h1{font-size:24px}.bot-content h2{font-size:20px}.bot-content h3{font-size:17px}
.bot-content strong{color:#fff;font-weight:600}
.bot-content a{color:var(--brand-3);text-decoration:none;border-bottom:1px solid rgba(34,211,238,.35)}
.bot-content ul,.bot-content ol{margin:0 0 14px;padding-left:24px}
.bot-content li{margin:4px 0}.bot-content li::marker{color:var(--brand)}
.bot-content blockquote{margin:0 0 14px;padding:8px 16px;border-left:3px solid var(--brand-2);background:rgba(139,92,246,.07);border-radius:0 10px 10px 0;color:#BFC4DE}
.bot-content hr{border:0;border-top:1px solid var(--stroke);margin:22px 0}
.bot-content table{border-collapse:separate;border-spacing:0;margin:0 0 16px;width:100%;display:block;overflow-x:auto;border:1px solid var(--stroke);border-radius:12px}
.bot-content th,.bot-content td{padding:10px 14px;text-align:left;border-bottom:1px solid var(--stroke);white-space:nowrap}
.bot-content th{background:var(--panel);color:#fff;font-weight:600;font-size:13px}
.bot-content tr:last-child td{border-bottom:0}
.bot-content :not(pre)>code{font:500 13.2px 'JetBrains Mono',monospace;background:rgba(139,92,246,.14);color:#C4B5FD;padding:2px 7px;border-radius:7px;border:1px solid rgba(139,92,246,.22)}
.codebox{margin:0 0 16px;border-radius:14px;overflow:hidden;border:1px solid var(--stroke);background:#0F1119}
.codebar{display:flex;align-items:center;justify-content:space-between;padding:8px 8px 8px 16px;background:rgba(255,255,255,.04);border-bottom:1px solid var(--stroke)}
.codelang{font:500 12px 'JetBrains Mono',monospace;color:var(--muted)}
.copybtn,.act{display:flex;align-items:center;gap:6px;padding:5px 10px;border-radius:8px;border:0;cursor:pointer;background:transparent;color:var(--muted);font:500 12px 'Inter';transition:.2s}
.copybtn:hover,.act:hover{background:var(--panel-hover);color:var(--text)}
.copybtn.done,.act.done{color:var(--ok)}
.copybtn svg,.act svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.codebox pre{margin:0;padding:16px 18px;overflow-x:auto}
.codebox pre code{font:400 13.5px/1.65 'JetBrains Mono',monospace;background:transparent!important;padding:0!important}
.codebox pre::-webkit-scrollbar{height:8px}.codebox pre::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);border-radius:8px}
.think{margin-bottom:14px;border-radius:14px;border:1px solid var(--stroke);background:rgba(255,255,255,.025);overflow:hidden}
.think summary{list-style:none;cursor:pointer;padding:10px 14px;font-size:13px;font-weight:500;color:var(--muted);display:flex;align-items:center;gap:9px;user-select:none}
.think summary::-webkit-details-marker{display:none}
.think summary::after{content:'';margin-left:auto;width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(45deg);transition:transform .2s}
.think[open] summary::after{transform:rotate(-135deg)}
.think .spark{width:8px;height:8px;border-radius:50%;background:var(--brand-3);animation:pulse2 1.2s infinite}
.think.done .spark{animation:none;background:var(--muted)}
@keyframes pulse2{50%{opacity:.3;transform:scale(.7)}}
.think-text{padding:12px 16px 14px;font-size:13.5px;line-height:1.7;color:#8E93AB;white-space:pre-wrap;max-height:260px;overflow-y:auto;border-top:1px dashed var(--stroke)}
.actions{display:flex;gap:4px;margin-top:6px;opacity:0;transition:opacity .2s}
.msg:hover .actions{opacity:1}
.typing{display:inline-flex;gap:5px;padding:10px 0}
.typing span{width:7px;height:7px;border-radius:50%;background:var(--brand);animation:bounce 1.3s infinite ease-in-out both}
.typing span:nth-child(1){animation-delay:-.32s}.typing span:nth-child(2){animation-delay:-.16s}
@keyframes bounce{0%,80%,100%{transform:scale(.4);opacity:.4}40%{transform:scale(1);opacity:1}}
.cursor>*:last-child::after{content:'';display:inline-block;width:8px;height:16px;margin-left:3px;background:var(--brand);border-radius:2px;vertical-align:text-bottom;animation:blink 1s steps(2) infinite}
@keyframes blink{50%{opacity:0}}
.stopnote{color:var(--muted);font-size:13px;margin:6px 0 0}

/* карточки инструментов агента */
.tool{margin:0 0 12px;border-radius:14px;border:1px solid var(--stroke);background:rgba(255,255,255,.03);overflow:hidden;animation:rise .3s}
.tool.pending{border-color:rgba(251,191,36,.55);box-shadow:0 0 0 3px rgba(251,191,36,.08)}
.tool-head{display:flex;align-items:center;gap:10px;padding:10px 14px;font-size:13px}
.tool-ic{width:28px;height:28px;border-radius:9px;display:grid;place-items:center;background:rgba(77,107,254,.15);color:#A5B4FC;flex-shrink:0}
.tool-ic svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.tool-title{font-weight:600;white-space:nowrap}
.tool-sum{font:400 12.5px 'JetBrains Mono',monospace;color:var(--muted);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tool-st{width:16px;height:16px;flex-shrink:0;display:grid;place-items:center;font-size:14px;font-weight:700}
.tool.running .tool-st{border:2px solid rgba(255,255,255,.2);border-top-color:var(--brand-3);border-radius:50%;animation:rot .7s linear infinite}
.tool.pending .tool-st::after{content:'?';color:var(--warn)}
.tool.ok .tool-st::after{content:'✓';color:var(--ok)}
.tool.err .tool-st::after{content:'✕';color:var(--danger)}
.tool-body{padding:0 14px 12px}
.tool pre{margin:0;padding:10px 12px;border-radius:10px;background:#0F1119;border:1px solid var(--stroke);font:400 12.5px/1.6 'JetBrains Mono',monospace;color:#C8CCE0;white-space:pre-wrap;word-break:break-word;max-height:240px;overflow:auto}
.tool pre+pre{margin-top:8px}
.tool details{padding:0 14px 12px}
.tool details summary{cursor:pointer;font-size:12px;color:var(--muted);padding:2px 0 8px;user-select:none}
.approve{display:flex;gap:8px;flex-wrap:wrap;padding:0 14px 14px;align-items:center}
.approve .warn{font-size:12.5px;color:#FCD34D;flex:1 1 100%}
.ab{padding:8px 15px;border-radius:10px;border:0;cursor:pointer;font:600 12.5px 'Inter';transition:.2s}
.ab.allow{background:var(--grad);color:#fff}.ab.allow:hover{filter:brightness(1.1)}
.ab.deny{background:var(--panel-hover);color:var(--text);border:1px solid var(--stroke)}.ab.deny:hover{border-color:var(--danger);color:var(--danger)}
.ab.all{background:transparent;color:var(--muted);border:1px dashed var(--stroke-strong)}.ab.all:hover{color:var(--text)}
@keyframes rot{to{transform:rotate(360deg)}}

/* док: панель управления + ввод */
#dock{position:absolute;left:0;right:0;bottom:0;padding:34px 24px 20px;background:linear-gradient(to top,var(--bg) 50%,transparent);pointer-events:none}
.tools-row{pointer-events:auto;max-width:820px;margin:0 auto 10px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.tl-group{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pill{display:flex;align-items:center;gap:8px;padding:8px 13px;border-radius:999px;border:1px solid var(--stroke-strong);background:rgba(24,27,40,.82);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);color:var(--text);font:600 12.5px 'Inter';cursor:pointer;transition:.2s;white-space:nowrap}
.pill:hover{border-color:rgba(77,107,254,.6);background:rgba(34,38,58,.92)}
.pill svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}
.pill .mn{max-width:230px;overflow:hidden;text-overflow:ellipsis}
.pill .chev{opacity:.6}
.badge{font:700 9.5px 'Inter';letter-spacing:.05em;padding:2px 6px;border-radius:6px;display:inline-block}
.b-vis{background:rgba(34,211,238,.14);color:#67E8F9}
.b-tool{background:rgba(139,92,246,.2);color:#C4B5FD}
.b-free{background:rgba(52,211,153,.15);color:#6EE7B7}
.sw{width:30px;height:17px;border-radius:99px;background:rgba(255,255,255,.16);position:relative;transition:.25s;flex-shrink:0}
.sw::after{content:'';position:absolute;top:2px;left:2px;width:13px;height:13px;border-radius:50%;background:#fff;transition:.25s}
.pill.agent.on{border-color:rgba(139,92,246,.7);background:rgba(77,107,254,.2)}
.pill.agent.on .sw{background:var(--grad)}
.pill.agent.on .sw::after{left:15px}
#appr-btn,#wd-btn{display:none}
body.agent-on #appr-btn,body.agent-on #wd-btn{display:flex}
#appr-btn.auto{border-color:rgba(251,191,36,.6);color:#FCD34D}
#appr-btn.armed{border-color:var(--danger);color:var(--danger);background:rgba(248,113,113,.12)}
.composer{pointer-events:auto;max-width:820px;margin:0 auto;display:flex;flex-direction:column;padding:10px 10px 10px 12px;background:rgba(24,27,40,.86);backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px);border:1px solid var(--stroke-strong);border-radius:26px;box-shadow:0 18px 50px rgba(0,0,0,.45);transition:border-color .25s,box-shadow .25s}
.composer:focus-within{border-color:rgba(77,107,254,.7);box-shadow:0 18px 50px rgba(0,0,0,.45),0 0 0 4px rgba(77,107,254,.14)}
.composer-row{display:flex;align-items:flex-end;gap:6px}
#att-tray{display:none;flex-wrap:wrap;gap:8px;padding:2px 4px 10px}
#att-tray.has{display:flex}
.att{position:relative;display:flex;align-items:center;gap:9px;padding:6px 30px 6px 6px;border-radius:12px;background:var(--panel-hover);border:1px solid var(--stroke);max-width:250px}
.att img{width:40px;height:40px;border-radius:8px;object-fit:cover;display:block}
.att .fi{width:40px;height:40px;border-radius:8px;background:rgba(77,107,254,.16);display:grid;place-items:center;font:700 10px 'JetBrains Mono',monospace;color:#A5B4FC;text-transform:uppercase}
.att .an{font-size:12.5px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:150px}
.att .as{font-size:11px;color:var(--muted)}
.att .x{position:absolute;top:5px;right:5px;width:20px;height:20px;border-radius:6px;border:0;background:transparent;color:var(--muted);cursor:pointer;display:grid;place-items:center}
.att .x:hover{background:rgba(255,255,255,.12);color:#fff}
.att .x svg{width:12px;height:12px;stroke:currentColor;fill:none;stroke-width:2.6;stroke-linecap:round}
#input{flex:1;background:transparent;border:0;outline:0;resize:none;color:var(--text);font:400 15.5px/1.55 'Inter';max-height:200px;min-height:28px;padding:8px 0}
#input::placeholder{color:#6B7089}
#send{width:42px;height:42px;flex-shrink:0;border-radius:50%;border:0;cursor:pointer;background:var(--grad);color:#fff;display:grid;place-items:center;transition:transform .15s,filter .2s;box-shadow:0 6px 20px rgba(77,107,254,.45)}
#send:hover{transform:scale(1.07);filter:brightness(1.1)}#send:active{transform:scale(.94)}
#send svg{width:19px;height:19px;stroke:#fff;fill:none;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
#send .ic-stop{display:none;fill:#fff;stroke:none;width:15px;height:15px}
body.busy #send .ic-send{display:none}body.busy #send .ic-stop{display:block}
body.busy #send{background:#2B2F44;box-shadow:none;border:1px solid var(--stroke-strong)}
.hint{text-align:center;font-size:11.5px;color:#5E637C;margin-top:10px;pointer-events:auto}
.hint kbd{font:500 11px 'JetBrains Mono',monospace;padding:1px 6px;border-radius:5px;background:var(--panel);border:1px solid var(--stroke)}
#to-bottom{position:absolute;bottom:190px;left:50%;transform:translateX(-50%) scale(.8);width:38px;height:38px;border-radius:50%;border:1px solid var(--stroke-strong);background:#1B1E2E;color:var(--text);cursor:pointer;display:grid;place-items:center;opacity:0;pointer-events:none;transition:.25s;z-index:3}
#to-bottom.show{opacity:1;pointer-events:auto;transform:translateX(-50%) scale(1)}
#to-bottom svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
#toast{position:fixed;top:22px;left:50%;transform:translate(-50%,-90px);z-index:80;padding:10px 18px;border-radius:12px;font-size:13px;font-weight:500;background:#1B1E2E;border:1px solid var(--stroke-strong);box-shadow:0 12px 40px rgba(0,0,0,.5);transition:transform .35s cubic-bezier(.2,.9,.3,1.2);max-width:80vw;text-align:center}
#toast.show{transform:translate(-50%,0)}
#dropzone{position:fixed;inset:12px;z-index:70;display:none;align-items:center;justify-content:center;background:rgba(11,13,20,.82);backdrop-filter:blur(8px);border:2px dashed var(--brand);border-radius:24px;font-size:20px;font-weight:600;pointer-events:none}
#dropzone.show{display:flex}

/* выбор модели */
.popover{position:fixed;z-index:25;width:440px;max-width:calc(100vw - 32px);max-height:min(540px,72vh);display:none;flex-direction:column;border-radius:18px;background:rgba(20,23,35,.98);border:1px solid var(--stroke-strong);box-shadow:0 24px 80px rgba(0,0,0,.65);overflow:hidden}
.popover.open{display:flex;animation:pop .2s}
@keyframes pop{from{opacity:0;transform:translateY(14px) scale(.97)}to{opacity:1;transform:none}}
.pk-head{padding:12px 12px 8px;display:flex;flex-direction:column;gap:10px;border-bottom:1px solid var(--stroke)}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:4px;padding:4px;background:rgba(0,0,0,.25);border:1px solid var(--stroke);border-radius:13px}
.seg button{padding:9px;border:0;border-radius:10px;background:transparent;color:var(--muted);font:600 13px 'Inter';cursor:pointer;transition:.2s}
.seg button:hover{color:var(--text)}
.seg button.on{background:var(--grad);color:#fff;box-shadow:0 6px 18px rgba(77,107,254,.35)}
.pk-search{display:flex;gap:8px;align-items:center}
.pk-search input{flex:1;padding:10px 13px;border-radius:11px;background:var(--panel);border:1px solid var(--stroke);color:var(--text);font:500 13.5px 'Inter';outline:none}
.pk-search input:focus{border-color:var(--brand)}
.fchip{padding:8px 11px;border-radius:10px;border:1px solid var(--stroke);background:var(--panel);color:var(--muted);font:700 10.5px 'Inter';letter-spacing:.05em;cursor:pointer;transition:.2s}
.fchip:hover{color:var(--text)}
.fchip.on{background:rgba(77,107,254,.2);border-color:rgba(77,107,254,.6);color:#fff}
#pk-nokey{display:none;align-items:center;gap:10px;padding:10px 14px;background:rgba(251,191,36,.08);border-bottom:1px solid rgba(251,191,36,.25);font-size:12.5px;color:#FCD34D}
#pk-nokey button{margin-left:auto;padding:6px 12px;border-radius:9px;border:0;background:var(--warn);color:#1a1300;font:700 12px 'Inter';cursor:pointer}
#pk-list{overflow-y:auto;padding:6px}
#pk-list::-webkit-scrollbar{width:8px}#pk-list::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:8px}
.pk-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:12px;cursor:pointer;transition:.15s;border:1px solid transparent}
.pk-item:hover{background:var(--panel-hover)}
.pk-item.sel{background:rgba(77,107,254,.14);border-color:rgba(77,107,254,.4)}
.pk-info{flex:1;min-width:0}
.pk-name{font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pk-id{font:400 11px 'JetBrains Mono',monospace;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pk-badges{display:flex;gap:5px;flex-shrink:0}
.pk-foot{padding:9px 14px;border-top:1px solid var(--stroke);font-size:11.5px;color:var(--muted);display:flex;align-items:center;gap:10px}
.pk-foot button{margin-left:auto}

/* формы */
.field{margin-bottom:16px}
.field>label{display:block;font-size:12px;font-weight:500;color:var(--muted);margin:0 4px 7px}
.field select,.field input[type=text],.field input[type=password],.field input[type=number],.field textarea{width:100%;padding:12px 14px;border-radius:12px;font:500 14px 'Inter';background:var(--panel);color:var(--text);border:1px solid var(--stroke);outline:none;transition:border-color .2s,background .2s,box-shadow .2s;appearance:none;-webkit-appearance:none}
.field textarea{resize:vertical;min-height:92px;max-height:220px;line-height:1.55;font-weight:400}
.field select{background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238B90A5' stroke-width='3' stroke-linecap='round'><path d='M6 9l6 6 6-6'/></svg>");background-repeat:no-repeat;background-position:right 14px center;padding-right:36px;cursor:pointer}
.field select option{background:#14161F;color:var(--text)}
.field select:hover,.field input:hover,.field textarea:hover{background-color:var(--panel-hover)}
.field select:focus,.field input:focus,.field textarea:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(77,107,254,.18)}
.key-wrap{position:relative}
.key-wrap input{padding-right:46px!important;font-family:'JetBrains Mono',monospace!important;font-size:13px!important}
.key-wrap button{position:absolute;right:7px;top:50%;transform:translateY(-50%);width:32px;height:32px;background:transparent;border:0;border-radius:8px;color:var(--muted);cursor:pointer;display:grid;place-items:center}
.key-wrap button:hover{background:var(--panel-hover);color:var(--text)}
.key-wrap svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.range-row{display:flex;align-items:center;gap:14px}
.range-row output{min-width:38px;text-align:right;font:500 13px 'JetBrains Mono',monospace;color:var(--brand-3)}
input[type=range]{flex:1;-webkit-appearance:none;appearance:none;height:6px;border-radius:6px;background:rgba(255,255,255,.1);outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--grad);border:2px solid #fff;cursor:pointer;box-shadow:0 2px 10px rgba(77,107,254,.6)}
.help{font-size:12px;color:var(--muted);margin:7px 4px 0;line-height:1.5}
.err{color:var(--danger);font-size:13px;min-height:18px;margin:4px 4px 12px}
.btn-primary{width:100%;padding:13px;border-radius:13px;border:0;cursor:pointer;background:var(--grad);color:#fff;font:600 14.5px 'Inter';box-shadow:0 10px 28px rgba(77,107,254,.4);transition:transform .15s,filter .2s;display:flex;align-items:center;justify-content:center;gap:10px}
.btn-primary:hover{filter:brightness(1.08);transform:translateY(-1px)}.btn-primary:active{transform:scale(.98)}
.btn-primary:disabled{opacity:.6;cursor:default;transform:none}
.btn-ghost{padding:11px 16px;border-radius:12px;cursor:pointer;font:600 13.5px 'Inter';background:var(--panel);color:var(--text);border:1px solid var(--stroke);transition:.2s;white-space:nowrap}
.btn-ghost:hover{background:var(--panel-hover);border-color:var(--stroke-strong)}
.btn-ghost.danger{color:var(--danger)}
.btn-ghost.danger:hover{background:rgba(248,113,113,.1);border-color:rgba(248,113,113,.4)}
.btn-ghost.armed{background:rgba(248,113,113,.2);border-color:var(--danger);color:var(--danger)}
.btn-ghost.sm{padding:6px 11px;font-size:12px;border-radius:9px}
.spin{width:16px;height:16px;border-radius:50%;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;animation:rot .7s linear infinite;display:none}
.loading .spin{display:block}
.linkbtn{background:none;border:0;color:var(--brand-3);cursor:pointer;font:500 12.5px 'Inter';padding:0}
.linkbtn:hover{text-decoration:underline}
.seg2{display:grid;grid-template-columns:1fr 1fr;gap:4px;padding:4px;background:rgba(0,0,0,.25);border:1px solid var(--stroke);border-radius:13px}
.seg2 button{padding:10px;border:0;border-radius:10px;background:transparent;color:var(--muted);font:600 13.5px 'Inter';cursor:pointer;transition:.2s}
.seg2 button.on{background:var(--grad);color:#fff;box-shadow:0 6px 18px rgba(77,107,254,.35)}

/* логин */
#login{position:fixed;inset:0;z-index:40;display:none;align-items:center;justify-content:center;padding:24px;background:rgba(11,13,20,.6);backdrop-filter:blur(6px)}
#login.show{display:flex}
.login-card{width:100%;max-width:420px;padding:38px 34px 30px;border-radius:26px;background:rgba(20,23,35,.78);backdrop-filter:blur(28px);-webkit-backdrop-filter:blur(28px);border:1px solid var(--stroke-strong);box-shadow:0 30px 90px rgba(0,0,0,.6);animation:pop .5s cubic-bezier(.2,.9,.3,1.1)}
.login-mark{width:84px;height:84px;border-radius:24px;margin:0 auto 22px;box-shadow:0 20px 60px rgba(77,107,254,.5)}
.login-title{text-align:center;font-size:26px;font-weight:700;letter-spacing:-.02em;margin-bottom:6px}
.login-sub{text-align:center;font-size:14px;color:var(--muted);margin-bottom:26px;line-height:1.5}
.login-foot{text-align:center;margin-top:18px;font-size:12.5px;color:var(--muted);line-height:1.6}

/* настройки */
#settings{position:fixed;inset:0;z-index:30;display:flex;align-items:center;justify-content:center;padding:24px;background:rgba(5,6,10,.62);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);opacity:0;pointer-events:none;transition:opacity .25s}
#settings.open{opacity:1;pointer-events:auto}
.modal{width:100%;max-width:600px;max-height:100%;display:flex;flex-direction:column;border-radius:24px;background:rgba(20,23,35,.94);border:1px solid var(--stroke-strong);box-shadow:0 30px 100px rgba(0,0,0,.65);transform:translateY(18px) scale(.97);transition:transform .3s cubic-bezier(.2,.9,.3,1.1);overflow:hidden}
#settings.open .modal{transform:none}
.modal-head{display:flex;align-items:center;justify-content:space-between;padding:20px 24px;border-bottom:1px solid var(--stroke)}
.modal-head h2{font-size:18px;font-weight:700;letter-spacing:-.01em}
.modal-body{padding:22px 24px 8px;overflow-y:auto}
.modal-body::-webkit-scrollbar{width:8px}.modal-body::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:8px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.sep{height:1px;background:var(--stroke);margin:8px 0 20px}
.sec-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:0 4px 12px}
.modal-foot{display:flex;gap:10px;align-items:center;padding:16px 24px 20px;border-top:1px solid var(--stroke)}
.modal-foot .btn-primary{width:auto;flex:1}
.acct{display:flex;align-items:center;gap:12px;padding:14px;border-radius:14px;background:var(--panel);border:1px solid var(--stroke);margin-bottom:18px}
.acct .user-ava{width:40px;height:40px;border-radius:12px}
.acct-info{flex:1;min-width:0;line-height:1.5;font-size:12.5px;color:var(--muted)}
.acct-info b{color:var(--text);font-size:14px;display:block}
.acct-info span{font-family:'JetBrains Mono',monospace}
.inline{display:flex;gap:8px}
.inline input{flex:1}
@media(max-width:900px){.cards,.row2{grid-template-columns:1fr}.hero-title{font-size:30px}}
</style>
</head>
<body>
<div class="aurora"><i></i><i></i><i></i></div>
<div class="grain"></div>
<div id="dropzone">Отпустите файлы — я их изучу</div>

<!-- ЛОГИН -->
<div id="login">
  <div class="login-card">
    <div class="login-mark logo-tile" data-logo></div>
    <div class="login-title">Войти в DeepSeek</div>
    <div class="login-sub">Подключите аккаунт по API-ключу.<br>Ключ хранится только на этом компьютере.</div>
    <div class="field"><div class="seg" id="login-seg">
      <button class="on" data-p="DeepSeek" onclick="pickLoginProvider('DeepSeek')">DeepSeek</button>
      <button data-p="OpenRouter" onclick="pickLoginProvider('OpenRouter')">OpenRouter</button>
    </div></div>
    <div class="field"><label>API-ключ</label>
      <div class="key-wrap">
        <input type="password" id="login-key" placeholder="sk-..." autocomplete="off" spellcheck="false" onkeydown="if(event.key==='Enter')doLogin()">
        <button type="button" onclick="toggleKey('login-key')"><svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></button>
      </div>
    </div>
    <div class="err" id="login-err"></div>
    <button class="btn-primary" id="login-btn" onclick="doLogin()"><span class="spin"></span><span id="login-btn-t">Войти</span></button>
    <div class="login-foot"><button class="linkbtn" onclick="openKeyPage()">Где взять API-ключ?</button>
      <div style="margin-top:6px;opacity:.8">Модели со зрением (VISION) доступны через OpenRouter</div></div>
  </div>
</div>

<!-- НАСТРОЙКИ -->
<div id="settings" onclick="if(event.target===this)toggleSettings(false)">
  <div class="modal">
    <div class="modal-head"><h2>Настройки</h2>
      <button class="icon-btn" onclick="toggleSettings(false)" title="Закрыть (Esc)"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
    <div class="modal-body">
      <div class="acct">
        <div class="user-ava" id="acct-ava">D</div>
        <div class="acct-info"><b>Аккаунты</b><span id="acct-keys">—</span></div>
        <button class="btn-ghost danger" onclick="doLogout()">Выйти</button>
      </div>
      <div class="sec-title">API-ключи</div>
      <div class="field"><label>DeepSeek <span id="hint-DeepSeek" style="opacity:.7"></span></label>
        <div class="key-wrap"><input type="password" id="s-key-DeepSeek" placeholder="Оставьте пустым, чтобы не менять" autocomplete="off" spellcheck="false">
        <button type="button" onclick="toggleKey('s-key-DeepSeek')"><svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></button></div></div>
      <div class="field"><label>OpenRouter <span id="hint-OpenRouter" style="opacity:.7"></span></label>
        <div class="key-wrap"><input type="password" id="s-key-OpenRouter" placeholder="Оставьте пустым, чтобы не менять" autocomplete="off" spellcheck="false">
        <button type="button" onclick="toggleKey('s-key-OpenRouter')"><svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></button></div>
        <div class="help">Модель, агента и подтверждения переключаются прямо над полем ввода.</div></div>

      <div class="sep"></div>
      <div class="sec-title">Агент</div>
      <div class="field"><label>Рабочая папка (workspace)</label>
        <div class="inline"><input type="text" id="s-workdir" spellcheck="false">
        <button class="btn-ghost" onclick="pickWorkdir(true)">Обзор…</button>
        <button class="btn-ghost" onclick="api().open_workdir()">Открыть</button></div></div>
      <div class="row2">
        <div class="field"><label>Подтверждение действий</label>
          <select id="s-approval"><option value="ask">Спрашивать (запись, команды)</option><option value="auto">Авто — всё сам</option></select></div>
        <div class="field"><label>Макс. шагов за задачу</label><input type="number" id="s-steps" min="1" max="60"></div>
      </div>
      <div class="help" style="margin:-6px 4px 18px">Команды, содержащие явно опасные паттерны (rm -rf /, format, shutdown…), всегда требуют подтверждения.</div>

      <div class="sep"></div>
      <div class="sec-title">Поведение модели</div>
      <div class="field"><label>Системный промпт</label><textarea id="s-system" spellcheck="false"></textarea></div>
      <div class="field"><label>Температура</label>
        <div class="range-row"><input type="range" id="s-temp" min="0" max="2" step="0.1" oninput="$('s-temp-o').textContent=(+this.value).toFixed(1)"><output id="s-temp-o">0.7</output></div>
        <div class="help">Для reasoner / R1 не применяется.</div></div>

      <div class="sep"></div>
      <div class="sec-title">Данные</div>
      <div class="field" style="display:flex;align-items:center;gap:14px">
        <div class="help" style="flex:1;margin:0">Чаты и вложения хранятся локально в <span style="font-family:'JetBrains Mono',monospace">~/.deepseek_desktop</span></div>
        <button class="btn-ghost danger" id="del-all" onclick="deleteAll()">Удалить все чаты</button></div>
      <div class="err" id="s-err"></div>
    </div>
    <div class="modal-foot">
      <button class="btn-ghost" onclick="toggleSettings(false)">Отмена</button>
      <button class="btn-primary" id="s-save" onclick="saveSettings()"><span class="spin"></span><span>Сохранить</span></button>
    </div>
  </div>
</div>

<!-- ВЫБОР МОДЕЛИ -->
<div class="popover" id="picker">
  <div class="pk-head">
    <div class="seg" id="pk-tabs">
      <button data-p="DeepSeek" onclick="pickTab('DeepSeek')">DeepSeek</button>
      <button data-p="OpenRouter" onclick="pickTab('OpenRouter')">OpenRouter</button>
    </div>
    <div class="pk-search">
      <input type="text" id="pk-q" placeholder="Поиск модели или вставьте её ID…" oninput="pickerQ=this.value;renderPicker()" spellcheck="false">
      <button class="fchip" id="f-vis" onclick="toggleF('vis')">VISION</button>
      <button class="fchip" id="f-tool" onclick="toggleF('tool')">TOOLS</button>
    </div>
  </div>
  <div id="pk-nokey"><span id="pk-nokey-t">Нужен ключ</span><button onclick="toggleModelPicker(false);toggleSettings(true)">Добавить</button></div>
  <div id="pk-list"></div>
  <div class="pk-foot"><span id="pk-count"></span><button class="btn-ghost sm" id="pk-refresh" onclick="refreshModels()">Обновить список</button></div>
</div>

<!-- САЙДБАР -->
<aside id="sidebar">
  <div class="brand"><div class="logo-tile" data-logo style="width:40px;height:40px;border-radius:12px"></div>
    <div class="brand-name">DeepSeek<small>Desktop · Agent</small></div></div>
  <button class="btn-new" onclick="newChat()"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg> Новый чат</button>
  <div id="chats"></div>
  <div class="side-foot">
    <div class="user" onclick="toggleSettings()"><div class="user-ava" id="side-ava">D</div>
      <div class="user-info"><b id="side-name">DeepSeek</b><span id="side-status">Готов к работе</span></div></div>
    <div class="dot"></div>
    <button class="icon-btn" onclick="toggleSettings()" title="Настройки"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
  </div>
</aside>

<div id="main">
  <div class="topbar">
    <button class="icon-btn" onclick="document.body.classList.toggle('collapsed')" title="Боковая панель"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/></svg></button>
    <div class="top-title" id="top-title">Новый чат</div>
    <div class="chip-model" id="chip" onclick="toggleSettings(true)" title="Открыть настройки">—</div>
    <button class="icon-btn" id="gear" onclick="toggleSettings()" title="Настройки"><svg viewBox="0 0 24 24"><path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg></button>
  </div>

  <div id="welcome">
    <div class="hero-mark logo-tile" data-logo></div>
    <div class="hero-title">Чем я могу помочь?</div>
    <div class="hero-sub">Включите «Агент», и я сам выполню задачу на вашем компьютере</div>
    <div class="cards">
      <button class="card" onclick="quick(this)"><b>АГЕНТ</b><span>Создай в рабочей папке простой сайт-визитку (index.html + style.css) и проверь, что файлы на месте</span></button>
      <button class="card" onclick="quick(this)"><b>ИНСПЕКТ</b><span>Изучи приложенные файлы и расскажи, что в них не так</span></button>
      <button class="card" onclick="quick(this)"><b>КОД</b><span>Напиши скрипт на Python, который переименует файлы в папке по дате</span></button>
      <button class="card" onclick="quick(this)"><b>ОБЪЯСНИ</b><span>Объясни простыми словами, как работают нейросети</span></button>
    </div>
  </div>

  <div id="scroll"><div id="chat"></div></div>
  <button id="to-bottom" onclick="scrollToBottom(true)"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12l7 7 7-7"/></svg></button>

  <div id="dock">
    <div class="tools-row">
      <div class="tl-group">
        <button class="pill" id="model-btn" onclick="toggleModelPicker()" title="Выбрать модель">
          <svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="4"/><path d="M9 9h6v6H9z"/></svg>
          <span class="mn" id="model-name">—</span><span id="model-badges"></span>
          <svg class="chev" viewBox="0 0 24 24"><path d="M6 15l6-6 6 6"/></svg>
        </button>
        <button class="pill agent" id="agent-btn" onclick="toggleAgent()" title="Агент выполняет задачи сам: команды, файлы, интернет">
          <span class="sw"></span>Агент
        </button>
      </div>
      <div class="tl-group">
        <button class="pill" id="appr-btn" onclick="toggleApproval()" title="Подтверждать ли записи файлов и команды">
          <svg viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/></svg><span id="appr-t">Спрашивать</span>
        </button>
        <button class="pill" id="wd-btn" onclick="pickWorkdir()" title="Рабочая папка агента">
          <svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg><span class="mn" id="wd-t" style="max-width:150px">workspace</span>
        </button>
      </div>
    </div>
    <div class="composer">
      <div id="att-tray"></div>
      <div class="composer-row">
        <button class="icon-btn" id="attach" onclick="attachFiles()" title="Прикрепить изображения и любые файлы"><svg viewBox="0 0 24 24"><path d="M21 11.5l-8.6 8.6a5 5 0 0 1-7-7l8.6-8.6a3.3 3.3 0 0 1 4.7 4.7l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9"/></svg></button>
        <textarea id="input" rows="1" placeholder="Напишите сообщение..." oninput="autoResize(this)" onkeydown="handleKey(event)"></textarea>
        <button id="send" onclick="onSend()" title="Отправить / Остановить">
          <svg class="ic-send" viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
          <svg class="ic-stop" viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="3"/></svg>
        </button>
      </div>
    </div>
    <div class="hint"><kbd>Enter</kbd> — отправить &nbsp;·&nbsp; <kbd>Shift</kbd>+<kbd>Enter</kbd> — новая строка &nbsp;·&nbsp; файлы и скриншоты можно перетащить или вставить <kbd>Ctrl</kbd>+<kbd>V</kbd></div>
  </div>
</div>
<div id="toast"></div>

<script>
const $=(id)=>document.getElementById(id);
const api=()=>window.pywebview.api;
const ICON_COPY='<svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
const ICON_OK='<svg viewBox="0 0 24 24"><path d="M5 12l5 5L20 6"/></svg>';
const ICON_EDIT='<svg viewBox="0 0 24 24"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
const ICON_DEL='<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>';
const ICON_X='<svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>';
const FALLBACK_SVG='<svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>';
const TOOL_UI={
 run_command:['Команда','<path d="M4 17l6-6-6-6M12 19h8"/>'],
 read_file:['Чтение файла','<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'],
 write_file:['Запись файла','<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 14h6"/>'],
 edit_file:['Правка файла','<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'],
 list_dir:['Содержимое папки','<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'],
 search_files:['Поиск по файлам','<circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/>'],
 fetch_url:['Загрузка страницы','<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>'],
 web_search:['Поиск в интернете','<circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/>']
};

let S={models:{},key_hints:{}};
let LOGO='';
let busy=false, cur=null, stick=true, raf=null, loginProvider='DeepSeek', ACTIVE=null;
let pending=[];
let pickerTab='DeepSeek', pickerQ='', fVis=false, fTool=false;
marked.setOptions({breaks:true,gfm:true});

/* ---------- логотип ---------- */
function paintLogo(el){
  el.innerHTML=''; el.classList.toggle('fallback',!LOGO);
  if(LOGO){const i=document.createElement('img');i.src=LOGO;i.alt='';i.draggable=false;el.appendChild(i);} else el.innerHTML=FALLBACK_SVG;
}
function applyLogo(){document.querySelectorAll('[data-logo],.avatar.logo-tile').forEach(paintLogo);}
function logoEl(){const d=document.createElement('div');d.className='avatar logo-tile';paintLogo(d);return d;}

/* ---------- запуск ---------- */
window.addEventListener('pywebviewready',boot);
async function boot(){
  S=await api().get_state(); LOGO=S.logo; ACTIVE=S.active; applyLogo();
  if(!LOGO) api().fetch_logo().then(l=>{if(l){LOGO=l;applyLogo();}});
  if(!S.has_key){$('login').classList.add('show');setTimeout(()=>$('login-key').focus(),100);return;}
  $('login').classList.remove('show');
  pickerTab=S.provider; applyState(); renderChats(S.chats,S.active); resetChatView(); $('input').focus();
  if(S.keys_ok!==false && S.key_hints.OpenRouter && (S.models_age===null||S.models_age>86400)) refreshModels(true);
}
function curInfo(){return (S.models[S.provider]||[]).find(m=>m.id===S.model)||null;}
function badgesHTML(m){
  if(!m)return '';let h='';
  if(m.vision)h+='<span class="badge b-vis">VISION</span> ';
  if(m.tools)h+='<span class="badge b-tool">TOOLS</span> ';
  if(m.free)h+='<span class="badge b-free">FREE</span>';
  return h;
}
function applyState(){
  const mi=curInfo();
  $('chip').textContent=S.model;
  $('model-name').textContent=(mi&&mi.name)||S.model;
  $('model-badges').innerHTML=badgesHTML(mi);
  $('agent-btn').classList.toggle('on',!!S.agent);
  document.body.classList.toggle('agent-on',!!S.agent);
  const auto=S.approval==='auto';
  $('appr-btn').classList.toggle('auto',auto); $('appr-btn').classList.remove('armed');
  $('appr-t').textContent=auto?'Авто':'Спрашивать';
  $('wd-t').textContent=(S.workdir||'').split(/[\\/]/).filter(Boolean).pop()||S.workdir; $('wd-btn').title='Рабочая папка: '+S.workdir;
  $('side-name').textContent=S.provider; $('side-ava').textContent=$('acct-ava').textContent=(S.provider||'D')[0];
  $('input').placeholder=S.agent?'Поставьте задачу агенту…':'Напишите сообщение...';
  ['DeepSeek','OpenRouter'].forEach(p=>{$('hint-'+p).textContent=S.key_hints[p]?('· '+S.key_hints[p]):'· не задан';});
  $('acct-keys').textContent=['DeepSeek','OpenRouter'].map(p=>p+': '+(S.key_hints[p]||'—')).join('  ·  ');
}
function resetChatView(){$('chat').innerHTML='';cur=null;$('welcome').classList.remove('hide');$('top-title').textContent='Новый чат';}

/* ---------- логин ---------- */
function pickLoginProvider(p){loginProvider=p;document.querySelectorAll('#login-seg button').forEach(b=>b.classList.toggle('on',b.dataset.p===p));}
function openKeyPage(){api().open_url(loginProvider==='DeepSeek'?'https://platform.deepseek.com/api_keys':'https://openrouter.ai/keys');}
async function doLogin(){
  const key=$('login-key').value.trim(); $('login-err').textContent='';
  if(!key){$('login-err').textContent='Введите API-ключ.';return;}
  $('login-btn').classList.add('loading');$('login-btn').disabled=true;$('login-btn-t').textContent='Проверяю ключ…';
  const r=await api().login(loginProvider,key);
  $('login-btn').classList.remove('loading');$('login-btn').disabled=false;$('login-btn-t').textContent='Войти';
  if(!r.ok){$('login-err').textContent=r.error;return;}
  $('login-key').value=''; toggleSettings(false); await boot(); toast('✓ Вход выполнен');
}
async function doLogout(){await api().logout();toggleSettings(false);$('chats').innerHTML='';resetChatView();await boot();}
function toggleKey(id){const k=$(id);k.type=k.type==='password'?'text':'password';}

/* ---------- настройки ---------- */
function toggleSettings(force){
  const el=$('settings'); const open=force===undefined?!el.classList.contains('open'):force;
  if(open&&!S.has_key)return;
  if(open){
    toggleModelPicker(false);
    ['DeepSeek','OpenRouter'].forEach(p=>{$('s-key-'+p).value='';$('s-key-'+p).type='password';});
    $('s-workdir').value=S.workdir||''; $('s-approval').value=S.approval; $('s-steps').value=S.max_steps||25;
    $('s-system').value=S.system_prompt; $('s-temp').value=S.temperature; $('s-temp-o').textContent=(+S.temperature).toFixed(1);
    $('s-err').textContent=''; armReset($('del-all'),'Удалить все чаты');
  }
  el.classList.toggle('open',open); $('gear').classList.toggle('on',open);
}
async function saveSettings(){
  $('s-err').textContent=''; const btn=$('s-save'); btn.classList.add('loading'); btn.disabled=true;
  const r=await api().save_settings({DeepSeek:$('s-key-DeepSeek').value,OpenRouter:$('s-key-OpenRouter').value},
    $('s-system').value,$('s-temp').value,$('s-workdir').value,$('s-approval').value,$('s-steps').value);
  btn.classList.remove('loading'); btn.disabled=false;
  if(!r.ok){$('s-err').textContent=r.error;return;}
  Object.assign(S,r.state); applyState(); toggleSettings(false); toast('✓ Настройки сохранены');
  if(S.key_hints.OpenRouter&&(S.models_age===null||S.models_age>86400)) refreshModels(true);
}
let armT;
function armReset(btn,label){btn.classList.remove('armed');btn.textContent=label;clearTimeout(armT);}
async function deleteAll(){
  const b=$('del-all');
  if(!b.classList.contains('armed')){b.classList.add('armed');b.textContent='Точно удалить всё?';clearTimeout(armT);armT=setTimeout(()=>armReset(b,'Удалить все чаты'),3000);return;}
  armReset(b,'Удалить все чаты'); const r=await api().delete_all_chats();
  resetChatView(); renderChats(r.chats,r.active); toggleSettings(false); toast('Все чаты удалены');
}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){toggleSettings(false);toggleModelPicker(false);}});
document.addEventListener('mousedown',e=>{
  const p=$('picker'); if(p.classList.contains('open')&&!p.contains(e.target)&&!$('model-btn').contains(e.target)&&e.target!==$('chip'))toggleModelPicker(false);
});
let toastT;
function toast(t){const el=$('toast');el.textContent=t;el.classList.add('show');clearTimeout(toastT);toastT=setTimeout(()=>el.classList.remove('show'),2600);}

/* ---------- панель над чатом ---------- */
async function toggleAgent(){
  const on=!S.agent; const r=await api().set_agent(on); S.agent=r.agent; applyState();
  const mi=curInfo();
  if(on&&mi&&mi.tools===false) toast('⚠ У модели «'+mi.name+'» нет вызова инструментов — выберите модель с TOOLS');
  else if(on) toast('Агент включён: команды, файлы и интернет');
}
let apprT;
async function toggleApproval(){
  const b=$('appr-btn');
  if(S.approval==='ask'){
    if(!b.classList.contains('armed')){b.classList.add('armed');$('appr-t').textContent='Точно? Агент будет всё делать сам';clearTimeout(apprT);apprT=setTimeout(()=>applyState(),3200);return;}
    S.approval='auto';
  } else S.approval='ask';
  await api().set_approval(S.approval); applyState();
}
async function pickWorkdir(fromSettings){
  const r=await api().pick_workdir(); if(!r.ok)return;
  S.workdir=r.workdir; applyState(); if(fromSettings)$('s-workdir').value=r.workdir; toast('Рабочая папка: '+r.workdir);
}

/* ---------- выбор модели ---------- */
function toggleModelPicker(force){
  const p=$('picker'); const open=force===undefined?!p.classList.contains('open'):force;
  if(open){
    pickerTab=S.provider; pickerQ=''; $('pk-q').value='';
    const r=$('model-btn').getBoundingClientRect();
    p.style.left=Math.max(12,Math.min(r.left,window.innerWidth-p.offsetWidth-12))+'px';
    p.style.bottom=(window.innerHeight-r.top+10)+'px';
    renderPicker();
  }
  p.classList.toggle('open',open);
  if(open)setTimeout(()=>$('pk-q').focus(),50);
}
function pickTab(p){pickerTab=p;renderPicker();}
function toggleF(w){if(w==='vis'){fVis=!fVis;}else{fTool=!fTool;}renderPicker();}
function renderPicker(){
  document.querySelectorAll('#pk-tabs button').forEach(b=>b.classList.toggle('on',b.dataset.p===pickerTab));
  $('f-vis').classList.toggle('on',fVis); $('f-tool').classList.toggle('on',fTool);
  $('pk-refresh').style.display=pickerTab==='OpenRouter'?'':'none';
  const hasKey=!!S.key_hints[pickerTab];
  $('pk-nokey').style.display=hasKey?'none':'flex'; $('pk-nokey-t').textContent='Для '+pickerTab+' нужен API-ключ';
  const q=pickerQ.trim().toLowerCase(), list=$('pk-list'); list.innerHTML='';
  const all=S.models[pickerTab]||[];
  const items=all.filter(m=>(!fVis||m.vision)&&(!fTool||m.tools)&&(!q||m.id.toLowerCase().includes(q)||(m.name||'').toLowerCase().includes(q)));
  if(q.length>2&&!all.some(m=>m.id.toLowerCase()===q)){
    const it=document.createElement('div');it.className='pk-item';
    it.innerHTML='<div class="pk-info"><div class="pk-name"></div><div class="pk-id">Своя модель — введённый ID</div></div>';
    it.querySelector('.pk-name').textContent='Использовать «'+pickerQ.trim()+'»';
    it.onclick=()=>selectModel(pickerTab,pickerQ.trim()); list.appendChild(it);
  }
  items.slice(0,120).forEach(m=>{
    const it=document.createElement('div'); it.className='pk-item'+(pickerTab===S.provider&&m.id===S.model?' sel':'');
    it.innerHTML='<div class="pk-info"><div class="pk-name"></div><div class="pk-id"></div></div><div class="pk-badges">'+badgesHTML(m)+'</div>';
    it.querySelector('.pk-name').textContent=m.name||m.id; it.querySelector('.pk-id').textContent=m.id;
    it.onclick=()=>selectModel(pickerTab,m.id); list.appendChild(it);
  });
  if(!items.length&&!(q.length>2)){list.innerHTML='<div class="empty">Ничего не найдено</div>';}
  $('pk-count').textContent=items.length>120?('Показано 120 из '+items.length+' — уточните поиск'):('Моделей: '+items.length);
}
async function selectModel(p,id){
  const r=await api().set_model(p,id);
  if(!r.ok){ if(r.error==='no_key'){toast('Сначала добавьте ключ '+p);toggleModelPicker(false);toggleSettings(true);} return; }
  Object.assign(S,r.state); applyState(); toggleModelPicker(false);
  const mi=curInfo();
  if(S.agent&&mi&&mi.tools===false) toast('⚠ Эта модель без TOOLS — агент с ней не заработает');
}
async function refreshModels(silent){
  if(!S.key_hints.OpenRouter&&silent)return;
  const b=$('pk-refresh'); b.textContent='Загружаю…';
  const r=await api().refresh_models(); b.textContent='Обновить список';
  if(!r.ok){if(!silent)toast(r.error);return;}
  S.models=r.models; S.models_age=0; applyState(); if($('picker').classList.contains('open'))renderPicker(); if(!silent)toast('✓ Список моделей обновлён');
}

/* ---------- вложения ---------- */
function fmtSize(n){return n<1024?n+' Б':n<1048576?(n/1024).toFixed(1)+' КБ':(n/1048576).toFixed(1)+' МБ';}
function extOf(n){const m=/\.([A-Za-z0-9]+)$/.exec(n);return m?m[1].slice(0,4):'file';}
function renderTray(){
  const t=$('att-tray'); t.innerHTML=''; t.classList.toggle('has',pending.length>0);
  pending.forEach(a=>{
    const d=document.createElement('div'); d.className='att';
    if(a.kind==='image'&&a.thumb){const i=document.createElement('img');i.src=a.thumb;d.appendChild(i);}
    else{const f=document.createElement('div');f.className='fi';f.textContent=a.kind==='image'?'img':extOf(a.name);d.appendChild(f);}
    const w=document.createElement('div'); w.innerHTML='<div class="an"></div><div class="as"></div>';
    w.querySelector('.an').textContent=a.name; w.querySelector('.as').textContent=fmtSize(a.size); d.appendChild(w);
    const x=document.createElement('button'); x.className='x'; x.innerHTML=ICON_X; x.onclick=()=>{pending=pending.filter(p=>p.id!==a.id);api().remove_attachment(a.id);renderTray();};
    d.appendChild(x); t.appendChild(d);
  });
}
async function attachFiles(){
  const r=await api().attach_files(); if(!r.ok){toast('Не удалось открыть диалог: '+(r.errors||[]).join(', '));return;}
  pending.push(...r.files); renderTray(); (r.errors||[]).forEach(e=>toast('⚠ '+e));
  const mi=curInfo(); if(r.files.some(f=>f.kind==='image')&&mi&&mi.vision===false) toast('⚠ Текущая модель не видит изображения — выберите модель с VISION');
}
function handleFiles(files){
  [...files].forEach(file=>{
    if(file.size>40*1048576){toast('«'+file.name+'» больше 40 МБ');return;}
    const rd=new FileReader();
    rd.onload=async()=>{
      const b64=String(rd.result).split(',')[1]||''; const name=file.name||('paste.'+((file.type||'image/png').split('/')[1]||'png'));
      const r=await api().add_upload(name,file.type,b64);
      if(r.ok){pending.push(r.file);renderTray();const mi=curInfo();if(r.file.kind==='image'&&mi&&mi.vision===false)toast('⚠ Текущая модель не видит изображения — выберите модель с VISION');}
      else toast('⚠ '+r.error);
    };
    rd.readAsDataURL(file);
  });
}
let dragN=0;
window.addEventListener('dragenter',e=>{if(e.dataTransfer&&[...e.dataTransfer.types].includes('Files')){dragN++;$('dropzone').classList.add('show');}});
window.addEventListener('dragleave',()=>{dragN=Math.max(0,dragN-1);if(!dragN)$('dropzone').classList.remove('show');});
window.addEventListener('dragover',e=>e.preventDefault());
window.addEventListener('drop',e=>{e.preventDefault();dragN=0;$('dropzone').classList.remove('show');if(e.dataTransfer&&e.dataTransfer.files.length)handleFiles(e.dataTransfer.files);});
document.addEventListener('paste',e=>{
  const fs=[...(e.clipboardData?e.clipboardData.files:[])]; if(fs.length){e.preventDefault();handleFiles(fs);}
});

/* ---------- чаты ---------- */
function groupLabel(ts){
  const d=new Date(ts*1000),n=new Date(); const day=x=>new Date(x.getFullYear(),x.getMonth(),x.getDate()).getTime();
  const diff=Math.round((day(n)-day(d))/86400000);
  if(diff<=0)return 'Сегодня'; if(diff===1)return 'Вчера'; if(diff<7)return 'На этой неделе'; return 'Ранее';
}
function renderChats(chats,active){
  S.chats=chats; ACTIVE=active; S.active=active;
  const box=$('chats'); box.innerHTML='';
  if(!chats.length){box.innerHTML='<div class="empty">Здесь появятся ваши чаты.<br>Они сохраняются автоматически.</div>';return;}
  let last='';
  chats.forEach(c=>{
    const g=groupLabel(c.updated);
    if(g!==last){const h=document.createElement('div');h.className='group';h.textContent=g;box.appendChild(h);last=g;}
    const it=document.createElement('div'); it.className='chat-item'+(c.id===active?' active':'');
    it.innerHTML='<span class="ct"></span><div class="ci-actions"><button class="mini" title="Переименовать">'+ICON_EDIT+'</button><button class="mini" title="Удалить">'+ICON_DEL+'</button></div>';
    it.querySelector('.ct').textContent=c.title; it.onclick=()=>openChat(c.id);
    const [bRen,bDel]=it.querySelectorAll('.mini');
    bRen.onclick=e=>{e.stopPropagation();startRename(it,c);};
    bDel.onclick=e=>{e.stopPropagation();if(!bDel.classList.contains('armed')){bDel.classList.add('armed');setTimeout(()=>bDel.classList.remove('armed'),2500);return;}removeChat(c.id);};
    box.appendChild(it);
  });
}
function refreshChats(chats,active){renderChats(chats,active);const c=chats.find(x=>x.id===active);if(c)$('top-title').textContent=c.title;}
function startRename(it,c){
  const ct=it.querySelector('.ct'); const inp=document.createElement('input'); inp.value=c.title;
  ct.replaceWith(inp); it.querySelector('.ci-actions').style.display='none'; inp.focus(); inp.select(); let done=false;
  const finish=async(save)=>{
    if(done)return; done=true;
    if(save&&inp.value.trim()&&inp.value.trim()!==c.title){const r=await api().rename_chat(c.id,inp.value);if(r.ok){refreshChats(r.chats,r.active);return;}}
    renderChats(S.chats,S.active);
  };
  inp.onclick=e=>e.stopPropagation(); inp.onkeydown=e=>{if(e.key==='Enter')finish(true);if(e.key==='Escape')finish(false);}; inp.onblur=()=>finish(true);
}
async function removeChat(id){const r=await api().delete_chat(id);if(r.reset)resetChatView();renderChats(r.chats,r.active);}
async function newChat(){const r=await api().new_chat();pending=[];renderTray();resetChatView();renderChats(r.chats,r.active);$('input').focus();}
async function openChat(id){
  if(id===ACTIVE&&!busy)return;
  const r=await api().open_chat(id); if(!r.ok)return;
  renderHistory(r.messages); $('welcome').classList.toggle('hide',r.messages.length>0);
  renderChats(r.chats,r.active); const c=r.chats.find(x=>x.id===r.active); $('top-title').textContent=c?c.title:'Чат';
  stick=true; scrollToBottom(true);
}

/* ---------- отправка ---------- */
function quick(btn){$('input').value=btn.querySelector('span').textContent;autoResize($('input'));onSend();}
function autoResize(t){t.style.height='auto';t.style.height=Math.min(t.scrollHeight,200)+'px';}
function handleKey(e){if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();onSend();}}
function addUserMsg(text,atts){
  const m=document.createElement('div'); m.className='msg user'; const w=document.createElement('div'); w.className='u-wrap';
  if(atts&&atts.length){
    const box=document.createElement('div'); box.className='u-atts';
    atts.forEach(a=>{
      if(a.kind==='image'){const i=document.createElement('img');if(a.thumb)i.src=a.thumb;else if(a.path)api().get_image(a.path).then(u=>{if(u)i.src=u;else i.style.display='none';});box.appendChild(i);}
      else{const f=document.createElement('div');f.className='u-file';f.innerHTML='<i></i><span></span>';f.querySelector('i').textContent=extOf(a.name);f.querySelector('span').textContent=a.name;box.appendChild(f);}
    });
    w.appendChild(box);
  }
  if(text){const b=document.createElement('div');b.className='bubble';b.textContent=text;w.appendChild(b);}
  m.appendChild(w); $('chat').appendChild(m); return m;
}
async function onSend(){
  if(busy){api().stop();return;}
  const input=$('input'); const text=input.value.trim(); if(!text&&!pending.length)return;
  const atts=pending.slice(); input.value=''; input.style.height='auto'; pending=[]; renderTray();
  $('welcome').classList.add('hide'); const bubble=addUserMsg(text,atts);
  if($('top-title').textContent==='Новый чат')$('top-title').textContent=(text||atts[0].name).slice(0,48);
  stick=true; scrollToBottom(true);
  const r=await api().send_message(text,atts.map(a=>a.id));
  if(r.status!=='started'){
    bubble.remove(); input.value=text; autoResize(input); pending=atts; renderTray(); toast(r.error||'Подождите…');
    if(!$('chat').children.length)$('welcome').classList.remove('hide');
  }
}
function setBusy(v){busy=v;document.body.classList.toggle('busy',v);$('side-status').textContent=v?(S.agent?'Агент работает…':'Генерация ответа…'):'Готов к работе';}
const scroller=$('scroll');
scroller.addEventListener('scroll',()=>{const d=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight;stick=d<80;$('to-bottom').classList.toggle('show',d>260);});
function scrollToBottom(force){if(force||stick)scroller.scrollTo({top:scroller.scrollHeight,behavior:force?'smooth':'auto'});}

/* ---------- рендер ответов ---------- */
function buildBotMsg(){
  const msg=document.createElement('div'); msg.className='msg bot'; msg.appendChild(logoEl());
  const body=document.createElement('div'); body.className='bot-body'; msg.appendChild(body); $('chat').appendChild(msg); return msg;
}
function mdInto(el,raw){el.innerHTML=DOMPurify.sanitize(marked.parse(raw));enhance(el);}
function enhance(root){
  root.querySelectorAll('pre').forEach(pre=>{
    if(pre.parentNode.classList&&pre.parentNode.classList.contains('codebox'))return;
    const code=pre.querySelector('code'); if(!code)return;
    const m=(code.className||'').match(/language-([\w+-]+)/); const lang=m?m[1]:'text';
    hljs.highlightElement(code);
    const box=document.createElement('div'); box.className='codebox'; const bar=document.createElement('div'); bar.className='codebar';
    bar.innerHTML='<span class="codelang"></span><button class="copybtn">'+ICON_COPY+'<span>Копировать</span></button>';
    bar.querySelector('.codelang').textContent=lang; const btn=bar.querySelector('.copybtn'); btn.onclick=()=>copyText(code.innerText,btn);
    pre.parentNode.insertBefore(box,pre); box.appendChild(bar); box.appendChild(pre);
  });
}
function copyText(text,btn){
  const done=()=>{const old=btn.innerHTML;btn.classList.add('done');btn.innerHTML=ICON_OK+'<span>Скопировано</span>';setTimeout(()=>{btn.classList.remove('done');btn.innerHTML=old;},1500);};
  if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(text).then(done).catch(()=>fb(text,done));else fb(text,done);
}
function fb(text,cb){const t=document.createElement('textarea');t.value=text;document.body.appendChild(t);t.select();try{document.execCommand('copy');cb();}catch(e){}t.remove();}
function attachActions(body,raw){
  if(!raw.trim())return; const act=document.createElement('div'); act.className='actions';
  act.innerHTML='<button class="act">'+ICON_COPY+'<span>Копировать</span></button>'; act.firstChild.onclick=e=>copyText(raw.trim(),e.currentTarget); body.appendChild(act);
}

/* --- карточки инструментов --- */
function toolSummary(name,a){
  switch(name){
    case 'run_command':return '$ '+(a.command||'');
    case 'read_file':case 'write_file':case 'edit_file':case 'list_dir':return a.path||'.';
    case 'search_files':return (a.pattern||'')+'  в  '+(a.path||'.');
    case 'fetch_url':return a.url||'';
    case 'web_search':return a.query||'';
  } return JSON.stringify(a);
}
function buildToolCard(id,name,args,needs,forced){
  const ui=TOOL_UI[name]||[name,'<circle cx="12" cy="12" r="3"/>'];
  const card=document.createElement('div'); card.className='tool running'+(needs?' pending':''); card.dataset.id=id;
  card.innerHTML='<div class="tool-head"><div class="tool-ic"><svg viewBox="0 0 24 24">'+ui[1]+'</svg></div><div class="tool-title"></div><div class="tool-sum"></div><div class="tool-st"></div></div>';
  card.querySelector('.tool-title').textContent=ui[0]; card.querySelector('.tool-sum').textContent=toolSummary(name,args).replace(/\s+/g,' ').slice(0,300);
  let body=null;
  const addPre=(t)=>{if(!body){body=document.createElement('div');body.className='tool-body';card.appendChild(body);}const p=document.createElement('pre');p.textContent=t;body.appendChild(p);};
  if(name==='run_command'&&(args.command||'').length>70)addPre(args.command);
  if(name==='write_file')addPre((args.content||'').slice(0,2000)+((args.content||'').length>2000?'\n…':''));
  if(name==='edit_file'){addPre('− '+(args.old_text||'').slice(0,1200));addPre('+ '+(args.new_text||'').slice(0,1200));}
  if(needs){
    const ap=document.createElement('div'); ap.className='approve';
    if(forced)ap.innerHTML='<div class="warn">⚠ Потенциально опасная команда — требуется ваше подтверждение даже в режиме «Авто».</div>';
    else ap.innerHTML='<div class="warn">Агент хочет выполнить это действие. Разрешить?</div>';
    const mk=(cls,label,dec)=>{const b=document.createElement('button');b.className='ab '+cls;b.textContent=label;b.onclick=()=>{api().approve(id,dec);ap.remove();card.classList.remove('pending');};return b;};
    ap.appendChild(mk('allow','Разрешить','allow')); ap.appendChild(mk('deny','Отклонить','deny'));
    if(!forced)ap.appendChild(mk('all','Разрешить всё в этой задаче','all'));
    card.appendChild(ap);
  }
  return card;
}
function fillResult(card,res,ok){
  card.classList.remove('running','pending','ok','err'); card.classList.add(ok?'ok':'err');
  const ap=card.querySelector('.approve'); if(ap)ap.remove();
  if(card.querySelector('details'))return;
  const d=document.createElement('details'); d.innerHTML='<summary>Результат</summary><pre></pre>';
  d.querySelector('pre').textContent=String(res).slice(0,8000); if(!ok)d.open=true; card.appendChild(d);
}

/* --- живой ход агента --- */
function showTyping(){if(!cur||cur.typing)return;const t=document.createElement('div');t.className='typing';t.innerHTML='<span></span><span></span><span></span>';cur.body.appendChild(t);cur.typing=t;scrollToBottom();}
function hideTyping(){if(cur&&cur.typing){cur.typing.remove();cur.typing=null;}}
function closeThink(){if(cur&&cur.think){cur.think.classList.add('done');cur.think.querySelector('.tl').textContent='Ход мыслей';cur.think.open=false;cur.think=null;}}
function closeSeg(){
  if(!cur||!cur.seg)return; if(raf){cancelAnimationFrame(raf);raf=null;}
  cur.seg.classList.remove('cursor'); mdInto(cur.seg,cur.segRaw); cur.seg=null; cur.segRaw='';
}
function beginTurn(){
  const msg=buildBotMsg(); cur={msg,body:msg.querySelector('.bot-body'),seg:null,segRaw:'',all:'',think:null,thinkRaw:'',typing:null,cards:{}}; showTyping();
}
function updateThink(chunk){
  if(!cur)return; hideTyping();
  if(!cur.think){
    const d=document.createElement('details'); d.className='think'; d.open=true;
    d.innerHTML='<summary><span class="spark"></span><span class="tl">Размышляю…</span></summary><div class="think-text"></div>';
    cur.body.appendChild(d); cur.think=d; cur.thinkRaw='';
  }
  cur.thinkRaw+=chunk; const t=cur.think.querySelector('.think-text'); t.textContent=cur.thinkRaw; t.scrollTop=t.scrollHeight; scrollToBottom();
}
function updateBotBubble(chunk){
  if(!cur)return; closeThink(); hideTyping();
  if(!cur.seg){const d=document.createElement('div');d.className='bot-content';cur.body.appendChild(d);cur.seg=d;cur.segRaw='';}
  cur.segRaw+=chunk; cur.all+=chunk;
  if(!raf)raf=requestAnimationFrame(()=>{raf=null;if(!cur||!cur.seg)return;mdInto(cur.seg,cur.segRaw);cur.seg.classList.add('cursor');scrollToBottom();});
}
function toolStart(id,name,args,needs,forced){
  if(!cur)return; closeThink(); closeSeg(); hideTyping();
  const card=buildToolCard(id,name,args,needs,forced); cur.body.appendChild(card); cur.cards[id]=card; scrollToBottom(true);
}
function toolDone(id,res,ok){
  if(!cur)return; const card=cur.cards[id]; if(!card)return; fillResult(card,res,ok); if(busy)showTyping(); scrollToBottom();
}
function finishTurn(stopped){
  if(!cur)return; closeThink(); closeSeg(); hideTyping();
  if(stopped&&cur.body.children.length){const n=document.createElement('div');n.className='stopnote';n.textContent='⏹ Остановлено';cur.body.appendChild(n);}
  if(!cur.body.children.length)cur.msg.remove(); else attachActions(cur.body,cur.all);
  cur=null; scrollToBottom();
}
function addBotMessage(text){
  $('welcome').classList.add('hide'); const msg=buildBotMsg(); const c=document.createElement('div'); c.className='bot-content';
  mdInto(c,text); msg.querySelector('.bot-body').appendChild(c); stick=true; scrollToBottom(true);
}

/* --- история --- */
function renderHistory(msgs){
  $('chat').innerHTML=''; cur=null; let turn=null; const cards={};
  const close=()=>{if(turn){attachActions(turn.body,turn.text);turn=null;}};
  msgs.forEach(m=>{
    if(m.role==='user'){close();addUserMsg(m.display!==undefined?m.display:m.content,m.attachments||[]);return;}
    if(!turn){const msg=buildBotMsg();turn={body:msg.querySelector('.bot-body'),text:''};}
    if(m.role==='assistant'){
      if(m.content){const c=document.createElement('div');c.className='bot-content';mdInto(c,m.content);turn.body.appendChild(c);turn.text+=m.content+'\n\n';}
      (m.tool_calls||[]).forEach(tc=>{
        let a={};try{a=JSON.parse(tc.function.arguments||'{}');}catch(e){}
        const card=buildToolCard(tc.id,tc.function.name,a,false,false); card.classList.remove('running'); card.classList.add('ok'); turn.body.appendChild(card); cards[tc.id]=card;
      });
    } else if(m.role==='tool'){
      const card=cards[m.tool_call_id]; if(card)fillResult(card,m.content,!/^Ошибка/.test(m.content));
    }
  });
  close();
}
</script>
</body>
</html>
"""

def main():
    api = Api()
    window = webview.create_window(
        "DeepSeek",
        html=HTML,
        js_api=api,
        width=1320,
        height=880,
        min_size=(940, 640),
        background_color="#0B0D14",
        text_select=True,
    )
    api.window = window
    webview.start(debug=False)

if __name__ == "__main__":
    main()
