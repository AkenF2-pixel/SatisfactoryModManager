"""
Satisfactory Mod Manager - a small Windows app (Tkinter, standard library only).

Features
  - Auto-detects the game folder (Steam / Epic) or lets you browse for it
  - Browse and search mods on ficsit.app (the official mod repository)
  - One-click install with required dependencies (SML etc.)
  - See installed mods, enable / disable / uninstall them

Run:   python satisfactory_mod_manager.py
Build: pip install pyinstaller
       pyinstaller --onefile --windowed --name SatisfactoryModManager satisfactory_mod_manager.py
"""
import io
from html import unescape
import json
import os
import shutil
import string
import re
import subprocess
import threading
import datetime
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
import zipfile
from tkinter import filedialog, messagebox, simpledialog, ttk

import satsave
from extras import LIGHT, ExtrasMixin, is_elevation_error, style_toplevel
from savetabs import SaveTabsMixin

API = "https://api.ficsit.app"
GQL = API + "/v2/query"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SatModManager")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
HISTORY_FILE = os.path.join(CONFIG_DIR, "launch_history.json")
STEAM_APP_ID = "526870"


# ---------------------------------------------------------------- helpers
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            h = json.load(f)
    except Exception:
        h = {}
    h.setdefault("good", {})
    h.setdefault("launches", [])
    return h


def save_history(h):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    h["launches"] = h["launches"][-50:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=2)


def detect_game_dirs():
    rels = [
        r"Program Files (x86)\Steam\steamapps\common\Satisfactory",
        r"Program Files\Steam\steamapps\common\Satisfactory",
        r"SteamLibrary\steamapps\common\Satisfactory",
        r"Steam\steamapps\common\Satisfactory",
        r"Program Files\Epic Games\SatisfactoryEarlyAccess",
        r"Program Files\Epic Games\Satisfactory",
        r"Epic Games\SatisfactoryEarlyAccess",
        r"Epic Games\Satisfactory",
    ]
    for letter in string.ascii_uppercase:
        for rel in rels:
            p = f"{letter}:\\{rel}"
            if os.path.isdir(os.path.join(p, "FactoryGame")):
                return p
    return ""


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SatModManager/1.1"


def gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        GQL, data=body, headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ficsit.app returned HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach ficsit.app ({e.reason}). Check your internet / firewall.")
    if data.get("errors"):
        raise RuntimeError(data["errors"][0].get("message", "API error"))
    return data["data"]


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


Q_SEARCH = """
query($offset:Int!__SVAR__){
  getMods(filter:{__SF__limit:30,offset:$offset,order_by:__ORDER__,order:desc}){
    count
    mods{ id name mod_reference short_description downloads __VERS__ }
  }
}"""

Q_VERSION = """
query($ref:String!){
  getModByReference(modReference:$ref){
    id name mod_reference
    versions(filter:{limit:25,order_by:created_at,order:desc}){
      id version link game_version
      targets{ targetName link }
      dependencies{ mod_id condition optional }
    }
  }
}"""


VERS_FRAGMENT = "versions(filter:{limit:10,order_by:created_at,order:desc}){ version game_version dependencies{ mod_id optional } }"


def required_deps(ver):
    return [d["mod_id"] for d in (ver.get("dependencies") or [])
            if not d.get("optional") and d["mod_id"] != "FactoryGame"]


Q_DETAIL = """
query($ref:String!){
  getModByReference(modReference:$ref){
    name mod_reference short_description full_description views downloads source_url
    authors{ role user{ username } }
    versions(filter:{limit:8,order_by:created_at,order:desc}){ version game_version }
  }
}"""


def gql_ref(query, ref):
    try:
        return gql(query, {"ref": ref})
    except RuntimeError as e:
        if "ModReference" not in str(e) and "type" not in str(e).lower():
            raise
        return gql(query.replace("String!", "ModReference!"), {"ref": ref})


def clean_html(md):
    """Mod pages mix markdown with raw HTML/CSS: drop <style>/<script> blocks and comments, turn block tags
    into line breaks, strip the remaining tags and decode entities."""
    md = re.sub(r"(?is)<(style|script)\b.*?</\1\s*>", "", md)
    md = re.sub(r"(?s)<!--.*?-->", "", md)
    md = re.sub(r"(?is)<h([1-6])\b[^>]*>(.*?)</h\1\s*>", lambda m: "\n" + "#" * int(m.group(1)) + " " + m.group(2) + "\n", md)
    md = re.sub(r"(?i)<li\b[^>]*>", "\n- ", md)
    md = re.sub(r"(?i)<br\s*/?>|</(?:p|div|tr|ul|ol|table|section)\s*>", "\n", md)
    md = re.sub(r"</?[A-Za-z][^>]*>", "", md)
    return re.sub(r"\n{3,}", "\n\n", unescape(md)).strip()


def markdown_segments(md):
    """Very small markdown -> (text, tag) pieces for the Text widget."""
    md = clean_html(md)
    out, in_code = [], False
    for raw in md.replace("\r", "").split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            out.append((line + "\n", "code"))
            continue
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)            # images
        line = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1", line)      # links -> text
        line = re.sub(r"<[^>]+>", "", line)                            # html
        line = re.sub(r"(\*\*|__|`)", "", line)
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*)", line)
        if m:
            out.append((m.group(1) + "\n", "h"))
        elif re.match(r"^\s*[-*+]\s+", line):
            out.append(("\u2022 " + re.sub(r"^\s*[-*+]\s+", "", line) + "\n", "b"))
        elif line.strip():
            out.append((line + "\n", ""))
        else:
            out.append(("\n", ""))
    return out


def fetch_mod(mod_ref):
    try:
        d = gql(Q_VERSION, {"ref": mod_ref})["getModByReference"]
    except RuntimeError as e:
        if "ModReference" not in str(e) and "type" not in str(e).lower():
            raise
        d = gql(Q_VERSION.replace("String!", "ModReference!"), {"ref": mod_ref})["getModByReference"]
    if not d or not d["versions"]:
        raise RuntimeError(f"No downloadable version for '{mod_ref}'")
    return d


def detect_game_version(game_dir):
    """Return (changelist:int|None, branch:str). Reads Unreal's Build.version (what mods are checked against)."""
    cands = [os.path.join(game_dir, "Engine", "Build", "Build.version")]
    for sub in (("FactoryGame", "Binaries", "Win64"), ("FactoryGame", "Build"), ("Engine", "Binaries", "Win64")):
        d = os.path.join(game_dir, *sub)
        if os.path.isdir(d):
            cands += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.lower().endswith(".version")]
    for c in cands:
        try:
            with open(c, "r", encoding="utf-8-sig") as f:
                j = json.load(f)
            if j.get("Changelist"):
                return int(j["Changelist"]), str(j.get("BranchName", ""))
        except Exception:
            continue
    return None, ""


def satisfies(cl, constraint):
    """Is game changelist `cl` allowed by a ficsit.app game_version constraint like '>=365306'?"""
    constraint = (constraint or "").strip()
    if not constraint or constraint in ("*", "x"):
        return True
    for alt in constraint.split("||"):
        parts = re.findall(r"(>=|<=|>|<|=|\^|~)?\s*v?(\d+)", alt)
        if not parts:
            continue
        ok = True
        for op, num in parts:
            n = int(num)
            if op in ("", "="):
                ok &= cl == n
            elif op == ">=" or op in ("^", "~"):
                ok &= cl >= n
            elif op == ">":
                ok &= cl > n
            elif op == "<=":
                ok &= cl <= n
            elif op == "<":
                ok &= cl < n
        if ok:
            return True
    return False


def pick_version(versions, cl):
    """Newest version compatible with the game. Returns (version, is_compatible)."""
    if cl is None:
        return versions[0], True
    for v in versions:
        if satisfies(cl, v.get("game_version")):
            return v, True
    return versions[0], False


def read_uplugin(folder):
    for fn in os.listdir(folder):
        if fn.lower().endswith(".uplugin"):
            try:
                with open(os.path.join(folder, fn), "r", encoding="utf-8-sig") as f:
                    j = json.load(f)
                return j.get("FriendlyName") or fn[:-8], j.get("VersionName") or str(j.get("Version", "?"))
            except Exception:
                return fn[:-8], "?"
    return os.path.basename(folder), "?"


def read_plugin_deps(folder):
    """Names of non-optional, enabled plugins this mod's .uplugin lists as dependencies."""
    found = find_uplugin(folder)
    if not found:
        return []
    rel, plugin = found
    try:
        with open(os.path.join(folder, rel, plugin + ".uplugin"), "r", encoding="utf-8-sig") as f:
            j = json.load(f)
    except Exception:
        return []
    out = []
    for d in j.get("Plugins") or []:
        n = d.get("Name")
        if n and d.get("Enabled", True) and not d.get("Optional", False):
            out.append(n)
    return out


Q_EXISTS = "query($ref:String!){ getModByReference(modReference:$ref){ mod_reference } }"


def mod_exists_online(ref):
    """True/False if ficsit.app has a mod with this reference; None if we couldn't tell (offline etc.)."""
    try:
        return bool(gql_ref(Q_EXISTS, ref).get("getModByReference"))
    except RuntimeError as e:
        msg = str(e)
        if msg.startswith("Could not reach") or msg.startswith("ficsit.app returned HTTP"):
            return None
        return False  # API said "not found"


def find_uplugin(folder):
    """Return (relative_dir, plugin_name) of the shallowest .uplugin inside folder, or None."""
    for cur, _dirs, files in os.walk(folder):
        up = [f for f in files if f.lower().endswith(".uplugin")]
        if up:
            return os.path.relpath(cur, folder), up[0][:-8]
    return None


def check_structure(folder, folder_name):
    """Check the layout the SML manual-install guide requires. Returns (status, fixable)."""
    found = find_uplugin(folder)
    if not found:
        return "No .uplugin found", False
    rel, plugin = found
    if rel != ".":
        return "Nested folder (extra level)", True
    if plugin != folder_name:
        return f"Folder should be named {plugin}", True
    return "OK", False


# -------------------------------------------------------------------- app
class App(ExtrasMixin, SaveTabsMixin, tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Satisfactory Mod Manager")
        self.geometry("1240x700")
        self.minsize(980, 520)
        self.cfg = load_config()
        if not self.cfg.get("game_dir") or not os.path.isdir(self.cfg["game_dir"]):
            self.cfg["game_dir"] = detect_game_dirs()
        self.results = []
        self.offset = 0
        self.total = 0
        self.pal = LIGHT
        self.dark_var = tk.BooleanVar(value=self.cfg.get("dark_mode", False))
        self._build()
        self.refresh_game_version()
        self.refresh_installed()
        self.search(reset=True)
        self.start_auto_backup()
        self.setup_extras()

    # paths
    @property
    def mods_dir(self):
        return os.path.join(self.cfg["game_dir"], "FactoryGame", "Mods")

    @property
    def disabled_dir(self):
        return os.path.join(self.cfg["game_dir"], "FactoryGame", "Mods_disabled")

    def save_cfg(self):
        save_config(self.cfg)

    def need_game(self):
        if not self.cfg.get("game_dir") or not os.path.isdir(os.path.join(self.cfg["game_dir"], "FactoryGame")):
            messagebox.showwarning("Game folder", "Set your Satisfactory folder first (top right).")
            return False
        return True

    # ui
    def _build(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Game folder:").pack(side="left")
        self.path_var = tk.StringVar(value=self.cfg.get("game_dir", ""))
        ttk.Entry(top, textvariable=self.path_var, state="readonly").pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="\u25b6 Launch game", command=self.launch_game).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Browse...", command=self.browse).pack(side="left")
        ttk.Button(top, text="Install from file...", command=self.install_from_files).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Open Mods folder", command=self.open_mods).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(top, text="Dark mode", variable=self.dark_var, command=self.toggle_dark).pack(side="left", padx=(10, 0))
        self.admin_btn = ttk.Button(top, text="Restart as admin", command=self.restart_as_admin)
        self.admin_btn.pack(side="left", padx=(6, 0))

        vbar = ttk.Frame(self, padding=(8, 0, 8, 6))
        vbar.pack(fill="x")
        self.ver_var = tk.StringVar()
        ttk.Label(vbar, textvariable=self.ver_var, font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(vbar, text="Set manually...", command=self.set_game_version).pack(side="left", padx=8)

        nb = self.nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        # Browse tab
        b = ttk.Frame(nb, padding=6)
        nb.add(b, text="Browse mods")
        row = ttk.Frame(b)
        row.pack(fill="x")
        self.q = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.q)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda _: self.search(reset=True))
        ttk.Button(row, text="Search", command=lambda: self.search(reset=True)).pack(side="left", padx=6)
        self.list_title = tk.StringVar()
        ttk.Label(b, textvariable=self.list_title, style="Dim.TLabel").pack(fill="x", pady=(4, 0))
        self.btn_more = ttk.Button(row, text="Load more", command=lambda: self.search(reset=False))
        self.btn_more.pack(side="left")

        pane = ttk.PanedWindow(b, orient="horizontal")
        pane.pack(fill="both", expand=True, pady=6)

        left = ttk.Frame(pane)
        pane.add(left, weight=3)
        cols = ("name", "compat", "deps", "downloads")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c, t, w in (("name", "Name", 170), ("compat", "Compatible with your game", 170),
                        ("deps", "Required mods", 150), ("downloads", "Downloads", 70)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("ok", foreground="#1a7f37")
        self.tree.tag_configure("bad", foreground="#c62828")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda _: self.install_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.on_select())

        # ---- mod page (right side)
        right = ttk.Frame(pane, padding=(10, 0, 0, 0))
        pane.add(right, weight=2)
        self.d_title = tk.StringVar(value="Select a mod to see its page")
        ttk.Label(right, textvariable=self.d_title, font=("Segoe UI", 14, "bold"), wraplength=380).pack(anchor="w")
        self.d_meta = tk.StringVar()
        ttk.Label(right, textvariable=self.d_meta, style="Dim.TLabel", wraplength=380).pack(anchor="w", pady=(2, 6))
        btns = ttk.Frame(right)
        btns.pack(fill="x")
        self.btn_install = ttk.Button(btns, text="Install (with dependencies)", command=self.install_selected)
        self.btn_install.pack(side="left")
        ttk.Button(btns, text="Open on ficsit.app", command=self.open_page).pack(side="left", padx=6)
        self.d_compat = tk.StringVar()
        self.d_compat_lbl = ttk.Label(right, textvariable=self.d_compat, wraplength=380, font=("Segoe UI", 10, "bold"))
        self.d_compat_lbl.pack(anchor="w", pady=(8, 0))
        self.d_deps = tk.StringVar()
        ttk.Label(right, textvariable=self.d_deps, wraplength=380).pack(anchor="w", pady=(2, 6))
        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True)
        self.d_text = tk.Text(tf, wrap="word", relief="flat", borderwidth=0, padx=4, pady=4,
                              font=("Segoe UI", 10), state="disabled", height=10, width=40)
        tsb = ttk.Scrollbar(tf, orient="vertical", command=self.d_text.yview)
        self.d_text.configure(yscrollcommand=tsb.set)
        self.d_text.pack(side="left", fill="both", expand=True)
        tsb.pack(side="left", fill="y")
        self.d_text.tag_configure("h", font=("Segoe UI", 11, "bold"), spacing1=8, spacing3=2)
        self.d_text.tag_configure("b", lmargin1=12, lmargin2=24)
        self.d_text.tag_configure("dim", foreground="#777")
        self.d_text.tag_configure("code", font=("Consolas", 9), background="#f2f2f2")
        self._detail_token = 0

        # Installed tab
        i = ttk.Frame(nb, padding=6)
        nb.add(i, text="Installed")
        self._build_profile_row(i)
        icols = ("name", "ref", "version", "state", "check", "deps", "launch")
        self.itree = ttk.Treeview(i, columns=icols, show="headings", selectmode="extended")
        for c, t, w in (("name", "Name", 190), ("ref", "Folder", 130), ("version", "Version", 70), ("state", "State", 70), ("check", "Install check", 150), ("deps", "Dependencies", 190), ("launch", "Launch status", 170)):
            self.itree.heading(c, text=t)
            self.itree.column(c, width=w, anchor="w")
        self.itree.tag_configure("depbad", foreground="#c62828")
        self.itree.tag_configure("worked", foreground="#1a7f37")
        self.itree.tag_configure("untested", foreground="#b26a00")
        isb = ttk.Scrollbar(i, orient="vertical", command=self.itree.yview)
        self.itree.configure(yscrollcommand=isb.set)
        self.itree.pack(side="left", fill="both", expand=True)
        isb.pack(side="left", fill="y")
        iside = ttk.Frame(i)
        iside.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(iside, text="\u25b6 Launch game", command=self.launch_game).pack(fill="x")
        ttk.Button(iside, text="Disable untested mods", command=self.disable_untested).pack(fill="x", pady=6)
        ttk.Button(iside, text="Launch history", command=self.show_history).pack(fill="x", pady=(0, 10))
        self.dep_var = tk.BooleanVar(value=self.cfg.get("check_deps", True))
        ttk.Checkbutton(iside, text="Mark mods with\nmissing dependencies", variable=self.dep_var,
                        command=self.on_dep_option).pack(fill="x", pady=(0, 6))
        ttk.Button(iside, text="Install missing deps", command=self.install_missing_deps).pack(fill="x", pady=(0, 10))
        ttk.Button(iside, text="Enable", command=lambda: self.toggle(True)).pack(fill="x")
        ttk.Button(iside, text="Disable", command=lambda: self.toggle(False)).pack(fill="x", pady=6)
        ttk.Button(iside, text="Update", command=self.update_selected).pack(fill="x")
        ttk.Button(iside, text="Uninstall", command=self.uninstall).pack(fill="x", pady=6)
        ttk.Button(iside, text="Fix folder layout", command=self.fix_layout).pack(fill="x")
        ttk.Button(iside, text="Refresh / re-check", command=self.refresh_installed).pack(fill="x", pady=6)
        ttk.Button(iside, text="Open game logs", command=self.open_logs).pack(fill="x")
        ttk.Label(iside, wraplength=150, style="Dim.TLabel", text=(
            "To confirm mods are loaded: start the game, open the Mods menu on the main menu, "
            "and check each mod is listed. Folders with a problem in 'Install check' will not load."
        )).pack(fill="x", pady=(14, 0))

        self._build_save_tabs(self.nb)
        self.status = tk.StringVar(value="Ready - drop .sav saves, .smod mods or .smpack modpacks onto the window")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w", padding=4).pack(fill="x", side="bottom")

    def set_status(self, s):
        self.after(0, lambda: self.status.set(s))

    def run_bg(self, fn, done=None, fail=None):
        def work():
            try:
                r = fn()
                if done:
                    self.after(0, lambda: done(r))
            except Exception as ex:
                if fail:
                    self.after(0, lambda ex=ex: fail(ex))
                elif isinstance(ex, PermissionError):
                    self.after(0, lambda ex=ex: self.offer_admin(f"Windows blocked this action: {ex}"))
                else:
                    self.after(0, lambda ex=ex: (self.status.set("Error: " + str(ex)), messagebox.showerror("Error", str(ex))))
        threading.Thread(target=work, daemon=True).start()

    # game folder
    def browse(self):
        p = filedialog.askdirectory(title="Select your Satisfactory folder (contains FactoryGame)")
        if not p:
            return
        if not os.path.isdir(os.path.join(p, "FactoryGame")):
            messagebox.showerror("Not found", "That folder doesn't contain a 'FactoryGame' directory.")
            return
        self.cfg["game_dir"] = p
        save_config(self.cfg)
        self.path_var.set(p)
        self.refresh_game_version()
        self.refresh_installed()

    @property
    def game_cl(self):
        return self.cfg.get("game_version_override") or self._detected_cl

    def refresh_game_version(self):
        self._detected_cl, branch = (None, "")
        if self.cfg.get("game_dir"):
            self._detected_cl, branch = detect_game_version(self.cfg["game_dir"])
        if self.cfg.get("game_version_override"):
            self.ver_var.set(f"Game version: {self.cfg['game_version_override']} (set manually)")
        elif self._detected_cl:
            self.ver_var.set(f"Game version: {self._detected_cl}" + (f"  ({branch})" if branch else ""))
        else:
            self.ver_var.set("Game version: unknown - compatibility checks are off (use 'Set manually')")

    def set_game_version(self):
        v = simpledialog.askstring(
            "Game version",
            "Enter your game's build number (the number shown on ficsit.app / the mod manager, e.g. 365306).\n"
            "Leave empty to go back to auto-detect.", parent=self)
        if v is None:
            return
        v = v.strip()
        if v and not v.isdigit():
            messagebox.showerror("Game version", "Please enter digits only.")
            return
        if v:
            self.cfg["game_version_override"] = int(v)
        else:
            self.cfg.pop("game_version_override", None)
        save_config(self.cfg)
        self.refresh_game_version()

    def ask(self, title, msg):
        """Ask a yes/no question from a worker thread."""
        ev, box = threading.Event(), {}

        def show():
            box["r"] = messagebox.askyesno(title, msg, icon="warning")
            ev.set()
        self.after(0, show)
        ev.wait()
        return box["r"]

    def open_mods(self):
        if self.need_game():
            os.makedirs(self.mods_dir, exist_ok=True)
            os.startfile(self.mods_dir)

    # browse
    def search(self, reset):
        if reset:
            self.offset = 0
            self.results = []
            self.tree.delete(*self.tree.get_children())
        term, off = self.q.get().strip(), self.offset
        self.set_status("Loading mods from ficsit.app...")
        self.list_title.set(f"Results for \"{term}\"" if term else "Most popular mods")

        def work():
            orders = ["search", "popularity"] if term else ["popularity", "downloads"]
            last = None
            for vers in (VERS_FRAGMENT, ""):  # second pass: plain list if the version info is refused
                for o in orders:
                    try:
                        # the API rejects an empty search string, so leave the field out entirely when nothing is typed
                        q = Q_SEARCH.replace("__ORDER__", o).replace("__VERS__", vers)
                        q = q.replace("__SVAR__", ",$search:String!" if term else "").replace("__SF__", "search:$search," if term else "")
                        variables = {"offset": off, **({"search": term} if term else {})}
                        return gql(q, variables)["getMods"]
                    except RuntimeError as e:
                        last = e
            raise last

        def done(res):
            self.total = res["count"]
            for m in res["mods"]:
                self.results.append(m)
                compat, deps, tag = self.describe_mod(m)
                self.tree.insert("", "end", iid=str(len(self.results) - 1), tags=(tag,) if tag else (), values=(
                    m["name"], compat, deps, f'{m["downloads"]:,}'))
            self.offset += len(res["mods"])
            self.set_status(f"Showing {len(self.results)} of {self.total} mods")
            self.btn_more.state(["!disabled"] if self.offset < self.total else ["disabled"])

        def fail(ex):
            self.status.set("Could not load mods: " + str(ex))
            self.tree.insert("", "end", values=("(failed to load)", str(ex), "", ""))
        self.run_bg(work, done, fail)

    def describe_mod(self, m):
        """-> (compatibility text, dependency text, row tag) for the browse list."""
        vers = m.get("versions")
        if not vers:
            return "?", "?", ""
        cl = self.game_cl
        ver, ok = pick_version(vers, cl)
        deps = required_deps(ver)
        installed = {d for d in deps if os.path.isdir(os.path.join(self.mods_dir, d))} if self.cfg.get("game_dir") else set()
        dep_txt = "None" if not deps else ", ".join(d + (" (installed)" if d in installed else "") for d in deps)
        if cl is None:
            return f"Unknown (needs {ver.get('game_version') or 'any'})", dep_txt, ""
        if not ok:
            return f"No - needs {ver.get('game_version') or '?'}", dep_txt, "bad"
        if ver is not vers[0]:
            return f"Yes (older v{ver['version']})", dep_txt, "ok"
        return f"Yes (v{ver['version']})", dep_txt, "ok"

    def tree_refresh_deps(self):
        for i, m in enumerate(self.results):
            if self.tree.exists(str(i)):
                compat, deps, tag = self.describe_mod(m)
                self.tree.item(str(i), tags=(tag,) if tag else (), values=(
                    m["name"], compat, deps, f'{m["downloads"]:,}'))

    def on_select(self):
        m = self.selected_mod()
        if not m:
            return
        compat, deps, tag = self.describe_mod(m)
        self._detail_token += 1
        token = self._detail_token
        self.d_title.set(m["name"])
        self.d_meta.set(f'{m["mod_reference"]}  \u2022  {m["downloads"]:,} downloads')
        self.d_compat.set("Game compatibility: " + compat)
        self.d_compat_lbl.configure(foreground={"ok": self.pal["ok"], "bad": self.pal["err"]}.get(tag, self.pal["fg"]))
        self.d_deps.set("Requires: " + deps)
        self.set_detail_text([(m["short_description"] or "", ""), ("\n\nLoading full page...", "dim")])
        ref = m["mod_reference"]

        def work():
            return gql_ref(Q_DETAIL, ref)["getModByReference"]

        def done(d):
            if token != self._detail_token or not d:
                return
            authors = ", ".join(a["user"]["username"] for a in (d.get("authors") or []) if a.get("user"))
            bits = [ref, f'{d.get("downloads", 0):,} downloads', f'{d.get("views", 0):,} views']
            if authors:
                bits.insert(1, "by " + authors)
            self.d_meta.set("  \u2022  ".join(bits))
            segs = [(d.get("short_description") or "", "dim"), ("\n", "")]
            segs += markdown_segments(d.get("full_description") or "")
            vers = d.get("versions") or []
            if vers:
                segs.append(("\n\nRecent versions\n", "h"))
                for v in vers:
                    segs.append((f'v{v["version"]}   game: {v.get("game_version") or "any"}\n', "code"))
            if d.get("source_url"):
                segs.append(("\nSource: " + d["source_url"] + "\n", "dim"))
            self.set_detail_text(segs)

        def fail(ex):
            if token == self._detail_token:
                self.set_detail_text([(m["short_description"] or "", ""), ("\n\nCould not load the full page: " + str(ex), "dim")])
        self.run_bg(work, done, fail)

    def set_detail_text(self, segs):
        t = self.d_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for text, tag in segs:
            t.insert("end", text, tag) if tag else t.insert("end", text)
        t.configure(state="disabled")
        t.yview_moveto(0)

    def selected_mod(self):
        s = self.tree.selection()
        if not s or not s[0].isdigit():
            return None
        return self.results[int(s[0])]

    def open_page(self):
        m = self.selected_mod()
        if m:
            webbrowser.open(f"https://ficsit.app/mod/{m['mod_reference']}")

    # install
    def install_selected(self):
        m = self.selected_mod()
        if m and self.need_game():
            self.install_refs([m["mod_reference"]])

    def install_refs(self, refs, pins=None, after=None):
        def done(res):
            installed, skipped = res
            self.refresh_installed()
            msg = ""
            if installed:
                msg += "Installed:\n" + "\n".join(installed)
            if skipped:
                msg += ("\n\n" if msg else "") + "Skipped:\n" + "\n".join(skipped)
            n = sum(1 for i in installed if not i.startswith("("))
            self.set_status(f"Installed {n} mod(s), skipped {len(skipped)}")
            self.tree_refresh_deps()
            if msg:
                messagebox.showinfo("Done", msg)
            if after:
                after()
        self.run_bg(lambda: self._install_many(refs, pins), done)

    def installed_version(self, ref):
        folder = os.path.join(self.mods_dir, ref)
        return read_uplugin(folder)[1] if os.path.isdir(folder) else None

    def _install_many(self, refs, pins=None):
        pins = pins or {}
        done, skipped, queue, seen = [], [], list(refs), set()
        requested = set(refs)
        have = []
        cl = self.game_cl
        while queue:
            ref = queue.pop(0)
            if ref in seen or ref == "FactoryGame":
                continue
            seen.add(ref)
            if ref in pins and self.installed_version(ref) == pins[ref]:
                have.append(ref)  # exact version already installed
                continue
            if ref not in requested and os.path.isdir(os.path.join(self.mods_dir, ref)):
                have.append(ref)  # dependency already installed - nothing to download
                continue
            self.set_status(f"Resolving {ref}...")
            mod = fetch_mod(ref)
            vers = mod["versions"]
            ver, ok = None, True
            note = ""
            if ref in pins:
                ver = next((v for v in vers if v["version"] == pins[ref]), None)
                ok = ver is not None and (not cl or satisfies(cl, ver.get("game_version")))
                if ver is None:
                    note = f"  [pinned version {pins[ref]} not found]"
            if ver is None:
                ver, ok = pick_version(vers, cl)
            if cl and not ok:
                latest = ver  # newest version, or the pinned one for modpacks
                if not self.ask("Not compatible with your game", (
                        f"{mod['name']} has no version that supports your game version ({cl}).\n\n"
                        f"Version {latest['version']} requires game version: {latest.get('game_version') or 'unknown'}\n\n"
                        "It will probably not load. Install it anyway?")):
                    skipped.append(f"{mod['name']} (incompatible with game version {cl})")
                    continue
                ver = latest
                note = "  [may be incompatible]"
            elif cl and ver is not vers[0] and ref not in pins:
                note = f"  [older version, newest needs {vers[0].get('game_version')}]"
            for dep in ver.get("dependencies") or []:
                if not dep.get("optional") and dep["mod_id"] not in seen and dep["mod_id"] != "FactoryGame":
                    queue.append(dep["mod_id"])
            link = None
            for t in ver.get("targets") or []:
                if t["targetName"] in ("Windows", "WindowsNoEditor", "WindowsClient"):
                    link = t["link"]
                    break
            link = link or ver["link"]
            url = link if link.startswith("http") else API + link
            self.set_status(f"Downloading {mod['name']} {ver['version']}...")
            data = http_get(url)
            self.install_zip(data, mod["mod_reference"])
            done.append(f"{mod['name']} {ver['version']}{note}")
        if have:
            done.append("(dependencies already installed: " + ", ".join(have) + ")")
        return done, skipped

    def install_from_files(self):
        if not self.need_game():
            return
        paths = filedialog.askopenfilenames(
            title="Select downloaded mod files",
            filetypes=[("Satisfactory mods", "*.smod *.zip"), ("All files", "*.*")])
        if paths:
            self.install_local(paths)

    def install_local(self, paths):
        if not self.need_game():
            return

        def work():
            ok, bad = [], []
            for path in paths:
                fname = os.path.basename(path)
                try:
                    self.set_status(f"Installing {fname}...")
                    if not zipfile.is_zipfile(path):
                        raise RuntimeError("not a valid .smod/.zip archive")
                    with open(path, "rb") as f:
                        data = f.read()
                    stem = os.path.splitext(fname)[0].split("_")[0].split("-")[0] or "UnknownMod"
                    ok.append(f"{fname}  ->  Mods\\{self.install_zip(data, stem)}")
                except Exception as ex:
                    bad.append(f"{fname}: {ex}")
            return ok, bad

        def done(res):
            ok, bad = res
            self.refresh_installed()
            self.nb.select(1)
            msg = ""
            if ok:
                msg += "Installed:\n" + "\n".join(ok)
            if bad:
                msg += ("\n\n" if msg else "") + "Failed:\n" + "\n".join(bad)
            if ok:
                msg += "\n\nNote: compatibility with your game version can't be checked for local files - check the mod's page for supported versions."
            if ok and not os.path.isfile(os.path.join(self.mods_dir, "SML", "SML.uplugin")):
                msg += "\n\nNote: SML (Satisfactory Mod Loader) isn't installed. Most mods need it - install SML too."
            self.set_status(f"Installed {len(ok)} file(s), {len(bad)} failed")
            (messagebox.showwarning if bad else messagebox.showinfo)("Install from file", msg)

        self.run_bg(work, done)

    def install_zip(self, data, fallback_name):
        """Extract so the .uplugin sits directly in Mods/<PluginName>/ (no nested folders)."""
        tmp = os.path.join(self.cfg["game_dir"], "FactoryGame", "_smm_tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                z.extractall(tmp)
            root, name = tmp, fallback_name
            for cur, _dirs, files in os.walk(tmp):  # top-down: shallowest .uplugin wins
                up = [f for f in files if f.lower().endswith(".uplugin")]
                if up:
                    root, name = cur, up[0][:-8]
                    break
            os.makedirs(self.mods_dir, exist_ok=True)
            dest = os.path.join(self.mods_dir, name)
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            shutil.move(root, dest)
            return name
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # installed
    # ---------------------------------------------------------- launching
    def mod_status(self, key):
        """(worked_before: bool, date text) for a 'Folder@version' key, against the current game version."""
        rec = self._history["good"].get(key)
        if not rec:
            return False, ""
        cl = self.game_cl
        if cl and rec.get("game") and rec["game"] != cl:
            return False, ""  # game was updated since - needs re-testing
        return True, (rec.get("last_ok") or "")[:10]

    def enabled_mod_keys(self):
        keys = {}
        if os.path.isdir(self.mods_dir):
            for d in sorted(os.listdir(self.mods_dir)):
                full = os.path.join(self.mods_dir, d)
                if os.path.isdir(full):
                    keys[d] = f"{d}@{read_uplugin(full)[1]}"
        return keys

    def start_game(self):
        gd = self.cfg["game_dir"]
        # Preferred: run the launcher executable in the game's root folder directly
        for exe_name, label in (("FactoryGameSteam.exe", "FactoryGameSteam.exe"), ("FactoryGameEGS.exe", "FactoryGameEGS.exe")):
            exe = os.path.join(gd, exe_name)
            if os.path.isfile(exe):
                subprocess.Popen([exe], cwd=gd)
                return label
        win64 = os.path.join(self.cfg["game_dir"], "FactoryGame", "Binaries", "Win64")
        steam = os.path.join(win64, "FactoryGameSteam-Win64-Shipping.exe")
        egs = os.path.join(win64, "FactoryGameEGS-Win64-Shipping.exe")
        if os.path.isfile(steam):
            os.startfile(f"steam://rungameid/{STEAM_APP_ID}")
            return "Steam"
        if os.path.isfile(egs):
            try:
                os.startfile("com.epicgames.launcher://apps/CrewDragon?action=launch&silent=true")
                return "Epic Games"
            except OSError:
                subprocess.Popen([egs], cwd=win64)
                return "game executable"
        raise RuntimeError("Couldn't find the game executable in FactoryGame\\Binaries\\Win64.")

    def launch_game(self):
        if not self.need_game():
            return
        if getattr(self, "_launch_win", None) is not None and self._launch_win.winfo_exists():
            self._launch_win.lift()
            return
        if self.ab_var.get():
            self.run_bg(lambda: satsave.auto_backup(satsave.save_roots(), self.backup_root, self.backup_state, min_age=0),
                        lambda made: None, lambda ex: None)
        keys = self.enabled_mod_keys()
        snapshot, cl = list(keys.values()), self.game_cl
        try:
            how = self.start_game()
        except Exception as ex:
            if is_elevation_error(ex):
                self.offer_admin(f"The game couldn't start without administrator rights ({ex}).", auto_launch=True)
            else:
                messagebox.showerror("Launch failed", str(ex))
            return
        self._history = load_history()
        untested = [k for k in snapshot if not self.mod_status(k)[0]]
        self.set_status(f"Launching via {how} with {len(snapshot)} mod(s) ({len(untested)} untested)...")
        self.ask_launch_result(snapshot, cl, len(untested))

    def ask_launch_result(self, snapshot, cl, n_untested):
        win = self._launch_win = tk.Toplevel(self)
        style_toplevel(self, win)
        win.title("Did the game launch?")
        win.transient(self)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        ttk.Label(win, wraplength=400, justify="left", padding=12, text=(
            f"Satisfactory is starting with {len(snapshot)} mod(s) enabled ({n_untested} never tested).\n\n"
            "Wait until you reach the main menu and check the Mods menu, then answer here. "
            "Mods are only marked as working if you answer Yes.")).pack()
        row = ttk.Frame(win, padding=(12, 0, 12, 12))
        row.pack(fill="x")

        def answer(r):
            win.destroy()
            if r:
                self.record_result(r, snapshot, cl)
        ttk.Button(row, text="Yes, it worked", command=lambda: answer("ok")).pack(side="left")
        ttk.Button(row, text="No - crashed / mods missing", command=lambda: answer("crash")).pack(side="left", padx=6)
        ttk.Button(row, text="Not sure", command=lambda: answer(None)).pack(side="left")
        win.protocol("WM_DELETE_WINDOW", lambda: answer(None))

    def record_result(self, result, snapshot, cl):
        h = load_history()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        untested = [k for k in snapshot if not self.mod_status(k)[0]]
        if result == "ok":
            for k in snapshot:
                rec = h["good"].get(k, {})
                h["good"][k] = {"first_ok": rec.get("first_ok", now), "last_ok": now, "game": cl}
        h["launches"].append({"time": now, "result": result, "game": cl, "mods": snapshot, "untested": untested})
        save_history(h)
        self._history = h
        self.refresh_installed()
        if result == "ok":
            self.set_status(f"Marked {len(snapshot)} mod(s) as working.")
            return
        names = [k.split("@")[0] for k in untested]
        if not names:
            msg = ("Every enabled mod has launched successfully before, so the crash is probably caused by a game update, "
                   "a mod version change, or two mods conflicting. Check the game log (Open game logs).")
        elif len(names) == 1:
            msg = f"Most likely culprit: {names[0]} - it's the only mod that has never launched successfully."
        else:
            msg = ("Mods that have never launched successfully (the suspects):\n  " + "\n  ".join(names) +
                   "\n\nTip: press 'Disable untested mods', launch to confirm the game works, then enable them back one at a time.")
        self.set_status("Crash recorded. " + (f"{len(names)} suspect mod(s)." if names else ""))
        messagebox.showwarning("Crash recorded", msg)

    def disable_untested(self):
        if not self.need_game():
            return
        h = load_history()
        self._history = h
        todo = []
        for folder, key in self.enabled_mod_keys().items():
            if folder != "SML" and not self.mod_status(key)[0]:
                todo.append(folder)
        if not todo:
            messagebox.showinfo("Untested mods", "Every enabled mod has launched successfully before.")
            return
        if not messagebox.askyesno("Disable untested mods", "Disable these mods (SML stays on)?\n\n  " + "\n  ".join(todo) +
                                   "\n\nYou can re-enable them from this tab."):
            return
        os.makedirs(self.disabled_dir, exist_ok=True)
        for d in todo:
            dst = os.path.join(self.disabled_dir, d)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.move(os.path.join(self.mods_dir, d), dst)
        self.refresh_installed()
        self.set_status(f"Disabled {len(todo)} untested mod(s).")

    def show_history(self):
        h = load_history()
        if not h["launches"]:
            messagebox.showinfo("Launch history", "No launches recorded yet. Use 'Launch game' and answer the question.")
            return
        lines = []
        for e in reversed(h["launches"][-10:]):
            res = {"ok": "WORKED", "crash": "CRASHED"}.get(e["result"], e["result"])
            sus = e.get("untested") or []
            extra = f" - untested: {', '.join(k.split('@')[0] for k in sus)}" if sus else ""
            lines.append(f'{e["time"]}  {res}  ({len(e["mods"])} mods){extra}')
        messagebox.showinfo("Launch history (latest first)", "\n\n".join(lines))

    def on_dep_option(self):
        self.cfg["check_deps"] = bool(self.dep_var.get())
        save_config(self.cfg)
        self.refresh_installed()

    def refresh_installed(self):
        self.itree.delete(*self.itree.get_children())
        self._dep_token = getattr(self, "_dep_token", 0) + 1
        self.missing_deps = {}
        self._launch_tag = {}
        self._history = load_history()
        if not self.cfg.get("game_dir"):
            return
        check = self.dep_var.get()
        rows = []  # (iid, deps list)
        enabled_names, disabled_names = set(), set()
        for base, state in ((self.mods_dir, "Enabled"), (self.disabled_dir, "Disabled")):
            if not os.path.isdir(base):
                continue
            for d in sorted(os.listdir(base)):
                full = os.path.join(base, d)
                if os.path.isdir(full):
                    name, ver = read_uplugin(full)
                    status, _ = check_structure(full, d)
                    if state == "Disabled":
                        status = "(disabled)" if status == "OK" else status
                    found = find_uplugin(full)
                    (enabled_names if state == "Enabled" else disabled_names).update(
                        n.lower() for n in {d, found[1] if found else d})
                    iid = f"{state}|{d}"
                    if state == "Enabled":
                        good, when = self.mod_status(f"{d}@{ver}")
                        launch_txt = f"\u2714 worked ({when})" if good else "not tested yet"
                        self._launch_tag[iid] = "worked" if good else "untested"
                    else:
                        launch_txt = "(disabled)"
                    self.itree.insert("", "end", iid=iid, tags=((self._launch_tag[iid],) if iid in self._launch_tag else ()),
                                      values=(name, d, ver, state, status, "checking..." if check else "-", launch_txt))
                    rows.append((iid, read_plugin_deps(full) if check else []))
        self.update_profile_hint()
        has_sml = os.path.isfile(os.path.join(self.mods_dir, "SML", "SML.uplugin"))
        if self.itree.get_children() and not has_sml:
            self.set_status("Warning: SML is not installed (or not enabled). Most mods will not load without it.")
        if check:
            token = self._dep_token
            self.run_bg(lambda: self._resolve_deps(rows, enabled_names, disabled_names),
                        lambda res: self._apply_deps(token, res), lambda ex: self._apply_deps(token, None))

    def _resolve_deps(self, rows, enabled, disabled):
        """Work out, per installed mod, which required mods are missing / disabled."""
        cache = self.__dict__.setdefault("_online_cache", {})
        result = {}
        for iid, deps in rows:
            missing, off, unknown = [], [], False
            for dep in deps:
                key = dep.lower()
                if key in enabled or key == "factorygame":
                    continue
                if key in disabled:
                    off.append(dep)
                    continue
                if key not in cache or cache[key] is None:
                    cache[key] = mod_exists_online(dep)
                if cache[key] is True:
                    missing.append(dep)
                elif cache[key] is None:
                    unknown = True  # offline - can't tell engine plugins from mods
            result[iid] = (missing, off, unknown)
        return result

    def _apply_deps(self, token, result):
        if token != self._dep_token:
            return
        total = 0
        for iid in self.itree.get_children():
            vals = list(self.itree.item(iid, "values"))
            if result is None or iid not in result:
                vals[5] = "?"
                self.itree.item(iid, values=vals)
                continue
            missing, off, unknown = result[iid]
            tag = (self._launch_tag[iid],) if iid in self._launch_tag else ()
            if missing or off:
                parts = []
                if missing:
                    parts.append("MISSING: " + ", ".join(missing))
                if off:
                    parts.append("disabled: " + ", ".join(off))
                vals[5] = "  |  ".join(parts)
                tag = ("depbad",)
                total += 1
                if missing:
                    self.missing_deps[iid] = missing
            else:
                vals[5] = "OK" + (" (offline - not fully checked)" if unknown else "")
            self.itree.item(iid, values=vals, tags=tag)
        if total:
            self.set_status(f"{total} installed mod(s) have missing or disabled dependencies - see the Dependencies column")

    def install_missing_deps(self):
        if not self.need_game():
            return
        names = sorted({n for lst in getattr(self, "missing_deps", {}).values() for n in lst})
        if not names:
            messagebox.showinfo("Dependencies", "No missing dependencies found.\n(Make sure the option is ticked, then wait for the check to finish.)")
            return
        self.install_refs(names)

    def selected_installed(self):
        return [tuple(i.split("|", 1)) for i in self.itree.selection()]

    def toggle(self, enable):
        if not self.need_game():
            return
        for state, d in self.selected_installed():
            src_base, dst_base = (self.disabled_dir, self.mods_dir) if enable else (self.mods_dir, self.disabled_dir)
            if (state == "Enabled") == enable:
                continue
            os.makedirs(dst_base, exist_ok=True)
            dst = os.path.join(dst_base, d)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.move(os.path.join(src_base, d), dst)
        self.refresh_installed()

    def fix_layout(self):
        if not self.need_game():
            return
        fixed = 0
        for state, d in self.selected_installed():
            base = self.mods_dir if state == "Enabled" else self.disabled_dir
            full = os.path.join(base, d)
            status, fixable = check_structure(full, d)
            if not fixable:
                continue
            rel, plugin = find_uplugin(full)
            src = full if rel == "." else os.path.join(full, rel)
            tmp = full + "_smmfix"
            shutil.move(src, tmp)
            if os.path.isdir(full):
                shutil.rmtree(full)
            dest = os.path.join(base, plugin)
            if os.path.isdir(dest) and dest != full:
                shutil.rmtree(dest)
            shutil.move(tmp, dest)
            fixed += 1
        self.refresh_installed()
        self.set_status(f"Fixed {fixed} mod folder(s)" if fixed else "Nothing to fix in the selection")

    def open_logs(self):
        p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "FactoryGame", "Saved", "Logs")
        if os.path.isdir(p):
            os.startfile(p)
        else:
            messagebox.showinfo("Logs", "Log folder not found yet. Launch the game once first.")

    def uninstall(self):
        sel = self.selected_installed()
        if not sel or not messagebox.askyesno("Uninstall", f"Remove {len(sel)} mod(s)? Other mods may depend on them."):
            return
        for state, d in sel:
            base = self.mods_dir if state == "Enabled" else self.disabled_dir
            shutil.rmtree(os.path.join(base, d), ignore_errors=True)
        self.refresh_installed()

    def update_selected(self):
        sel = [d for state, d in self.selected_installed() if state == "Enabled"]
        if sel and self.need_game():
            self.install_refs(sel)


if __name__ == "__main__":
    App().mainloop()
