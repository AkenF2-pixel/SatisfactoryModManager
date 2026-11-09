"""Save editor + map tabs for the mod manager (mixin; the real work lives in satsave.py)."""
import base64
import datetime
import os
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import satsave

MAP_PX = 700
EMPTY = "(empty)"


class SaveTabsMixin:
    # ------------------------------------------------------------- setup
    def _build_save_tabs(self, nb):
        self.backup_root = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SatModManager", "save_backups")
        self.backup_state = dict(self.cfg.get("backup_state", {}))
        self.sv_ctx, self.sv_pending, self.sv_names, self.sv_files = None, {}, {}, {}
        self._build_editor_tab(nb)
        self._build_map_tab(nb)
        self.sv_refresh_list()

    def _build_editor_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Save editor")
        self.sv_tab = tab

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Save:").pack(side="left")
        self.sv_combo = ttk.Combobox(row, state="readonly", width=60)
        self.sv_combo.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(row, text="Open", command=self.sv_open).pack(side="left")
        ttk.Button(row, text="Browse...", command=self.sv_browse).pack(side="left", padx=4)
        ttk.Button(row, text="Refresh list", command=self.sv_refresh_list).pack(side="left")

        self.sv_info = tk.StringVar(value="Pick a save and press Open.")
        ttk.Label(tab, textvariable=self.sv_info, wraplength=1100).pack(anchor="w", pady=(6, 0))
        self.sv_warn = tk.StringVar()
        ttk.Label(tab, textvariable=self.sv_warn, style="Err.TLabel", wraplength=1100).pack(anchor="w")

        bk = ttk.LabelFrame(tab, text="Backups", padding=6)
        bk.pack(fill="x", pady=8)
        self.ab_var = tk.BooleanVar(value=self.cfg.get("auto_backup", True))
        ttk.Checkbutton(bk, text="Auto backup saves (checks every minute, keeps the last 15 per save)",
                        variable=self.ab_var, command=self.on_auto_backup_option).pack(side="left")
        ttk.Button(bk, text="Back up now", command=self.backup_all_now).pack(side="left", padx=8)
        ttk.Button(bk, text="Restore a backup...", command=self.sv_restore_dialog).pack(side="left")
        ttk.Button(bk, text="Open backup folder", command=self.open_backup_folder).pack(side="left", padx=8)

        inv = ttk.LabelFrame(tab, text="Player inventory", padding=6)
        inv.pack(fill="both", expand=True)
        top = ttk.Frame(inv)
        top.pack(fill="x")
        ttk.Label(top, text="Inventory:").pack(side="left")
        self.inv_combo = ttk.Combobox(top, state="readonly", width=50)
        self.inv_combo.pack(side="left", padx=6)
        self.inv_combo.bind("<<ComboboxSelected>>", lambda _: self.sv_fill_tree())

        mid = ttk.Frame(inv)
        mid.pack(fill="both", expand=True, pady=6)
        self.inv_tree = ttk.Treeview(mid, columns=("slot", "item", "amount", "state"), show="headings", selectmode="browse", height=12)
        for c, t, w in (("slot", "Slot", 50), ("item", "Item", 300), ("amount", "Amount", 80), ("state", "", 90)):
            self.inv_tree.heading(c, text=t)
            self.inv_tree.column(c, width=w, anchor="w")
        self.inv_tree.tag_configure("edited", foreground="#b26a00")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.inv_tree.yview)
        self.inv_tree.configure(yscrollcommand=sb.set)
        self.inv_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.inv_tree.bind("<<TreeviewSelect>>", lambda _: self.sv_load_editor())

        ed = ttk.Frame(mid, padding=(12, 0, 0, 0))
        ed.pack(side="left", fill="y")
        ttk.Label(ed, text="Item (only items already in this save):", wraplength=240).pack(anchor="w")
        self.ed_item = ttk.Combobox(ed, width=32)
        self.ed_item.pack(anchor="w", pady=(0, 6))
        ttk.Label(ed, text="Amount (0-999999):").pack(anchor="w")
        self.ed_amount = ttk.Spinbox(ed, from_=0, to=999999, width=10)
        self.ed_amount.pack(anchor="w", pady=(0, 6))
        ttk.Button(ed, text="Set slot", command=self.sv_set_slot).pack(fill="x")
        ttk.Button(ed, text="Revert slot", command=self.sv_revert_slot).pack(fill="x", pady=4)
        ttk.Button(ed, text="Revert all", command=self.sv_revert_all).pack(fill="x")
        ttk.Label(ed, wraplength=240, style="Dim.TLabel", text=(
            "Close the game first, then load the edited save from the main menu. "
            "Stack limits are not enforced - keep amounts within the item's stack size to be safe. "
            "A backup is made automatically before every write.")).pack(anchor="w", pady=(14, 0))
        self.btn_sv_save = ttk.Button(ed, text="Save changes to file", command=self.sv_save)
        self.btn_sv_save.pack(fill="x", pady=(14, 0))

    # ------------------------------------------------------- save list
    def sv_refresh_list(self):
        self.sv_files = {}
        for path, mtime, _size in satsave.list_saves(satsave.save_roots()):
            when = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            self.sv_files[f"{os.path.basename(path)}   ({when})"] = path
        self.sv_combo["values"] = list(self.sv_files)
        if self.sv_files and not self.sv_combo.get():
            self.sv_combo.current(0)

    def sv_browse(self):
        p = filedialog.askopenfilename(title="Select a Satisfactory save", filetypes=[("Satisfactory save", "*.sav")],
                                       initialdir=(satsave.save_roots() or [""])[0])
        if p:
            label = f"{os.path.basename(p)}   (browsed)"
            self.sv_files[label] = p
            self.sv_combo["values"] = list(self.sv_files)
            self.sv_combo.set(label)
            self.sv_open()

    def sv_selected_path(self):
        return self.sv_files.get(self.sv_combo.get())

    # ----------------------------------------------------------- loading
    def sv_open(self, path=None):
        path = path or self.sv_selected_path()
        if not path:
            messagebox.showinfo("Save editor", "No save selected.")
            return
        if self.sv_pending and not messagebox.askyesno("Unsaved edits", "Discard the unsaved edits?"):
            return

        def work():
            self.set_status("Reading save file...")
            with open(path, "rb") as f:
                data = f.read()
            info = satsave.read_header_info(data)
            self.set_status("Decompressing save (large saves take a while)...")
            head_len, tmpl, body = satsave.read_chunks(data)
            ctx = {"path": path, "head": data[:head_len], "tmpl": tmpl, "body": body, "info": info,
                   "mtime": os.path.getmtime(path), "levels": None, "invs": [], "err": ""}
            del data
            self.set_status("Reading objects...")
            try:
                ctx["levels"] = satsave.parse_save(body, info.get("save_version", 0))
                errs = []
                ctx["invs"] = satsave.player_inventories(body, ctx["levels"], errs)
                if not ctx["invs"]:
                    ctx["err"] = "No player inventory found in this save. " + "; ".join(errs[:2])
                elif errs:
                    ctx["err"] = "Some inventories could not be read: " + "; ".join(errs[:2])
            except satsave.SaveError as e:
                ctx["err"] = f"Inventory editing is not available for this save: {e}"
            self.set_status("Scanning map data...")
            ctx["actors"] = satsave.scan_actors(body)
            ctx["nodes"] = satsave.read_nodes(body, ctx["levels"]) if ctx["levels"] else satsave.nodes_from_actors(ctx["actors"])
            ctx["items"] = satsave.item_paths(body)
            return ctx
        self.run_bg(work, self._sv_loaded)

    def _sv_loaded(self, ctx):
        self.sv_ctx, self.sv_pending = ctx, {}
        i = ctx["info"]
        mins = (i.get("played_s") or 0) // 60
        self.sv_info.set("   |   ".join(x for x in (
            f'Session: {i.get("session", "?")}', f"Played {mins // 60}h {mins % 60:02d}m", f'Saved {i.get("saved", "?")}',
            f'Game build {i.get("build", "?")}', f'Save version {i.get("save_version", "?")}') if x))
        self.sv_warn.set(ctx["err"])
        names = {}
        for cls, path in sorted(ctx["items"].items(), key=lambda t: satsave.friendly(t[0])):
            n = satsave.friendly(cls)
            names[n if n not in names else f"{n} [{cls}]"] = path
        self.sv_names = names
        self.ed_item["values"] = [EMPTY] + list(names)
        self.inv_combo["values"] = [v["label"] for v in ctx["invs"]]
        if ctx["invs"]:
            self.inv_combo.current(0)
        else:
            self.inv_combo.set("")
        self.sv_fill_tree()
        self.map_rebuild()
        self.set_status(f'Loaded {os.path.basename(ctx["path"])}: {len(ctx["actors"]):,} objects with positions, '
                        f'{len(ctx["invs"])} player inventor{"y" if len(ctx["invs"]) == 1 else "ies"}')

    # ------------------------------------------------------- inventory
    def _inv_index(self):
        i = self.inv_combo.current()
        return i if self.sv_ctx and 0 <= i < len(self.sv_ctx["invs"]) else None

    def _item_text(self, path):
        return satsave.friendly(path.rsplit(".", 1)[-1]) if path else EMPTY

    def sv_fill_tree(self):
        self.inv_tree.delete(*self.inv_tree.get_children())
        ii = self._inv_index()
        if ii is None:
            return
        for s, st in enumerate(self.sv_ctx["invs"][ii]["stacks"]):
            path, num = self.sv_pending.get((ii, s), (st["item"], st["num"]))
            edited = (ii, s) in self.sv_pending
            self.inv_tree.insert("", "end", iid=str(s), tags=("edited",) if edited else (),
                                 values=(s + 1, self._item_text(path), num, "edited" if edited else ("unsupported slot" if "val_off" not in st else ("" if st["item_ok"] else "item locked"))))

    def sv_load_editor(self):
        sel, ii = self.inv_tree.selection(), self._inv_index()
        if not sel or ii is None:
            return
        s = int(sel[0])
        st = self.sv_ctx["invs"][ii]["stacks"][s]
        path, num = self.sv_pending.get((ii, s), (st["item"], st["num"]))
        self.ed_item.set(self._item_text(path))
        self.ed_amount.set(num)
        self.ed_item.configure(state="normal" if st["item_ok"] else "disabled")

    def sv_set_slot(self):
        sel, ii = self.inv_tree.selection(), self._inv_index()
        if not sel or ii is None:
            messagebox.showinfo("Save editor", "Select a slot first.")
            return
        s = int(sel[0])
        st = self.sv_ctx["invs"][ii]["stacks"][s]
        try:
            num = int(self.ed_amount.get())
        except ValueError:
            messagebox.showerror("Amount", "Amount must be a whole number.")
            return
        if not 0 <= num <= 999999:
            messagebox.showerror("Amount", "Amount must be between 0 and 999999.")
            return
        if "val_off" not in st:
            messagebox.showerror("Slot", "This slot uses a layout the editor doesn't understand (probably a modded slot type), so it can't be edited.")
            return
        text = self.ed_item.get().strip()
        if st["item_ok"]:
            path = "" if text in ("", EMPTY) else self.sv_names.get(text)
            if path is None:
                messagebox.showerror("Item", "Pick an item from the list (only items that already exist in this save can be used).")
                return
        else:
            path = st["item"]
        if path and num < 1:
            messagebox.showerror("Amount", "A slot with an item needs an amount of at least 1. Choose (empty) to clear the slot.")
            return
        if not path:
            num = 0
        if (path, num) == (st["item"], st["num"]):
            self.sv_pending.pop((ii, s), None)
        else:
            self.sv_pending[(ii, s)] = (path, num)
        self.sv_fill_tree()
        self.inv_tree.selection_set(str(s))

    def sv_revert_slot(self):
        sel, ii = self.inv_tree.selection(), self._inv_index()
        if sel and ii is not None:
            self.sv_pending.pop((ii, int(sel[0])), None)
            self.sv_fill_tree()
            self.inv_tree.selection_set(sel[0])

    def sv_revert_all(self):
        self.sv_pending = {}
        self.sv_fill_tree()

    def sv_save(self):
        ctx = self.sv_ctx
        if not ctx or not self.sv_pending:
            messagebox.showinfo("Save editor", "No edits to save.")
            return
        if ctx["levels"] is None:
            messagebox.showerror("Save editor", ctx["err"] or "This save can't be edited.")
            return
        if not messagebox.askyesno("Write save", "Write the edits to the save file?\nThe game must be closed. A backup is made first."):
            return
        edits = []
        for (ii, s), (path, num) in sorted(self.sv_pending.items()):
            st = ctx["invs"][ii]["stacks"][s]
            edits.append((ii, s, path if path != st["item"] else None, num if num != st["num"] else None))
        target = ctx["path"]

        def work():
            if os.path.getmtime(target) != ctx["mtime"]:
                raise satsave.SaveError("The save changed on disk since you opened it (the game may have saved). Open it again first.")
            self.set_status("Backing up...")
            bk = satsave.backup_save(target, self.backup_root, tag="__before-edit")
            self.set_status("Applying edits and checking the result...")
            new_body = satsave.apply_edits(ctx["body"], ctx["levels"], edits, ctx["info"].get("save_version", 0))
            data = satsave.build_file(ctx["head"], ctx["tmpl"], new_body)
            tmp = target + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
            return bk

        def done(bk):
            messagebox.showinfo("Saved", f"Edits written.\nBackup of the original:\n{bk}")
            self.sv_pending = {}
            self.sv_open(target)
        self.run_bg(work, done)

    # --------------------------------------------------------- backups
    def on_auto_backup_option(self):
        self.cfg["auto_backup"] = bool(self.ab_var.get())
        self.save_cfg()

    def start_auto_backup(self):
        self._ab_tick()

    def _ab_tick(self):
        if self.ab_var.get():
            def work():
                return satsave.auto_backup(satsave.save_roots(), self.backup_root, self.backup_state)

            def done(made):
                if made:
                    self.cfg["backup_state"] = self.backup_state
                    self.save_cfg()
                    self.set_status(f"Auto backup: copied {len(made)} save(s)")
            self.run_bg(work, done, lambda ex: None)
        self.after(60000, self._ab_tick)

    def backup_all_now(self):
        def work():
            n = 0
            for path, _m, _s in satsave.list_saves(satsave.save_roots()):
                satsave.backup_save(path, self.backup_root)
                n += 1
            return n
        self.run_bg(work, lambda n: self.set_status(f"Backed up {n} save(s) to {self.backup_root}"))

    def open_backup_folder(self):
        os.makedirs(self.backup_root, exist_ok=True)
        os.startfile(self.backup_root)

    def sv_restore_dialog(self):
        target = self.sv_selected_path()
        if not target:
            messagebox.showinfo("Restore", "Select a save in the list first.")
            return
        files = satsave.list_backups(self.backup_root, target)
        if not files:
            messagebox.showinfo("Restore", "No backups of this save yet.")
            return
        win = tk.Toplevel(self)
        win.configure(bg=self.pal["bg"])
        win.title("Restore a backup of " + os.path.basename(target))
        win.transient(self)
        lb = tk.Listbox(win, width=70, height=14, bg=self.pal["field"], fg=self.pal["fg"], selectbackground=self.pal["sel"],
                        selectforeground=self.pal["selfg"], highlightthickness=0)
        for f in files:
            lb.insert("end", os.path.basename(f))
        lb.pack(padx=10, pady=10)
        lb.selection_set(0)

        def restore():
            sel = lb.curselection()
            if not sel or not messagebox.askyesno("Restore", "Replace the current save with this backup?\nThe current file is backed up first.", parent=win):
                return
            satsave.backup_save(target, self.backup_root, tag="__before-restore")
            shutil.copy2(files[sel[0]], target)
            win.destroy()
            self.sv_refresh_list()
            self.set_status("Backup restored.")
        ttk.Button(win, text="Restore selected", command=restore).pack(pady=(0, 10))

    # --------------------------------------------------------------- map
    def _build_map_tab(self, nb):
        tab = ttk.Frame(nb, padding=6)
        nb.add(tab, text="Map")
        self.map_view = [(satsave.MAP_BOUNDS[0] + satsave.MAP_BOUNDS[1]) / 2, 0.0, 760000.0]
        self.map_layers, self.map_vis, self.map_nodes, self._map_token, self._map_drag = {}, {}, [], 0, None
        self.map_canvas = tk.Canvas(tab, width=MAP_PX, height=MAP_PX, bg="#0e1218", highlightthickness=0)
        self.map_canvas.pack(side="left")
        self.map_item = self.map_canvas.create_image(0, 0, anchor="nw")
        self.map_canvas.bind("<MouseWheel>", self.map_wheel)
        self.map_canvas.bind("<ButtonPress-1>", lambda e: setattr(self, "_map_drag", (e.x, e.y)))
        self.map_canvas.bind("<B1-Motion>", self.map_drag_move)
        self.map_canvas.bind("<ButtonRelease-1>", self.map_drag_end)
        self.map_canvas.bind("<Motion>", self.map_hover)

        side = ttk.Frame(tab, padding=(10, 0, 0, 0))
        side.pack(side="left", fill="both", expand=True)
        ttk.Button(side, text="Load newest save", command=self.map_load_newest).pack(fill="x")
        ttk.Label(side, text="(or open one in the Save editor tab)", style="Dim.TLabel").pack(anchor="w", pady=(0, 6))
        cols = ttk.Frame(side)
        cols.pack(fill="both", expand=True)
        c1, c2 = ttk.Frame(cols), ttk.Frame(cols)
        c1.pack(side="left", fill="y", anchor="n")
        c2.pack(side="left", fill="y", anchor="n", padx=(10, 0))

        self.map_layer_box = ttk.LabelFrame(c1, text="Map objects", padding=6)
        self.map_layer_box.pack(fill="x")
        ttk.Button(c1, text="Reset view", command=self.map_reset).pack(fill="x", pady=(8, 0))
        ttk.Button(c1, text="Load node data (JSON)...", command=self.map_load_nodes).pack(fill="x", pady=4)

        self.map_ore_box = ttk.LabelFrame(c2, text="Resource node types", padding=6)
        self.map_ore_box.pack(fill="x")
        ob = ttk.Frame(c2)
        ob.pack(fill="x", pady=4)
        ttk.Button(ob, text="All", width=6, command=lambda: self.map_toggle_ores(True)).pack(side="left")
        ttk.Button(ob, text="None", width=6, command=lambda: self.map_toggle_ores(False)).pack(side="left", padx=4)
        fb = ttk.LabelFrame(c2, text="Node filters", padding=6)
        fb.pack(fill="x", pady=(6, 0))
        self.f_purity = {}
        for key, label in (("impure", "Impure"), ("normal", "Normal"), ("pure", "Pure"), ("unknown", "Unknown purity")):
            v = tk.BooleanVar(value=True)
            self.f_purity[key] = v
            ttk.Checkbutton(fb, text=label, variable=v, command=self.map_render).pack(anchor="w")
        self.f_status = tk.StringVar(value="all")
        for val, label in (("all", "Claimed + unclaimed"), ("free", "Unclaimed only"), ("used", "Claimed only (dim)")):
            ttk.Radiobutton(fb, text=label, value=val, variable=self.f_status, command=self.map_render).pack(anchor="w")

        self.map_info = tk.StringVar(value="No save loaded.")
        ttk.Label(side, textvariable=self.map_info, wraplength=440, justify="left").pack(anchor="w", pady=(8, 0))
        self.map_coord = tk.StringVar()
        ttk.Label(side, textvariable=self.map_coord, style="Dim.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Label(side, wraplength=440, style="Dim.TLabel", justify="left", text=(
            "Wheel = zoom, drag = move. Dots are objects from the save (no terrain picture). "
            "Node types and purity are read from the save; bigger dot = higher purity, dim dot = claimed.")).pack(anchor="w", pady=(6, 0))
        p = self.cfg.get("node_json")
        if p and os.path.isfile(p):
            try:
                self.map_nodes = satsave.load_nodes(p)
            except Exception:
                pass
        self.map_rebuild()

    def map_load_newest(self):
        saves = satsave.list_saves(satsave.save_roots())
        if not saves:
            messagebox.showinfo("Map", "No saves found. Use Browse in the Save editor tab.")
            return
        self.sv_open(saves[0][0])

    def map_load_nodes(self):
        p = filedialog.askopenfilename(title="Resource node data", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not p:
            return
        try:
            nodes = satsave.load_nodes(p)
        except Exception as ex:
            messagebox.showerror("Resource nodes", f"Could not read that file: {ex}")
            return
        if not nodes:
            messagebox.showwarning("Resource nodes", "No nodes found. Expected entries with a 'location' and a 'resource' (or 'ClassName').")
            return
        self.map_nodes = nodes
        self.cfg["node_json"] = p
        self.save_cfg()
        self.map_rebuild()

    def map_rebuild(self):
        ctx = self.sv_ctx
        actors = ctx["actors"] if ctx else []
        nodes = satsave.merge_nodes(ctx["nodes"] if ctx else [], self.map_nodes)
        self.map_layers = satsave.build_layers(actors, nodes)
        for box in (self.map_layer_box, self.map_ore_box):
            for w in box.winfo_children():
                w.destroy()
        old = {k: v.get() for k, v in self.map_vis.items()}
        self.map_vis = {}
        for name, groups in self.map_layers.items():
            rgb = groups[0][0]
            ore = name.startswith("Ore: ")
            var = tk.BooleanVar(value=old.get(name, True))
            self.map_vis[name] = var
            shown = name[5:] if ore else name
            row = ttk.Frame(self.map_ore_box if ore else self.map_layer_box)
            row.pack(fill="x")
            best = max((g[0] for g in groups), key=sum)  # undimmed colour for the swatch
            tk.Label(row, width=2, bg="#%02x%02x%02x" % tuple(best)).pack(side="left", padx=(0, 4), pady=1)
            ttk.Checkbutton(row, text=f"{shown} ({sum(len(g[2]) for g in groups):,})", variable=var,
                            command=self.map_render).pack(side="left")
        n_ore = sum(len(g[2]) for n, gs in self.map_layers.items() if n.startswith("Ore: ") for g in gs)
        n_typed = sum(len(g[2]) for n, gs in self.map_layers.items() if n.startswith("Ore: ") and n != "Ore: Unknown type" for g in gs)
        self.map_info.set((f"{len(actors):,} objects. " if actors else "No save loaded. ")
                          + (f"{n_ore:,} resource nodes ({n_typed:,} with a known type)." if n_ore else
                             "No resource nodes found in the save - load a node list (JSON) to see ore deposits."))
        self.map_render()

    def map_toggle_ores(self, on):
        for name, var in self.map_vis.items():
            if name.startswith("Ore: "):
                var.set(on)
        self.map_render()

    def _group_ok(self, g):
        purity, claimed = g[3], g[4]
        if purity is None:
            return True
        if not self.f_purity.get(purity, self.f_purity["unknown"]).get():
            return False
        status = self.f_status.get()
        return not ((status == "free" and claimed) or (status == "used" and not claimed))

    def map_render(self):
        self._map_token += 1
        token = self._map_token
        groups = [g[:3] for name, gs in self.map_layers.items() if self.map_vis[name].get() for g in gs if self._group_ok(g)]
        cx, cy, span = self.map_view

        def work():
            return base64.b64encode(satsave.render_png(MAP_PX, MAP_PX, cx, cy, span, groups))

        def done(png):
            if token == self._map_token:
                self._map_photo = tk.PhotoImage(data=png)
                self.map_canvas.itemconfigure(self.map_item, image=self._map_photo)
                self.map_canvas.coords(self.map_item, 0, 0)
        self.run_bg(work, done)

    def map_reset(self):
        self.map_view = [(satsave.MAP_BOUNDS[0] + satsave.MAP_BOUNDS[1]) / 2, 0.0, 760000.0]
        self.map_render()

    def _map_world(self, px, py):
        cx, cy, span = self.map_view
        return cx - span / 2 + px / MAP_PX * span, cy - span / 2 + py / MAP_PX * span

    def map_wheel(self, e):
        wx, wy = self._map_world(e.x, e.y)
        f = 0.8 if e.delta > 0 else 1.25
        span = min(max(self.map_view[2] * f, 15000.0), 1500000.0)
        self.map_view = [wx - (e.x / MAP_PX - 0.5) * span, wy - (e.y / MAP_PX - 0.5) * span, span]
        self.map_render()

    def map_drag_move(self, e):
        if self._map_drag:
            self.map_canvas.coords(self.map_item, e.x - self._map_drag[0], e.y - self._map_drag[1])

    def map_drag_end(self, e):
        if not self._map_drag:
            return
        dx, dy = e.x - self._map_drag[0], e.y - self._map_drag[1]
        self._map_drag = None
        if abs(dx) + abs(dy) > 2:
            k = self.map_view[2] / MAP_PX
            self.map_view[0] -= dx * k
            self.map_view[1] -= dy * k
            self.map_render()

    def map_hover(self, e):
        x, y = self._map_world(e.x, e.y)
        self.map_coord.set(f"x {x / 100:,.0f} m   y {y / 100:,.0f} m")
