"""Extras for the mod manager: administrator relaunch, file drop, modpacks, profiles, dark mode."""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

PACK_FORMAT = "satisfactory-modpack"
LIGHT = dict(bg="#f0f0f0", fg="#000000", field="#ffffff", sel="#0078d7", selfg="#ffffff", dim="#555555",
             err="#c62828", ok="#1a7f37", warn="#b26a00", code="#f2f2f2", btn="#e1e1e1")
DARK = dict(bg="#1e1f22", fg="#e6e6e6", field="#2b2d31", sel="#3a6ea5", selfg="#ffffff", dim="#9aa0a6",
            err="#ff6b6b", ok="#4ade80", warn="#ffb454", code="#2a2c30", btn="#3a3c41")


# ------------------------------------------------------------ administrator
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_elevation_error(ex):
    return (getattr(ex, "winerror", None) == 740 or isinstance(ex, PermissionError)
            or "elevation" in str(ex).lower() or "administrator" in str(ex).lower())


def relaunch_as_admin(extra=()):
    """Start a fresh elevated copy of this program (UAC prompt). True if Windows accepted the request."""
    args = [] if getattr(sys, "frozen", False) else [os.path.abspath(sys.argv[0])]
    args += [a for a in sys.argv[1:] if a != "--launch"] + list(extra)
    try:
        return ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, subprocess.list2cmdline(args), None, 1) > 32
    except Exception:
        return False


# ---------------------------------------------------------------- file drop
def enable_file_drop(widget, callback):
    """Accept files dragged from Explorer onto the window (Windows only, no extra packages).
    callback(list_of_paths) runs on the Tk thread. Also allowed when this program is elevated."""
    if sys.platform != "win32":
        return False
    user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
    user32.GetParent.restype = wt.HWND
    user32.GetParent.argtypes = [wt.HWND]
    user32.ChangeWindowMessageFilterEx.argtypes = [wt.HWND, wt.UINT, wt.DWORD, ctypes.c_void_p]
    shell32.DragAcceptFiles.argtypes = [wt.HWND, wt.BOOL]
    widget.update_idletasks()
    hwnd = user32.GetParent(widget.winfo_id()) or widget.winfo_id()
    lresult = ctypes.c_ssize_t
    wndproc = ctypes.WINFUNCTYPE(lresult, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
    user32.CallWindowProcW.restype = lresult
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wt.UINT, wt.LPWSTR, wt.UINT]
    shell32.DragFinish.argtypes = [ctypes.c_void_p]
    for msg in (0x233, 0x49, 0x4A):  # WM_DROPFILES, WM_COPYGLOBALDATA, WM_COPYDATA
        user32.ChangeWindowMessageFilterEx(hwnd, msg, 1, None)
    shell32.DragAcceptFiles(hwnd, True)
    old = [None]

    def proc(h, msg, wp, lp):
        if msg == 0x233:
            files = []
            for i in range(shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)):
                n = shell32.DragQueryFileW(wp, i, None, 0)
                buf = ctypes.create_unicode_buffer(n + 1)
                shell32.DragQueryFileW(wp, i, buf, n + 1)
                files.append(buf.value)
            shell32.DragFinish(wp)
            widget.after(0, lambda: callback(files))
            return 0
        return user32.CallWindowProcW(old[0], h, msg, wp, lp)
    cb = wndproc(proc)
    old[0] = user32.SetWindowLongPtrW(hwnd, -4, ctypes.cast(cb, ctypes.c_void_p))
    widget._drop_ref = (cb, old)  # keep alive
    return True


# --------------------------------------------------------------- dark mode
def apply_theme(app, dark):
    st = ttk.Style(app)
    if not hasattr(app, "_orig_theme"):
        app._orig_theme = st.theme_use()
    st.theme_use("clam" if dark else app._orig_theme)
    p = app.pal = DARK if dark else LIGHT
    if dark:
        st.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"], troughcolor=p["bg"],
                     bordercolor="#3c3f41", lightcolor=p["bg"], darkcolor=p["bg"], insertcolor=p["fg"])
        st.map(".", foreground=[("disabled", p["dim"])])
        st.configure("TButton", background=p["btn"], foreground=p["fg"], padding=4)
        st.map("TButton", background=[("active", "#4a4d52"), ("pressed", "#4a4d52"), ("disabled", p["bg"])])
        for w in ("TEntry", "TCombobox", "TSpinbox"):
            st.configure(w, fieldbackground=p["field"], foreground=p["fg"], insertcolor=p["fg"], arrowcolor=p["fg"])
        st.map("TCombobox", fieldbackground=[("readonly", p["field"])], foreground=[("readonly", p["fg"])],
               selectbackground=[("readonly", p["field"])], selectforeground=[("readonly", p["fg"])])
        st.map("TEntry", fieldbackground=[("readonly", p["field"])])
        st.configure("TNotebook", background=p["bg"], bordercolor="#3c3f41")
        st.configure("TNotebook.Tab", background=p["btn"], foreground=p["fg"], padding=(8, 3))
        st.map("TNotebook.Tab", background=[("selected", p["field"])], foreground=[("selected", p["fg"])])
        st.configure("Treeview", background=p["field"], fieldbackground=p["field"], foreground=p["fg"], rowheight=22)
        st.map("Treeview", background=[("selected", p["sel"])], foreground=[("selected", p["selfg"])])
        st.configure("Treeview.Heading", background=p["btn"], foreground=p["fg"])
        st.map("Treeview.Heading", background=[("active", "#4a4d52")])
        st.configure("TScrollbar", background=p["btn"], troughcolor=p["bg"], arrowcolor=p["fg"])
        st.configure("TLabelframe", background=p["bg"], bordercolor="#3c3f41")
        st.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"])
        for w in ("TCheckbutton", "TRadiobutton"):
            st.configure(w, background=p["bg"], foreground=p["fg"], indicatorcolor=p["field"])
            st.map(w, background=[("active", p["bg"])], indicatorcolor=[("selected", p["sel"]), ("!selected", p["field"])])
        st.configure("TPanedwindow", background=p["bg"])
    st.configure("Dim.TLabel", foreground=p["dim"])
    st.configure("Err.TLabel", foreground=p["err"])
    st.configure("Warn.TLabel", foreground=p["warn"])
    app.configure(bg=p["bg"])
    app.option_add("*TCombobox*Listbox.background", p["field"])
    app.option_add("*TCombobox*Listbox.foreground", p["fg"])
    app.option_add("*TCombobox*Listbox.selectBackground", p["sel"])
    app.option_add("*TCombobox*Listbox.selectForeground", p["selfg"])
    tags = {"tree": {"ok": p["ok"], "bad": p["err"]}, "itree": {"depbad": p["err"], "worked": p["ok"], "untested": p["warn"]},
            "inv_tree": {"edited": p["warn"]}}
    for attr, mapping in tags.items():
        tree = getattr(app, attr, None)
        for tag, color in mapping.items():
            if tree is not None:
                tree.tag_configure(tag, foreground=color)
    txt = getattr(app, "d_text", None)
    if txt is not None:
        txt.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"])
        txt.tag_configure("code", background=p["code"], foreground=p["fg"])
        txt.tag_configure("dim", foreground=p["dim"])
    try:  # dark title bar on Windows 10/11
        hwnd = ctypes.windll.user32.GetParent(app.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1 if dark else 0)), 4)
    except Exception:
        pass


def style_toplevel(app, win):
    p = getattr(app, "pal", LIGHT)
    win.configure(bg=p["bg"])


# -------------------------------------------------------------------- mixin
class ExtrasMixin:
    def setup_extras(self):
        apply_theme(self, bool(self.dark_var.get()))
        if enable_file_drop(self, self.on_files_dropped) and is_admin():
            self.title(self.title() + "  (Administrator)")
        if is_admin():
            self.admin_btn.configure(text="Running as administrator", state="disabled")
        if "--launch" in sys.argv:
            self.after(800, self.launch_game)

    # ---- dark mode
    def toggle_dark(self):
        self.cfg["dark_mode"] = bool(self.dark_var.get())
        self.save_cfg()
        apply_theme(self, self.cfg["dark_mode"])

    # ---- administrator
    def restart_as_admin(self, auto_launch=False):
        if relaunch_as_admin(["--launch"] if auto_launch else ()):
            self.after(200, self.destroy)
        else:
            messagebox.showinfo("Administrator", "Administrator mode was not granted, so nothing changed.")

    def offer_admin(self, reason, auto_launch=False):
        if is_admin():
            messagebox.showerror("Error", reason)
        elif messagebox.askyesno("Administrator needed", f"{reason}\n\nRestart the manager as administrator"
                                 + (" and launch the game?" if auto_launch else "?")):
            self.restart_as_admin(auto_launch)

    # ---- dropped files
    def on_files_dropped(self, paths):
        sav, mods, packs = [], [], []
        for p in paths:
            low = p.lower()
            if low.endswith(".sav"):
                sav.append(p)
            elif low.endswith((".smod", ".zip")):
                mods.append(p)
            elif low.endswith(".smpack") or (low.endswith(".json") and self._is_pack(p)):
                packs.append(p)
        if sav:
            first = None
            for p in sav:
                label = f"{os.path.basename(p)}   (dropped)"
                self.sv_files[label] = p
                first = first or label
            self.sv_combo["values"] = list(self.sv_files)
            self.sv_combo.set(first)
            self.nb.select(self.sv_tab)
            self.sv_open()
        if mods:
            self.install_local(mods)
        if packs:
            self.load_modpack(packs[0])
        if not (sav or mods or packs):
            self.set_status("Drop .sav saves, .smod/.zip mods or .smpack modpacks onto the window.")

    @staticmethod
    def _is_pack(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return json.load(f).get("format") == PACK_FORMAT
        except Exception:
            return False

    # ---- modpacks
    def capture_enabled(self):
        """{mod folder: version} of every enabled mod."""
        return {d: k.split("@", 1)[1] for d, k in self.enabled_mod_keys().items()}

    def save_modpack(self):
        if not self.need_game():
            return
        mods = self.capture_enabled()
        if not mods:
            messagebox.showinfo("Modpack", "No enabled mods to save.")
            return
        name = simpledialog.askstring("Modpack", "Modpack name:", initialvalue=self.cfg.get("active_profile", "My modpack"), parent=self)
        if not name:
            return
        path = filedialog.asksaveasfilename(defaultextension=".smpack", initialfile=re.sub(r"[^\w\- ]", "", name) + ".smpack",
                                            filetypes=[("Satisfactory modpack", "*.smpack"), ("JSON", "*.json")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"format": PACK_FORMAT, "version": 1, "name": name, "game_version": self.game_cl,
                       "mods": [{"ref": r, "version": v} for r, v in sorted(mods.items())]}, f, indent=2)
        self.set_status(f"Saved modpack '{name}' with {len(mods)} mod(s).")

    def load_modpack(self, path=None):
        if not self.need_game():
            return
        path = path or filedialog.askopenfilename(title="Open modpack", filetypes=[("Satisfactory modpack", "*.smpack *.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                pack = json.load(f)
            assert pack.get("format") == PACK_FORMAT
            mods = {m["ref"]: str(m.get("version", "")) for m in pack["mods"]}
        except Exception:
            messagebox.showerror("Modpack", "That file isn't a valid modpack.")
            return
        name = pack.get("name") or os.path.splitext(os.path.basename(path))[0]
        todo = [r for r, v in mods.items() if self.installed_version(r) != v]
        note = ""
        if pack.get("game_version") and self.game_cl and pack["game_version"] != self.game_cl:
            note = f"\n\nThis pack was made for game build {pack['game_version']}; you have {self.game_cl}."
        if not messagebox.askyesno("Load modpack", f"Modpack '{name}' has {len(mods)} mod(s); {len(todo)} need downloading "
                                   f"(exact versions where available, plus any dependencies).{note}\n\nContinue?"):
            return

        def after():
            if messagebox.askyesno("Modpack", f"Create a profile '{name}' with exactly these mods and switch to it?\n"
                                   "(No = keep your other mods enabled.)"):
                self.cfg.setdefault("profiles", {})[name] = {"mods": {r: self.installed_version(r) or v for r, v in mods.items()}}
                self.save_cfg()
                self.switch_profile(name, force=True)
        if todo:
            self.install_refs(todo, pins={r: mods[r] for r in todo}, after=after)
        else:
            after()

    # ---- profiles
    def _build_profile_row(self, parent):
        row = ttk.Frame(parent)
        row.pack(side="top", fill="x", pady=(0, 6))
        ttk.Label(row, text="Profile:").pack(side="left")
        self.prof_combo = ttk.Combobox(row, state="readonly", width=22)
        self.prof_combo.pack(side="left", padx=6)
        self.prof_combo.bind("<<ComboboxSelected>>", lambda _: self.switch_profile(self.prof_combo.get()))
        for text, cmd in (("New from current", self.new_profile), ("Save current", self.save_profile),
                          ("Rename", self.rename_profile), ("Delete", self.delete_profile)):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Separator(row, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(row, text="Save modpack...", command=self.save_modpack).pack(side="left", padx=2)
        ttk.Button(row, text="Load modpack...", command=self.load_modpack).pack(side="left", padx=2)
        self.prof_hint = tk.StringVar()
        ttk.Label(row, textvariable=self.prof_hint, style="Warn.TLabel").pack(side="left", padx=10)
        self.init_profiles()

    def init_profiles(self):
        profs = self.cfg.setdefault("profiles", {})
        if not profs:
            profs["Default"] = {"mods": self.capture_enabled()}
            self.cfg["active_profile"] = "Default"
            self.save_cfg()
        if self.cfg.get("active_profile") not in profs:
            self.cfg["active_profile"] = sorted(profs)[0]
        self._refresh_profile_combo()

    def _refresh_profile_combo(self):
        self.prof_combo["values"] = sorted(self.cfg["profiles"])
        self.prof_combo.set(self.cfg["active_profile"])
        self.update_profile_hint()

    def update_profile_hint(self):
        if not hasattr(self, "prof_hint"):
            return
        prof = self.cfg.get("profiles", {}).get(self.cfg.get("active_profile"))
        if not prof:
            return
        now, saved = set(self.capture_enabled()) - {"SML"}, set(prof["mods"]) - {"SML"}
        self.prof_hint.set(f"Enabled mods differ from this profile (+{len(now - saved)} / -{len(saved - now)}) - 'Save current' to keep"
                           if now != saved else "")

    def new_profile(self):
        name = (simpledialog.askstring("New profile", "Name for the new profile (starts as your enabled mods):", parent=self) or "").strip()
        if not name:
            return
        if name in self.cfg["profiles"]:
            messagebox.showerror("Profile", "A profile with that name exists.")
            return
        self.cfg["profiles"][name] = {"mods": self.capture_enabled()}
        self.cfg["active_profile"] = name
        self.save_cfg()
        self._refresh_profile_combo()

    def save_profile(self):
        self.cfg["profiles"][self.cfg["active_profile"]]["mods"] = self.capture_enabled()
        self.save_cfg()
        self.update_profile_hint()
        self.set_status(f"Profile '{self.cfg['active_profile']}' saved.")

    def rename_profile(self):
        old = self.cfg["active_profile"]
        name = (simpledialog.askstring("Rename profile", "New name:", initialvalue=old, parent=self) or "").strip()
        if not name or name == old:
            return
        if name in self.cfg["profiles"]:
            messagebox.showerror("Profile", "A profile with that name exists.")
            return
        self.cfg["profiles"][name] = self.cfg["profiles"].pop(old)
        self.cfg["active_profile"] = name
        self.save_cfg()
        self._refresh_profile_combo()

    def delete_profile(self):
        if len(self.cfg["profiles"]) < 2:
            messagebox.showinfo("Profile", "You need at least one profile.")
            return
        name = self.cfg["active_profile"]
        if messagebox.askyesno("Delete profile", f"Delete profile '{name}'? (Mods stay installed.)"):
            del self.cfg["profiles"][name]
            self.cfg["active_profile"] = sorted(self.cfg["profiles"])[0]
            self.save_cfg()
            self._refresh_profile_combo()
            self.switch_profile(self.cfg["active_profile"], force=True)

    def switch_profile(self, name, force=False):
        if not self.need_game():
            self._refresh_profile_combo()
            return
        if not force and self.prof_hint.get() and not messagebox.askyesno(
                "Switch profile", f"'{self.cfg['active_profile']}' has unsaved changes that will be lost. Switch anyway?"):
            self._refresh_profile_combo()
            return
        want = self.cfg["profiles"][name]["mods"]
        enabled = self.capture_enabled()
        disabled = set(os.listdir(self.disabled_dir)) if os.path.isdir(self.disabled_dir) else set()
        os.makedirs(self.disabled_dir, exist_ok=True)
        try:
            for d in list(enabled):
                if d != "SML" and d not in want:  # SML always stays on
                    dst = os.path.join(self.disabled_dir, d)
                    shutil.rmtree(dst, ignore_errors=True)
                    shutil.move(os.path.join(self.mods_dir, d), dst)
            for r in want:
                if r in disabled and r not in enabled:
                    shutil.move(os.path.join(self.disabled_dir, r), os.path.join(self.mods_dir, r))
        except OSError as ex:
            self.offer_admin(f"Couldn't move mod folders: {ex}")
            return
        self.cfg["active_profile"] = name
        self.save_cfg()
        self._refresh_profile_combo()
        self.refresh_installed()
        missing = [r for r in want if r not in enabled and r not in disabled]
        self.set_status(f"Switched to profile '{name}'.")
        if missing and messagebox.askyesno("Missing mods", f"{len(missing)} mod(s) in this profile aren't installed:\n  "
                                           + "\n  ".join(missing) + "\n\nDownload them now?"):
            self.install_refs(missing, pins={r: want[r] for r in missing})
