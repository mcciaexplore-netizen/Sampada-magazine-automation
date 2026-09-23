from __future__ import annotations

import calendar
import csv
import os
import queue
import shutil
import subprocess
import threading
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import pandas as pd

from magazine_app import (
    CONFIG_PATH,
    ROOT,
    build_excel,
    copy_outputs,
    detect_qr_pdf_pages,
    monthly_paths,
    save_manifest,
    select_articles_containing_qr,
    status_summary,
)
from main import (
    create_links_template,
    extract_articles,
    load_config,
    make_qr_codes,
    metadata_and_script,
    narrate,
    render_videos,
)


class SampadaDesktopApp(tk.Tk):
    BG = "#F5F3EE"
    PANEL = "#FFFFFF"
    INK = "#17313B"
    MUTED = "#627077"
    ACCENT = "#D58B2A"
    DARK = "#12323E"

    def __init__(self) -> None:
        super().__init__()
        self.title("Sampada media studio")
        self.geometry("1280x820")
        self.minsize(1040, 680)
        self.configure(bg=self.BG)
        self.option_add("*Font", ("Segoe UI", 10))
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.busy = False
        self.issue_dir: Path | None = None
        self.manifest_path: Path | None = None
        self.drive_paths: dict[str, Path] | None = None
        self.config = load_config(CONFIG_PATH)
        self.rows: dict[str, dict] = {}

        self._configure_styles()
        self._build_ui()
        self.after(100, self._drain_events)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("TLabel", background=self.BG, foreground=self.INK)
        style.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED)
        style.configure("Panel.TLabel", background=self.PANEL, foreground=self.INK)
        style.configure("Title.TLabel", background=self.BG, foreground=self.INK, font=("Segoe UI Semibold", 28))
        style.configure("Section.TLabel", background=self.PANEL, foreground=self.INK, font=("Segoe UI Semibold", 13))
        style.configure("Primary.TButton", foreground="white", background=self.DARK, padding=(16, 9), font=("Segoe UI Semibold", 10))
        style.map("Primary.TButton", background=[("active", "#1B4655"), ("disabled", "#AAB4B8")])
        style.configure("TButton", padding=(12, 8))
        style.configure("Treeview", rowheight=30, background="white", fieldbackground="white", foreground=self.INK)
        style.configure("Treeview.Heading", background=self.DARK, foreground="white", font=("Segoe UI Semibold", 9), padding=7)
        style.map("Treeview", background=[("selected", "#DCE8E9")], foreground=[("selected", self.INK)])
        style.configure("Horizontal.TProgressbar", troughcolor="#E4E8E8", background=self.ACCENT)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=24)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Sampada media studio", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Monthly magazine to Excel, narration, video and YouTube QR codes — entirely on your computer.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 18))

        settings = ttk.Frame(outer, style="Panel.TFrame", padding=16)
        settings.pack(fill="x")
        ttk.Label(settings, text="Edition setup", style="Section.TLabel").grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 10))

        self.year_var = tk.StringVar(value=str(datetime.now().year))
        self.month_var = tk.StringVar(value=calendar.month_name[datetime.now().month])
        self.pdf_var = tk.StringVar()
        self.drive_var = tk.StringVar(value=str(ROOT / "drive_demo"))

        ttk.Label(settings, text="Year", style="Panel.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.year_var, width=9).grid(row=2, column=0, sticky="ew", padx=(0, 12))
        ttk.Label(settings, text="Month", style="Panel.TLabel").grid(row=1, column=1, sticky="w")
        ttk.Combobox(settings, textvariable=self.month_var, values=list(calendar.month_name)[1:], state="readonly", width=13).grid(row=2, column=1, sticky="ew", padx=(0, 12))

        ttk.Label(settings, text="Magazine PDF", style="Panel.TLabel").grid(row=1, column=2, sticky="w")
        ttk.Entry(settings, textvariable=self.pdf_var).grid(row=2, column=2, columnspan=2, sticky="ew", padx=(0, 6))
        ttk.Button(settings, text="Browse…", command=self.choose_pdf).grid(row=2, column=4, padx=(0, 12))

        ttk.Label(settings, text="Google Drive synced folder", style="Panel.TLabel").grid(row=1, column=5, sticky="w")
        ttk.Entry(settings, textvariable=self.drive_var).grid(row=2, column=5, columnspan=2, sticky="ew", padx=(0, 6))
        ttk.Button(settings, text="Browse…", command=self.choose_drive).grid(row=2, column=7)
        for column in (2, 3, 5, 6):
            settings.columnconfigure(column, weight=1)

        actions = ttk.Frame(outer, padding=(0, 14))
        actions.pack(fill="x")
        self.analyze_button = ttk.Button(actions, text="Analyze edition", style="Primary.TButton", command=self.analyze)
        self.analyze_button.pack(side="left")
        self.excel_button = ttk.Button(actions, text="Create Excel", command=self.create_excel, state="disabled")
        self.excel_button.pack(side="left", padx=8)
        self.audio_button = ttk.Button(actions, text="Create audio", command=self.create_audio, state="disabled")
        self.audio_button.pack(side="left", padx=(0, 8))
        self.video_button = ttk.Button(actions, text="Create videos", command=self.create_videos, state="disabled")
        self.video_button.pack(side="left", padx=(0, 8))
        self.links_button = ttk.Button(actions, text="YouTube links / QR", command=self.edit_links, state="disabled")
        self.links_button.pack(side="left")
        ttk.Button(actions, text="Open monthly folder", command=self.open_month_folder).pack(side="right")

        table_panel = ttk.Frame(outer, style="Panel.TFrame", padding=14)
        table_panel.pack(fill="both", expand=True)
        header = ttk.Frame(table_panel, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text="QR-enabled article review", style="Section.TLabel").pack(side="left")
        ttk.Label(header, text="Double-click a row to edit. Space toggles Create.", style="Panel.TLabel").pack(side="right")

        columns = ("create", "title", "language", "start", "end", "reason")
        self.tree = ttk.Treeview(table_panel, columns=columns, show="headings", selectmode="browse")
        headings = {"create": "Create", "title": "Title", "language": "Language", "start": "Start", "end": "End", "reason": "Selection reason"}
        widths = {"create": 70, "title": 460, "language": 90, "start": 65, "end": 65, "reason": 220}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=50, anchor="w" if column in {"title", "reason"} else "center")
        scrollbar = ttk.Scrollbar(table_panel, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.edit_selected_row)
        self.tree.bind("<space>", self.toggle_selected_row)

        footer = ttk.Frame(outer, padding=(0, 12, 0, 0))
        footer.pack(fill="x")
        self.progress = ttk.Progressbar(footer, mode="indeterminate")
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready. Choose a PDF to begin.")
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

    def choose_pdf(self) -> None:
        path = filedialog.askopenfilename(title="Choose Sampada magazine", filetypes=[("PDF files", "*.pdf")])
        if path:
            self.pdf_var.set(path)

    def choose_drive(self) -> None:
        path = filedialog.askdirectory(title="Choose Google Drive synced folder")
        if path:
            self.drive_var.set(path)

    def _run(self, label: str, task, on_success=None) -> None:
        if self.busy:
            return
        self.busy = True
        self.progress.start(12)
        self.status_var.set(label)
        self._set_action_state("disabled")

        def worker() -> None:
            try:
                result = task()
                self.events.put(("success", (result, on_success)))
            except Exception as exc:
                self.events.put(("error", (exc, traceback.format_exc())))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self.busy = False
                self.progress.stop()
                if kind == "success":
                    result, callback = payload
                    if callback:
                        callback(result)
                else:
                    exc, details = payload
                    self.status_var.set(f"Failed: {exc}")
                    messagebox.showerror("Sampada media studio", f"{exc}\n\nTechnical details:\n{details[-1500:]}")
                self._refresh_buttons()
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _set_action_state(self, state: str) -> None:
        for button in (self.analyze_button, self.excel_button, self.audio_button, self.video_button, self.links_button):
            button.configure(state=state)

    def _refresh_buttons(self) -> None:
        self.analyze_button.configure(state="normal")
        ready = self.manifest_path is not None and self.manifest_path.exists()
        self.excel_button.configure(state="normal" if ready else "disabled")
        metadata = bool(self.issue_dir and (self.issue_dir / "youtube_metadata.csv").exists())
        self.audio_button.configure(state="normal" if metadata else "disabled")
        stats = status_summary(self.issue_dir) if self.issue_dir else {"selected": 0, "audio": 0}
        all_audio = stats["selected"] > 0 and stats["audio"] >= stats["selected"]
        self.video_button.configure(state="normal" if all_audio else "disabled")
        self.links_button.configure(state="normal" if ready else "disabled")

    def analyze(self) -> None:
        try:
            pdf = Path(self.pdf_var.get()).resolve()
            if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
                raise ValueError("Choose a valid Sampada PDF.")
            year = int(self.year_var.get())
            month = list(calendar.month_name).index(self.month_var.get())
            drive_root = Path(self.drive_var.get()).resolve()
        except Exception as exc:
            messagebox.showwarning("Edition setup", str(exc))
            return

        def task():
            drive_paths = monthly_paths(drive_root, year, month)
            issue_dir = ROOT / "work" / f"{year}-{month:02d}"
            issue_dir.mkdir(parents=True, exist_ok=True)
            saved_pdf = issue_dir / pdf.name
            if pdf != saved_pdf:
                shutil.copy2(pdf, saved_pdf)
            config = load_config(CONFIG_PATH)
            manifest = extract_articles(saved_pdf, issue_dir, config)
            qr_pages = detect_qr_pdf_pages(saved_pdf, resolution=150)
            frame = select_articles_containing_qr(manifest, qr_pages, use_existing_fallback=True)
            return issue_dir, manifest, drive_paths, frame, qr_pages

        self._run("Analyzing article titles and scanning pages for QR codes…", task, self._analysis_complete)

    def _analysis_complete(self, result) -> None:
        self.issue_dir, self.manifest_path, self.drive_paths, frame, qr_pages = result
        self._populate_tree(frame)
        selected = int(frame["selected"].astype(str).str.lower().eq("yes").sum())
        if qr_pages:
            self.status_var.set(f"Found {selected} QR-enabled article(s). Review the table before creating Excel.")
        else:
            self.status_var.set(f"Proof PDF: no embedded QR images. Loaded {selected} confirmed fallback item(s) for review.")

    def _populate_tree(self, frame: pd.DataFrame) -> None:
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        for _, row in frame.iterrows():
            item_id = str(row["id"])
            data = row.to_dict()
            self.rows[item_id] = data
            selected = str(row["selected"]).lower() == "yes"
            self.tree.insert("", "end", iid=item_id, values=("Yes" if selected else "No", row["title"], row["language"], row["printed_start_page"], row["printed_end_page"], row.get("selection_reason", "")))

    def toggle_selected_row(self, _event=None) -> str:
        selection = self.tree.selection()
        if selection:
            item_id = selection[0]
            row = self.rows[item_id]
            row["selected"] = "no" if str(row["selected"]).lower() == "yes" else "yes"
            values = list(self.tree.item(item_id, "values"))
            values[0] = "Yes" if row["selected"] == "yes" else "No"
            self.tree.item(item_id, values=values)
        return "break"

    def edit_selected_row(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        item_id = selection[0]
        row = self.rows[item_id]
        title = simpledialog.askstring("Edit article", "Title", initialvalue=str(row["title"]), parent=self)
        if title is None:
            return
        language = simpledialog.askstring("Edit article", "Language: en, mr or hi", initialvalue=str(row["language"]), parent=self)
        if language not in {"en", "mr", "hi"}:
            messagebox.showwarning("Language", "Use en, mr or hi.")
            return
        row["title"], row["language"] = title.strip(), language
        values = list(self.tree.item(item_id, "values"))
        values[1], values[2] = row["title"], row["language"]
        self.tree.item(item_id, values=values)

    def _save_review(self) -> pd.DataFrame:
        if not self.manifest_path:
            raise ValueError("Analyze a magazine first.")
        frame = pd.DataFrame(list(self.rows.values()))
        selected = frame[frame["selected"].astype(str).str.lower() == "yes"]
        if selected.empty:
            raise ValueError("Select at least one article.")
        if selected["title"].astype(str).str.strip().eq("").any():
            raise ValueError("Every selected article needs a title.")
        save_manifest(frame, self.manifest_path)
        return frame

    def create_excel(self) -> None:
        try:
            self._save_review()
        except Exception as exc:
            messagebox.showwarning("Review", str(exc))
            return
        year = int(self.year_var.get())
        month = list(calendar.month_name).index(self.month_var.get())

        def task():
            metadata_csv = metadata_and_script(self.manifest_path, self.issue_dir, self.config)
            output = self.drive_paths["data"] / f"Sampada_{year}_{month:02d}_YouTube.xlsx"
            build_excel(metadata_csv, output, month, year)
            return output

        self._run("Creating the Excel production plan…", task, lambda output: self._done(f"Excel created: {output.name}", output))

    def create_audio(self) -> None:
        def task():
            narrate(self.manifest_path, self.issue_dir, self.config)
            try:
                from generate_captions import main as gen_captions_main
                gen_captions_main(self.issue_dir, self.config)
            except Exception:
                pass
            copy_outputs(self.issue_dir, self.drive_paths)
            return self.drive_paths["audio"]
        self._run("Creating multilingual narration and captions. Internet access is required…", task, lambda output: self._done("Audio and caption generation complete.", output))

    def create_videos(self) -> None:
        def task():
            render_videos(self.manifest_path, self.issue_dir, self.config)
            copy_outputs(self.issue_dir, self.drive_paths)
            return self.drive_paths["videos"]
        self._run("Rendering 1080p videos…", task, lambda output: self._done("Video rendering complete.", output))

    def edit_links(self) -> None:
        if not self.manifest_path:
            return
        links_path = create_links_template(self.manifest_path, self.issue_dir)
        dialog = LinksDialog(self, links_path)
        self.wait_window(dialog)
        if not dialog.saved:
            return

        def task():
            created = make_qr_codes(links_path, self.issue_dir)
            if created == 0:
                raise ValueError("Add at least one valid YouTube URL.")
            copy_outputs(self.issue_dir, self.drive_paths)
            return created
        self._run("Creating YouTube QR codes…", task, lambda count: self._done(f"Created {count} QR code(s).", self.drive_paths["qr"]))

    def _done(self, message: str, path: Path) -> None:
        self.status_var.set(message)
        messagebox.showinfo("Sampada media studio", message)
        self._refresh_buttons()

    def open_month_folder(self) -> None:
        if not self.drive_paths:
            messagebox.showinfo("Monthly folder", "Analyze an edition first.")
            return
        os.startfile(self.drive_paths["root"])

    def on_close(self) -> None:
        if self.busy and not messagebox.askyesno("Processing", "Processing is still running. Close the application anyway?"):
            return
        self.destroy()


class LinksDialog(tk.Toplevel):
    def __init__(self, parent: SampadaDesktopApp, path: Path) -> None:
        super().__init__(parent)
        self.title("YouTube links")
        self.geometry("900x560")
        self.transient(parent)
        self.grab_set()
        self.path = path
        self.saved = False
        self.entries: list[tuple[dict, tk.StringVar]] = []

        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Paste YouTube links", font=("Segoe UI Semibold", 17)).pack(anchor="w")
        ttk.Label(outer, text="QR codes are created only for rows containing a valid YouTube URL.").pack(anchor="w", pady=(2, 12))

        canvas = tk.Canvas(outer, highlightthickness=0, background="white")
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        rows_frame = ttk.Frame(canvas, style="Panel.TFrame")
        rows_frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for index, row in enumerate(rows):
            ttk.Label(rows_frame, text=row["title"], style="Panel.TLabel", wraplength=350).grid(row=index, column=0, sticky="w", padx=8, pady=6)
            variable = tk.StringVar(value=row.get("youtube_url", ""))
            ttk.Entry(rows_frame, textvariable=variable, width=58).grid(row=index, column=1, sticky="ew", padx=8, pady=6)
            self.entries.append((row, variable))
        rows_frame.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self, padding=(18, 0, 18, 18))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save and create QR", style="Primary.TButton", command=self.save).pack(side="right", padx=8)

    def save(self) -> None:
        rows = []
        for row, variable in self.entries:
            row["youtube_url"] = variable.get().strip()
            rows.append(row)
        with self.path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["id", "title", "youtube_url"])
            writer.writeheader()
            writer.writerows(rows)
        self.saved = True
        self.destroy()


if __name__ == "__main__":
    SampadaDesktopApp().mainloop()

