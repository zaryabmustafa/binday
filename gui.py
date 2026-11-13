#!/usr/bin/env python3
"""Binday for Mattermost: the desktop app (Flet). See DESIGN.md and mockup/index.html.

  python gui.py            desktop app
  python gui.py --demo     synthetic server and data, nothing real is touched
  python gui.py --web      open in a browser instead of a window (combine with --demo)

Flow: Connect -> Scan (dry run) -> Review -> Confirm -> Collecting -> Results.
All deletion logic lives in app.py; this file is UI only.
"""

import json
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

import flet as ft

import app as core

CONFIG_DIR = Path.home() / ".binday"
PROFILES_FILE = CONFIG_DIR / "profiles.json"
LOG_FILE = CONFIG_DIR / "binday.log"
LOG_SHOWN = "~/.binday/binday.log"  # how the path is shown (never the full home path with the user name)
KEYRING_SERVICE = "binday"
SECRETS = ("ssh_password", "db_password", "mm_token")
FONT_FILE = "fonts/PublicSans.ttf"  # bundled in assets/ (SIL OFL, see assets/fonts/OFL.txt)

# ---------------------------------------------------------------- theme (DESIGN.md tokens)
# Custom tokens ride on spare ColorScheme roles so light/dark switch with the OS automatically.
LIGHT = dict(primary="#1C58D9", on_primary="#FFFFFF",
             secondary="#12807F", on_secondary="#FFFFFF",                 # bin: archived channels (teal)
             tertiary="#6B4A2E", on_tertiary="#FFFFFF",                   # bin: orphaned files (brown)
             tertiary_container="#E8B21A", on_tertiary_container="#2B2100",  # bin: DMs (yellow)
             error="#C8262E", on_error="#FFFFFF", error_container="#FBE9E9", on_error_container="#A61E25",
             surface="#FFFFFF", on_surface="#182033", on_surface_variant="#4B5568",
             outline="#D5DBE5", outline_variant="#E8ECF2",
             surface_container="#FFFFFF", surface_container_low="#F8F9FC", surface_container_lowest="#F4F6FA",
             inverse_surface="#1E325C", on_inverse_surface="#E8EEFA",     # rail navy / rail ink
             secondary_container="#2B4577", on_secondary_container="#B4C3E0",  # rail active / rail dim
             primary_fixed="#1E7B4F", secondary_fixed="#C8CFDA", tertiary_fixed="#5C6679",  # ok / kept / ink-3
             scrim="#0A101E")
DARK = dict(LIGHT, primary="#6F9BFF", on_primary="#06173D",
            secondary="#3FB8B5", on_secondary="#062221", tertiary="#B08560", on_tertiary="#1E1208",
            error="#EF5A5F", on_error="#2A0406", error_container="#3A1A1C", on_error_container="#FF9A9D",
            surface="#161C28", on_surface="#E8ECF4", on_surface_variant="#B0B9CB",
            outline="#2C3546", outline_variant="#222A39",
            surface_container="#161C28", surface_container_low="#1A2130", surface_container_lowest="#0F131C",
            inverse_surface="#131F3A", on_inverse_surface="#E3EAF8",
            secondary_container="#24396A", on_secondary_container="#A6B6D6",
            primary_fixed="#6BD49E", secondary_fixed="#384357", tertiary_fixed="#9AA4B8", scrim="#000000")

C = ft.Colors
INK, INK2, INK3 = C.ON_SURFACE, C.ON_SURFACE_VARIANT, C.TERTIARY_FIXED
LINE, LINE2, SURF, SURF2, GROUND = C.OUTLINE, C.OUTLINE_VARIANT, C.SURFACE, C.SURFACE_CONTAINER_LOW, C.SURFACE_CONTAINER_LOWEST
RAIL, RAIL_INK, RAIL_DIM, RAIL_ACTIVE = C.INVERSE_SURFACE, C.ON_INVERSE_SURFACE, C.ON_SECONDARY_CONTAINER, C.SECONDARY_CONTAINER
OK, KEPT, PRIMARY, RED = C.PRIMARY_FIXED, C.SECONDARY_FIXED, C.PRIMARY, C.ERROR

KIND = {  # icon, chip bg, chip fg, short label, bin name, removes, stays
    "channel": (ft.Icons.INVENTORY_2_ROUNDED, C.SECONDARY, C.ON_SECONDARY, "Channel", "Archived channels",
                "The channel, its messages and every file posted in it", "Active channels, even empty ones"),
    "dm": (ft.Icons.PERSON_OFF_ROUNDED, C.TERTIARY_CONTAINER, C.ON_TERTIARY_CONTAINER, "DM", "Deactivated users' DMs",
           "Every message and file in DMs with deactivated users (the empty conversation stays)", "The user accounts, and DMs between active users"),
    "orphan": (ft.Icons.LINK_OFF_ROUNDED, C.TERTIARY, C.ON_TERTIARY, "Orphaned", "Orphaned files",
               "Files whose message no longer exists, and their records", "Avatars, plugins, uploads in drafts"),
}
STEPS = ("Connect", "Scan", "Review", "Collect")


# ---------------------------------------------------------------- profiles & secrets

def load_profiles() -> dict:
    try:
        return json.loads(PROFILES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_profiles(profiles: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2))


def get_secret(profile: str, key: str) -> str:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, f"{profile}:{key}") or ""
    except Exception:
        return ""


def set_secret(profile: str, key: str, value: str) -> bool:
    """Store in the OS keychain. Returns False if no keychain is available (secret is then not saved)."""
    try:
        import keyring
        if value:
            keyring.set_password(KEYRING_SERVICE, f"{profile}:{key}", value)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, f"{profile}:{key}")
            except Exception:
                pass
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- small UI pieces

def spinner(size=16, color=None) -> ft.Container:
    return ft.Container(ft.ProgressRing(stroke_width=2 if size < 20 else 3, color=color), width=size, height=size)


def T(value, size=14, weight=None, color=None, **kw) -> ft.Text:
    return ft.Text(str(value), size=size, weight=weight, color=color, **kw)


def B(value, size=14, color=None) -> ft.Text:
    return T(value, size, ft.FontWeight.W_700, color)


def gb(n: float) -> str:
    return core.format_bytes(n)


def panel(content, padding=20, expand=False, width=None, height=None) -> ft.Container:
    return ft.Container(content=content, padding=padding, expand=expand, width=width, height=height,
                        bgcolor=SURF, border=ft.Border.all(1, LINE), border_radius=16,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS)


def heading(title: str, sub: str) -> ft.Control:
    return ft.Column(spacing=6, controls=[
        T(title, 28, ft.FontWeight.W_800, INK), T(sub, 15, color=INK2)])


GAP, HEAD_GAP, BAR_H = 16, 24, 58  # between blocks; title to body; footer bar height (same on every screen)


def title_row(title: str, sub: str, tools=()) -> ft.Control:
    return ft.Row([ft.Container(heading(title, sub), expand=True), *tools], spacing=10,
                  vertical_alignment=ft.CrossAxisAlignment.CENTER)


def note(icon, text) -> ft.Control:
    return ft.Row([ft.Icon(icon, size=18, color=INK2),
                   ft.Container(T(text, color=INK2, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS), expand=True)],
                  spacing=8, expand=True)


def screen(head, body, left=(), right=(), scroll=False) -> ft.Control:
    """Every screen: title row, body blocks, then a footer bar (context on the left, actions on the right)."""
    content = ft.Column(list(body), spacing=GAP, expand=True, scroll=ft.ScrollMode.AUTO if scroll else None)
    bar = ft.Container(ft.Row([ft.Row(list(left), spacing=8, expand=True), *right], spacing=10,
                              vertical_alignment=ft.CrossAxisAlignment.CENTER),
                       height=BAR_H, padding=ft.Padding.only(top=14), border=ft.Border.only(top=ft.BorderSide(1, LINE)))
    return ft.Column([head, ft.Container(height=HEAD_GAP), content, ft.Container(height=GAP), bar], spacing=0, expand=True)


def section_title(icon, title: str) -> ft.Control:
    return ft.Row(spacing=10, controls=[ft.Icon(icon, color=PRIMARY, size=20), B(title, 15, INK)])


def chip(kind: str, size=26) -> ft.Container:
    icon, bg, fg = KIND[kind][:3]
    return ft.Container(ft.Icon(icon, color=fg, size=size * 0.65), width=size, height=size, bgcolor=bg,
                        border_radius=7 if size < 30 else 10, alignment=ft.Alignment(0, 0))


def bin_label(kind: str, text=None) -> ft.Control:
    return ft.Row(spacing=8, tight=True, controls=[chip(kind), T(text or KIND[kind][3], 13, color=INK)])


def swatch(color=None, pattern=None) -> ft.Container:
    return ft.Container(width=12, height=12, border_radius=3, bgcolor=color, gradient=pattern,
                        border=ft.Border.all(1, INK if pattern is XHATCH else KEPT) if pattern else None)


def _stripes(color) -> ft.LinearGradient:
    return ft.LinearGradient(begin=ft.Alignment(-1, -1), end=ft.Alignment(-0.96, -0.85),
                             colors=[color, color, "#00000000", "#00000000"], stops=[0, 0.45, 0.45, 1],
                             tile_mode=ft.GradientTileMode.REPEATED)


HATCH = _stripes(KEPT)   # kept / skipped: not collected
XHATCH = _stripes(INK)   # failed: not collected (never red)


def btn(text, icon=None, on_click=None, kind="fill", disabled=False, small=False) -> ft.Control:
    pad = ft.Padding.symmetric(horizontal=12 if small else 18, vertical=7 if small else 10)
    shape = ft.RoundedRectangleBorder(radius=10 if small else 12)
    style = ft.ButtonStyle(padding=pad, shape=shape, text_style=ft.TextStyle(size=13 if small else 14, weight=ft.FontWeight.W_700))
    if kind == "danger":
        # per-state colours: a fixed red would hide the disabled state
        style.bgcolor = {ft.ControlState.DISABLED: ft.Colors.with_opacity(0.12, INK), ft.ControlState.DEFAULT: RED}
        style.color = {ft.ControlState.DISABLED: ft.Colors.with_opacity(0.38, INK), ft.ControlState.DEFAULT: C.ON_ERROR}
        return ft.FilledButton(text, icon=icon, on_click=on_click, disabled=disabled, style=style)
    if kind == "outline":
        style.side, style.color = ft.BorderSide(1, LINE), INK
        return ft.OutlinedButton(text, icon=icon, on_click=on_click, disabled=disabled, style=style)
    if kind == "text":
        return ft.TextButton(text, icon=icon, on_click=on_click, disabled=disabled, style=style)
    return ft.FilledButton(text, icon=icon, on_click=on_click, disabled=disabled, style=style)


def outline(focused=False):
    return ft.OutlineInputBorder(side=ft.BorderSide(2 if focused else 1, PRIMARY if focused else LINE), border_radius=10)


def borders():
    return {ft.ControlState.DEFAULT: outline(), ft.ControlState.FOCUSED: outline(True)}


def field(label, value="", password=False, helper=None, hint=None, width=None, expand=False) -> ft.TextField:
    return ft.TextField(label=label, value=value, password=password, can_reveal_password=password, hint_text=hint,
                        helper=helper, width=width, expand=expand, border=borders(), text_size=14, dense=True,
                        content_padding=ft.Padding.symmetric(horizontal=14, vertical=14))


def filter_chip(text, selected, on_click, color=None) -> ft.Container:
    parts = ([swatch(color)] if color else []) + [T(text, 13, ft.FontWeight.W_600, SURF if selected else INK)]
    return ft.Container(ft.Row(parts, spacing=7, tight=True), padding=ft.Padding.symmetric(horizontal=13, vertical=7),
                        border_radius=9, bgcolor=INK if selected else SURF, border=ft.Border.all(1, INK if selected else LINE),
                        on_click=on_click, ink=True)


def tag(text) -> ft.Container:
    return ft.Container(T(text, 11, ft.FontWeight.W_700, INK2), padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                        border_radius=6, bgcolor=SURF2, border=ft.Border.all(1, LINE))


def load_bar(segments) -> ft.Control:
    """segments: list of (weight, color, pattern, fill) ; fill = list of (weight, color, pattern) inside a track."""
    def block(w, color, pattern=None, inner=None):
        content = ft.Row([block(iw, ic, ip) for iw, ic, ip in inner if iw > 0], spacing=0) if inner else None
        return ft.Container(expand=max(1, round(w * 1000)), height=20, bgcolor=color, gradient=pattern, content=content)
    blocks = [block(*s) for s in segments if s[0] > 0]
    return ft.Container(ft.Row(blocks, spacing=3), border_radius=6, clip_behavior=ft.ClipBehavior.ANTI_ALIAS)


def legend(entries) -> ft.Control:
    """entries: (color, pattern, label, count, size_text)"""
    out = []
    for color, pattern, label, count, size_text in entries:
        out.append(ft.Row(spacing=8, tight=True, controls=[
            swatch(color, pattern), T(label, 13, color=INK), B(count, 13, INK),
            T(f"· {size_text}" if size_text else "", 13, color=INK2)]))
    return ft.Row(out, spacing=24, wrap=True)


def size_cell(nbytes, max_bytes, kept=False) -> ft.Control:
    w = max(3, round(140 * nbytes / max_bytes)) if max_bytes else 3
    return ft.Row(alignment=ft.MainAxisAlignment.END, spacing=10, controls=[
        ft.Container(width=w, height=8, border_radius=3, bgcolor=KEPT if kept else INK),
        T(gb(nbytes), color=INK2 if kept else INK)])


def footer(icon, text) -> ft.Container:
    return ft.Container(ft.Row([ft.Icon(icon, size=16, color=INK2), T(text, 12, color=INK2)], spacing=8),
                        padding=ft.Padding.symmetric(horizontal=14, vertical=9), bgcolor=SURF2,
                        border=ft.Border.only(top=ft.BorderSide(1, LINE)))


def table(columns, rows, checkbox=False, on_select_all=None) -> ft.DataTable:
    return ft.DataTable(
        columns=[ft.DataColumn(T(c.upper(), 12, ft.FontWeight.W_700, INK2), numeric=c in ("Files", "Size", "Freed"))
                 for c in columns],
        rows=rows, show_checkbox_column=checkbox, on_select_all=on_select_all, heading_row_color=SURF2,
        heading_row_height=40, data_row_min_height=42, data_row_max_height=42, column_spacing=24,
        horizontal_margin=14, divider_thickness=1, horizontal_lines=ft.BorderSide(1, LINE2), expand=True)


def what_to_do(status: str, errors) -> str:
    text = " ".join(errors).lower()
    if status == "skipped":
        return "Nothing. It changed after the scan, so it was left as it was."
    if "permission" in text:
        return "Fix file ownership in the data directory, then scan again."
    if "mmctl" in text or "channel" in text:
        return "Check that channel deletion is enabled, then scan again."
    return "See the log for details, then scan again."


# ---------------------------------------------------------------- the app

class PurgeApp:
    def __init__(self, page: ft.Page, demo: bool = False):
        self.page, self.demo = page, demo
        self.sess = None
        self.step, self.max_step, self.locked = 0, 0, False
        self.server_name, self.server_state = "", ""
        self._last_paint = 0.0
        # connect
        self.checks, self.connecting = [], False
        self.f_profile = ft.Dropdown(label="Profile", width=230, dense=True, border=borders(),
                                     leading_icon=ft.Icons.BOOKMARK_BORDER_ROUNDED,
                                     on_select=lambda e: self._load_profile(self.f_profile.value))
        self.f_ssh = field("SSH login", hint="root@chat.example.org")
        self.f_ssh_pw = field("SSH password", password=True, helper="Leave empty to use your SSH key or agent")
        self.f_ssh_key = field("SSH key file (optional)", hint="~/.ssh/id_ed25519")
        self.f_data = field("Mattermost data directory", "/opt/mattermost/data")
        self.f_container = field("Docker container", "mattermost")
        self.sw_tunnel = ft.Switch(value=True, active_color=C.ON_PRIMARY, active_track_color=PRIMARY)
        self.f_db_host = field("Host (seen from server)", "127.0.0.1", expand=True)
        self.f_db_port = field("Port", "5432", width=96)
        self.f_db_name = field("Database", "mattermost")
        self.f_db_user = field("User", "mmuser", expand=True)
        self.f_db_pw = field("Password", password=True, expand=True)
        self.f_mm_server = field("Server URL", hint="http://localhost:8065", expand=True)
        self.f_mm_token = field("Access token", password=True, expand=True)
        # scan
        self.kinds = {"channel", "dm", "orphan"}
        self.scanning, self.stop_scan = False, False
        self.scan_msg, self.scan_started = "", 0.0
        self.found = []
        self.scan_done_kinds = set()
        self.items = []
        self.deletion_enabled = None
        # review
        self.rows, self.selected = [], set()
        self.filter_kind, self.search = None, ""
        self.vacuum_after = False
        self.f_search = field(None, hint="Filter by name", width=240)
        self.f_search.prefix_icon = ft.Icons.SEARCH_ROUNDED
        self.f_search.content_padding = ft.Padding.symmetric(horizontal=12, vertical=8)
        self.f_search.on_change = self._on_search
        # collect / results
        self.route, self.route_status, self.current = [], {}, None
        self.collect_started, self.result = 0.0, None
        self.storage_before = self.storage_after = 0
        self.results_filter, self.vacuum_ran = "problems", False

        self.body = ft.Container(expand=True, padding=ft.Padding.only(left=40, right=40, top=32, bottom=20))
        self.rail = ft.Container(width=104, bgcolor=RAIL, padding=ft.Padding.only(top=18, bottom=16))
        page.add(ft.Row([self.rail, self.body], spacing=0, expand=True,
                        vertical_alignment=ft.CrossAxisAlignment.STRETCH))
        self._refresh_profiles()
        self.paint()

    # ----------------------------------------------------------------- plumbing
    def paint(self, force=True):
        """Rebuild the current screen. Progress callbacks pass force=False and are throttled."""
        now = time.monotonic()
        if not force and now - self._last_paint < 0.4:
            return
        self._last_paint = now
        self.rail.content = self._rail()
        self.body.content = [self._connect, self._scan, self._review, self._collect][self.step]()
        self.page.update()

    def go(self, step):
        if self.locked or step > self.max_step:
            return
        self.step = step
        self.paint()

    def toast(self, msg):
        self.page.show_dialog(ft.SnackBar(T(msg, color=C.ON_INVERSE_SURFACE), bgcolor=RAIL, show_close_icon=True))

    def _rail(self) -> ft.Control:
        steps = []
        for i, name in enumerate(STEPS):
            on, done = i == self.step, i < self.step or (i < self.max_step and i != self.step)
            if self.step == 3 and i < 3:
                done = True
            if on and self.locked:
                num, name = spinner(22, C.WHITE), "Collecting"
            elif done and not on:
                num = ft.Icon(ft.Icons.CHECK_ROUNDED, size=26, color=RAIL_INK)
            else:
                num = T(i + 1, 26, ft.FontWeight.W_800, C.WHITE if on else RAIL_DIM)
            color = C.WHITE if on else (RAIL_INK if done else RAIL_DIM)
            steps.append(ft.Container(
                ft.Column([ft.Container(num, height=30, alignment=ft.Alignment(0, 0)), T(name, 12, ft.FontWeight.W_600, color)],
                          spacing=3, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                width=84, padding=ft.Padding.symmetric(vertical=10), border_radius=14,
                bgcolor=RAIL_ACTIVE if on else None, on_click=(lambda e, s=i: self.go(s)) if not on else None,
                tooltip=None if i <= self.max_step else "Finish the previous step first"))
        status = []
        if self.server_name:
            status = [ft.Container(width=8, height=8, border_radius=4, bgcolor="#5FD39A" if self.server_state == "connected" else RAIL_INK),
                      T(self.server_name, 12, ft.FontWeight.W_600, RAIL_INK, text_align=ft.TextAlign.CENTER),
                      T(self.server_state, 11, color=RAIL_DIM)]
        return ft.Column(horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=4, controls=[
            ft.Container(ft.Image(src="brand/mark-256.png", width=52, height=52, tooltip="Binday for Mattermost"),
                         margin=ft.Margin.only(bottom=18)),
            *steps, ft.Container(expand=True),
            ft.Column(status, spacing=3, horizontal_alignment=ft.CrossAxisAlignment.CENTER, width=88)])

    # ----------------------------------------------------------------- 1. Connect
    def _connect(self) -> ft.Control:
        server = panel(ft.Column(spacing=18, horizontal_alignment=ft.CrossAxisAlignment.STRETCH, controls=[
            section_title(ft.Icons.DNS_OUTLINED, "Server"), self.f_ssh, self.f_ssh_pw, self.f_ssh_key, self.f_data, self.f_container]),
            padding=20, expand=True)
        database = panel(ft.Column(spacing=18, horizontal_alignment=ft.CrossAxisAlignment.STRETCH, controls=[
            section_title(ft.Icons.STORAGE_ROUNDED, "Database"),
            ft.Row([self.sw_tunnel, ft.Column([T("Go through the SSH connection", weight=ft.FontWeight.W_600, color=INK),
                                               T("The database doesn't need to be reachable from this computer", 12, color=INK2)],
                                              spacing=1, expand=True)], spacing=12),
            ft.Row([self.f_db_host, self.f_db_port], spacing=12), self.f_db_name,
            ft.Row([self.f_db_user, self.f_db_pw], spacing=12)]),
            padding=20, expand=True)
        mmctl = panel(ft.Column(spacing=16, horizontal_alignment=ft.CrossAxisAlignment.STRETCH, controls=[
            ft.Row([section_title(ft.Icons.TERMINAL_ROUNDED, "mmctl login"),
                    T("Optional. Only needed if mmctl inside the container isn't logged in already.", 13, color=INK2)], spacing=12),
            ft.Row([self.f_mm_server, self.f_mm_token], spacing=12)]), padding=20)

        if self.checks:
            check_rows = [self._check_row(*c) for c in self.checks]
        else:
            check_rows = [T("Fill in the server and database, then connect. Nothing is changed on the server.", 13, color=INK2)]
        if self.connecting:
            check_rows = [ft.Row([spinner(18, PRIMARY), T("Connecting…", color=INK2)])]
        connected = self.max_step >= 1 and not self.connecting
        wide = (self.page.width or 1280) >= 1200  # logical px: three columns; narrower windows stack
        status = panel(ft.Column(spacing=0, controls=[
            section_title(ft.Icons.LAN_OUTLINED, "Connection"), ft.Container(height=8), ft.Column(check_rows, spacing=0)]),
            width=340 if wide else None)
        stretch = ft.CrossAxisAlignment.STRETCH
        forms = ft.Row([server, database], spacing=GAP, vertical_alignment=stretch, intrinsic_height=True)
        if wide:
            body = [ft.Row([ft.Column([forms, mmctl], spacing=GAP, expand=True), status], spacing=GAP,
                           vertical_alignment=stretch, intrinsic_height=True)]
        else:  # narrow: the connection result rides above the form as a strip of chips, so it's never below the fold
            body = ([self._check_strip()] if (self.checks or self.connecting) else []) + [forms, mmctl]
        head = title_row("Connect to your server", "Works over SSH. Nothing on the server changes until you confirm a deletion.",
                         [self.f_profile, btn("Save", ft.Icons.SAVE_OUTLINED, self._save_profile, "outline", small=True),
                          btn("New", ft.Icons.ADD_ROUNDED, self._new_profile, "text", small=True)])
        return screen(head, body, scroll=True,
                      left=[note(ft.Icons.KEY_OUTLINED, "Saved profiles keep passwords in your OS keychain, never in plain text.")],
                      right=[btn("Reconnect" if connected else "Connect",
                                 ft.Icons.REFRESH_ROUNDED if connected else ft.Icons.POWER_ROUNDED,
                                 self._on_connect, "outline" if connected else "fill", disabled=self.connecting),
                             *([btn("Continue", ft.Icons.ARROW_FORWARD_ROUNDED, lambda e: self.go(1))] if connected else [])])

    def _check_strip(self) -> ft.Control:
        if self.connecting:
            chips = [(spinner(16, PRIMARY), "Connecting…", None)]
        else:
            icons = {True: (ft.Icons.CHECK_CIRCLE_ROUNDED, OK), False: (ft.Icons.CANCEL_OUTLINED, INK),
                     None: (ft.Icons.WARNING_AMBER_ROUNDED, INK)}
            chips = [(ft.Icon(icons[ok][0], color=icons[ok][1], size=18), title, detail) for ok, title, detail in self.checks]
        return ft.Row(wrap=True, spacing=8, run_spacing=8, controls=[
            ft.Container(ft.Row([icon, T(title, 13, ft.FontWeight.W_600, INK)], spacing=8, tight=True), tooltip=detail,
                         padding=ft.Padding.symmetric(horizontal=12, vertical=8), border_radius=10, bgcolor=SURF,
                         border=ft.Border.all(1, LINE)) for icon, title, detail in chips])

    @staticmethod
    def _check_row(ok, title, detail) -> ft.Control:
        icon, color = {True: (ft.Icons.CHECK_CIRCLE_ROUNDED, OK), False: (ft.Icons.CANCEL_OUTLINED, INK),
                       None: (ft.Icons.WARNING_AMBER_ROUNDED, INK)}[ok]
        return ft.Container(ft.Row([ft.Icon(icon, color=color, size=21),
                                    ft.Column([T(title, weight=ft.FontWeight.W_600, color=INK),
                                               T(detail, 13, color=INK2, selectable=True)], spacing=1, expand=True)],
                                   spacing=12, vertical_alignment=ft.CrossAxisAlignment.START),
                            padding=ft.Padding.symmetric(vertical=11), border=ft.Border.only(bottom=ft.BorderSide(1, LINE2)))

    def _form(self) -> dict:
        return {"ssh": self.f_ssh.value.strip(), "ssh_password": self.f_ssh_pw.value, "ssh_key": self.f_ssh_key.value.strip(),
                "data_dir": self.f_data.value.strip(),
                "container": self.f_container.value.strip() or "mattermost", "db_via_ssh": self.sw_tunnel.value,
                "db_host": self.f_db_host.value.strip(), "db_port": self.f_db_port.value.strip(),
                "db_name": self.f_db_name.value.strip(), "db_user": self.f_db_user.value.strip(),
                "db_password": self.f_db_pw.value, "mm_server": self.f_mm_server.value.strip(), "mm_token": self.f_mm_token.value}

    def _fill(self, d: dict):
        self.f_ssh.value, self.f_ssh_pw.value = d.get("ssh", ""), d.get("ssh_password", "")
        self.f_ssh_key.value = d.get("ssh_key", "")
        self.f_data.value, self.f_container.value = d.get("data_dir", "/opt/mattermost/data"), d.get("container", "mattermost")
        self.sw_tunnel.value = d.get("db_via_ssh", True)
        self.f_db_host.value, self.f_db_port.value = d.get("db_host", "127.0.0.1"), d.get("db_port", "5432")
        self.f_db_name.value, self.f_db_user.value = d.get("db_name", "mattermost"), d.get("db_user", "mmuser")
        self.f_db_pw.value = d.get("db_password", "")
        self.f_mm_server.value, self.f_mm_token.value = d.get("mm_server", ""), d.get("mm_token", "")

    def _refresh_profiles(self):
        profiles = load_profiles()
        self.f_profile.options = [ft.DropdownOption(key=n, text=n) for n in sorted(profiles)]
        if profiles and not self.f_ssh.value:
            self._load_profile(sorted(profiles)[0], paint=False)

    def _load_profile(self, name, paint=True):
        data = load_profiles().get(name)
        if data:
            self._fill({**data, **{k: get_secret(name, k) for k in SECRETS}})
            self.f_profile.value = name
            if paint:
                self.paint()

    def _profile_name(self) -> str:
        return self.f_profile.value or (self.f_ssh.value.split("@")[-1].split(":")[0] if self.f_ssh.value else "default")

    def _save_profile(self, e=None):
        name, form = self._profile_name(), self._form()
        profiles = load_profiles()
        profiles[name] = {k: v for k, v in form.items() if k not in SECRETS}
        save_profiles(profiles)
        stored = all(set_secret(name, k, form[k]) for k in SECRETS)
        self._refresh_profiles()
        self.f_profile.value = name
        self.paint()
        self.toast(f"Saved “{name}”." + ("" if stored else " No system keychain found, so passwords were not saved."))

    def _new_profile(self, e=None):
        self.f_profile.value = None
        self._fill({})
        self.paint()

    def _on_connect(self, e=None):
        form, missing = self._form(), False
        for fld, key in ((self.f_ssh, "ssh"), (self.f_data, "data_dir"), (self.f_db_name, "db_name"), (self.f_db_user, "db_user")):
            fld.error = None if form[key] else "Required"
            missing = missing or not form[key]
        if missing:
            self.paint()
            return
        self.connecting, self.checks, self.max_step = True, [], 0
        self.paint()
        self.page.run_thread(self._connect_worker, form)

    def _connect_worker(self, form):
        if self.sess:
            self.sess.close()
            self.sess = None
        settings = core.Settings(
            ssh=form["ssh"], ssh_password=form["ssh_password"], ssh_key=os.path.expanduser(form["ssh_key"]) if form["ssh_key"] else None,
            remote_data_dir=form["data_dir"],
            container=form["container"], db_via_ssh=form["db_via_ssh"],
            pg={"host": form["db_host"], "port": form["db_port"], "dbname": form["db_name"],
                "user": form["db_user"], "password": form["db_password"]},
            mm_server=form["mm_server"] or None, mm_token=form["mm_token"] or None)
        checks = []
        try:
            self.sess = core.Session(settings)
            auth = "password" if form["ssh_password"] else ("key file" if form["ssh_key"] else "key from agent")
            checks.append((True, "SSH connected", f"{form['ssh']} · {auth}"))
            checks.append((True, "Database reachable", f"{form['db_name']} · schema {self.sess.schema}"
                                                      + (" · via SSH" if form["db_via_ssh"] else "")))
            try:
                teams = core.mmctl_team_names(self.sess)
                checks.append((True, "mmctl works", f"container {form['container']} · {len(teams)} teams"))
            except Exception as ex:
                checks.append((False, "mmctl failed", str(ex).splitlines()[0]))
            self.deletion_enabled = core.channel_deletion_enabled(self.sess)
            checks.append((True, "Channel deletion is on", "EnableAPIChannelDeletion = true") if self.deletion_enabled else
                          (None, "Channel deletion is off", "Archived channels can be scanned but not deleted. Set "
                                                            "ServiceSettings.EnableAPIChannelDeletion = true and reconnect."))
            self.storage_before = core.storage_usage(self.sess)
            checks.append((True, "Data directory found", f"{gb(self.storage_before)} in use"))
            self.server_name, self.server_state = self._profile_name(), "connected"
            self.max_step, self.items, self.rows, self.found = 1, [], [], []
        except Exception as ex:
            logging.exception("Connect failed")
            checks.append((False, "Couldn't connect", str(ex).splitlines()[0] if str(ex) else type(ex).__name__))
            self.server_name = ""
        self.checks, self.connecting = checks, False
        self.paint()

    # ----------------------------------------------------------------- 2. Scan
    def _scan(self) -> ft.Control:
        counts = {k: [0, 0, 0] for k in KIND}  # items, files, bytes
        for it in self.found:
            c = counts[it["kind"]]
            c[0] += 1; c[1] += len(it["paths"]); c[2] += it["bytes"]
        current = next((k for k in core.KINDS if k in self.kinds and k not in self.scan_done_kinds), None) if self.scanning else None

        header = ft.Container(ft.Row([ft.Container(width=40), *[ft.Container(B(h.upper(), 12, INK2), width=w, expand=x, alignment=ft.Alignment(1, 0) if h in ("Items", "Files", "Size") else None)
                              for h, w, x in (("Bin", 250, False), ("What it removes", None, True), ("What stays", None, True),
                                              ("Items", 70, False), ("Files", 70, False), ("Size", 170, False))]], spacing=14),
                              bgcolor=SURF2, padding=ft.Padding.symmetric(horizontal=14, vertical=11),
                              border=ft.Border.only(bottom=ft.BorderSide(1, LINE)))
        rows = [header]
        for kind, (icon, bg, fg, short, name, removes, stays) in KIND.items():
            items, files, size = counts[kind]
            on = kind in self.kinds
            if not on:
                tail = [T("Skipped", color=INK2)]
                cells = ("–", "–")
            elif kind in self.scan_done_kinds:
                tail = [B(gb(size), 14, INK), ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, color=OK, size=18)]
                cells = (items, files)
            elif kind == current:
                tail = [B(gb(size), 14, INK), spinner(16, OK)]
                cells = (items, files)
            else:
                tail = [T("Waiting" if self.scanning else "Not scanned", color=INK2)]
                cells = ("–", "–")
            rows.append(ft.Container(ft.Row(spacing=14, controls=[
                ft.Checkbox(value=on, disabled=self.scanning, on_change=lambda e, k=kind: self._toggle_kind(k)),
                ft.Container(ft.Row([chip(kind, 40), B(name, 15, INK)], spacing=12), width=250),
                ft.Container(T(removes, 13, color=INK2), expand=True), ft.Container(T(stays, 13, color=INK2), expand=True),
                ft.Container(B(cells[0], 14, INK), width=70, alignment=ft.Alignment(1, 0)),
                ft.Container(T(cells[1], color=INK), width=70, alignment=ft.Alignment(1, 0)),
                ft.Container(ft.Row(tail, spacing=6, alignment=ft.MainAxisAlignment.END), width=170)]),
                padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                border=ft.Border.only(bottom=ft.BorderSide(1, LINE2)) if kind != "orphan" else None))
        bins = panel(ft.Column(rows, spacing=0), padding=0)

        head = title_row("Choose what to collect",
                         "A scan only reads. It lists each item and measures it on disk, so you can review before anything is deleted.")
        blocks = [bins]
        total = sum(c[2] for c in counts.values())
        if self.scanning or self.found:
            elapsed = int(time.monotonic() - self.scan_started) if self.scanning else 0
            prog = panel(ft.Column(spacing=10, controls=[
                ft.Row([B(self.scan_msg or "Starting…", 14, INK), ft.Container(expand=True),
                        T(f"{elapsed // 60} min {elapsed % 60:02d} s" if self.scanning else "", color=INK2)]),
                ft.ProgressBar(value=None if self.scanning else 1, color=PRIMARY, bgcolor=LINE2, bar_height=6, border_radius=3)]),
                padding=ft.Padding.symmetric(horizontal=22, vertical=16))
            recent = list(reversed(self.found[-60:]))
            found_rows = [ft.Container(ft.Row(spacing=14, controls=[
                ft.Container(bin_label(it["kind"], it["label"]), expand=2), ft.Container(T(it["group"], color=INK2), expand=1),
                ft.Container(T(f"{len(it['paths'])} file{'s' if len(it['paths']) != 1 else ''}", color=INK), width=90, alignment=ft.Alignment(1, 0)),
                ft.Container(B(gb(it["bytes"]), 14, INK), width=110, alignment=ft.Alignment(1, 0))]),
                padding=ft.Padding.symmetric(horizontal=16, vertical=8), border=ft.Border.only(bottom=ft.BorderSide(1, LINE2)))
                for it in recent]
            found = panel(ft.Column(spacing=0, expand=True, controls=[
                ft.Container(ft.Row([B("Found so far", 14, INK), T(f"{len(self.found)} items · {gb(total)} · newest first", color=INK2)], spacing=10),
                             padding=ft.Padding.symmetric(horizontal=16, vertical=12), border=ft.Border.only(bottom=ft.BorderSide(1, LINE))),
                ft.ListView(found_rows, expand=True)]), padding=0, expand=True)
            blocks += [prog, found]
        return screen(head, blocks, left=[note(ft.Icons.VISIBILITY_OUTLINED, "Dry run · nothing is deleted at this step")],
                      right=[btn("Stop scan", ft.Icons.STOP_CIRCLE_OUTLINED, self._on_stop, "outline") if self.scanning else
                             btn("Scan again" if self.found else "Run dry-run scan", ft.Icons.RADAR_ROUNDED, self._on_scan,
                                 "outline" if self.found else "fill", disabled=not self.kinds),
                             btn("Review", ft.Icons.ARROW_FORWARD_ROUNDED, lambda e: self.go(2),
                                 disabled=self.scanning or self.max_step < 2)])

    def _toggle_kind(self, kind):
        self.kinds ^= {kind}
        self.paint()

    def _on_scan(self, e=None):
        self.scanning, self.stop_scan, self.found, self.scan_done_kinds = True, False, [], set()
        self.scan_started, self.scan_msg, self.max_step = time.monotonic(), "", 1
        self.paint()
        self.page.run_thread(self._scan_worker, set(self.kinds))

    def _on_stop(self, e=None):
        self.stop_scan = True
        self.scan_msg = "Stopping after the current item…"
        self.paint()

    def _scan_worker(self, kinds):
        class Stopped(Exception):
            pass

        def progress(msg):
            if self.stop_scan:
                raise Stopped()
            self.scan_msg = msg
            self.paint(force=False)

        def on_item(it):
            # kinds are scanned in order; seeing a later kind means the earlier ones are done
            for k in core.KINDS:
                if k == it["kind"]:
                    break
                self.scan_done_kinds.add(k)
            self.found.append(it)
            progress(f"Measuring {it['label']}")

        try:
            self.sess.errors.clear()
            self.items = core.scan(self.sess, kinds, progress=progress, on_item=on_item)
            self.scan_done_kinds = set(kinds)
            self.scan_msg = f"Dry run finished {time.strftime('%H:%M')}"
            self._build_rows()
            self.max_step = 2
        except Stopped:
            self.items, self.scan_msg = [], "Scan stopped. Nothing was deleted."
        except Exception as ex:
            logging.exception("Scan failed")
            self.items, self.scan_msg = [], f"Scan failed: {ex}"
        self.scanning = False
        if self.max_step == 2:
            self.step = 2
        self.paint()

    # ----------------------------------------------------------------- 3. Review
    def _build_rows(self):
        rows = [[it] for it in self.items if it["kind"] != "orphan"]
        orphans = [it for it in self.items if it["kind"] == "orphan"]
        if orphans:
            rows.append(orphans)  # ponytail: orphans are one all-or-nothing row; thousands of rows are slow
        rows.sort(key=lambda r: -sum(i["bytes"] for i in r))
        for n, r in enumerate(rows, 1):
            for it in r:
                it["plan"] = n
        self.rows = rows
        self.selected = set(range(len(rows)))
        self.filter_kind, self.search, self.f_search.value = None, "", ""

    def _sel_items(self):
        return [it for i in sorted(self.selected) for it in self.rows[i]]

    def _review(self) -> ft.Control:
        found_bytes = sum(it["bytes"] for it in self.items)
        sel = self._sel_items()
        by_kind = {k: [it for it in sel if it["kind"] == k] for k in KIND}
        kept = [it for i, r in enumerate(self.rows) if i not in self.selected for it in r]
        sel_bytes, kept_bytes = sum(it["bytes"] for it in sel), sum(it["bytes"] for it in kept)
        segs = [(sum(i["bytes"] for i in by_kind[k]) / found_bytes if found_bytes else 0, KIND[k][1], None, None) for k in KIND]
        segs.append((kept_bytes / found_bytes if found_bytes else 0, None, HATCH, None))
        load = panel(ft.Column(spacing=12, controls=[
            ft.Row(vertical_alignment=ft.CrossAxisAlignment.END, controls=[
                T(gb(sel_bytes), 32, ft.FontWeight.W_800, INK), T(f"selected to reclaim, of {gb(found_bytes)} found", color=INK2),
                ft.Container(expand=True),
                T(f"{len(sel):,} of {len(self.items):,} items · {sum(len(i['paths']) for i in sel):,} files", color=INK2)]),
            load_bar(segs),
            legend([(KIND[k][1], None, KIND[k][4] if k != "dm" else "Deactivated DMs", len(by_kind[k]),
                     gb(sum(i["bytes"] for i in by_kind[k]))) for k in KIND if any(i["kind"] == k for i in self.items)]
                   + ([(None, HATCH, "Kept", len(kept), gb(kept_bytes))] if kept else []))]),
            padding=ft.Padding.only(left=22, right=22, top=18, bottom=16))

        counts = {k: sum(1 for it in self.items if it["kind"] == k) for k in KIND}
        filters = ft.Row(spacing=8, controls=[
            filter_chip(f"All {len(self.items):,}", self.filter_kind is None, lambda e: self._set_filter(None)),
            *[filter_chip(f"{({'channel': 'Channels', 'dm': 'DMs', 'orphan': 'Orphans'})[k]} {counts[k]:,}",
                          self.filter_kind == k, lambda e, k=k: self._set_filter(k), KIND[k][1]) for k in KIND if counts[k]]])
        toolbar = ft.Row([filters, ft.Container(expand=True), self.f_search,
                          btn("Select all", None, lambda e: self._select_all(True), "text", small=True),
                          btn("Select none", None, lambda e: self._select_all(False), "text", small=True)])

        max_bytes = max((sum(i["bytes"] for i in r) for r in self.rows), default=0)
        drows, shown = [], 0
        for idx, r in enumerate(self.rows):
            first = r[0]
            name = f"{len(r):,} orphaned files" if first["kind"] == "orphan" else first["label"]
            if self.filter_kind and first["kind"] != self.filter_kind:
                continue
            if self.search and self.search.lower() not in name.lower():
                continue
            shown += 1
            on = idx in self.selected
            files = sum(len(i["paths"]) for i in r)
            missing = sum(i["missing"] for i in r)
            nbytes = sum(i["bytes"] for i in r)
            name_cell = ft.Row([T(name, weight=ft.FontWeight.W_600, color=INK if on else INK2)] + ([tag("Kept")] if not on else []), spacing=8)
            drows.append(ft.DataRow(selected=on, on_select_change=lambda e, i=idx: self._toggle_row(i), cells=[
                ft.DataCell(T(f"#{first['plan']:03d}", 12, ft.FontWeight.W_600, INK3)),
                ft.DataCell(name_cell),
                ft.DataCell(bin_label(first["kind"])),
                ft.DataCell(T("FileInfo table" if first["kind"] == "orphan" else first["group"], color=INK2 if not on else INK)),
                ft.DataCell(ft.Row([T(f"{files:,}", color=INK if on else INK2)] + ([tag(f"{missing} missing")] if missing else []),
                                   spacing=8, alignment=ft.MainAxisAlignment.END)),
                ft.DataCell(size_cell(nbytes, max_bytes, kept=not on))]))
        tbl = table(["Plan", "Name", "Bin", "Location", "Files", "Size"], drows, checkbox=True,
                    on_select_all=lambda e: self._select_all(len(self.selected) != len(self.rows)))
        orphan_note = " · the orphaned files are one row" if counts["orphan"] else ""
        table_panel = panel(ft.Column(spacing=0, expand=True, controls=[
            ft.Column([ft.Row([tbl])], scroll=ft.ScrollMode.AUTO, expand=True),
            footer(ft.Icons.SWAP_VERT_ROUNDED, f"{shown} of {len(self.rows)} rows{' · scroll for more' if shown > 8 else ''}{orphan_note}")]),
            padding=0, expand=True)
        if not self.rows:
            table_panel = panel(ft.Container(T("Nothing to collect. Your server is tidy.", 15, color=INK2),
                                             alignment=ft.Alignment(0, 0)), expand=True)

        return screen(title_row("Review the load", f"{self.scan_msg}. Untick anything you want to keep. Largest first."),
                      [load, toolbar, table_panel],
                      left=[ft.Checkbox(value=self.vacuum_after, on_change=lambda e: setattr(self, "vacuum_after", e.control.value)),
                            T("Run VACUUM FULL afterwards", color=INK),
                            ft.Container(T("· shrinks the database file, locks FileInfo while it runs", color=INK2, max_lines=1,
                                           overflow=ft.TextOverflow.ELLIPSIS), expand=True)],
                      right=[btn(f"Delete {len(sel):,} items · {gb(sel_bytes)}…", ft.Icons.DELETE_FOREVER_ROUNDED,
                                 self._on_delete, "danger", disabled=not sel)])

    def _set_filter(self, kind):
        self.filter_kind = kind
        self.paint()

    def _on_search(self, e):
        self.search = self.f_search.value or ""
        self.paint()

    def _toggle_row(self, i):
        self.selected ^= {i}
        self.paint()

    def _select_all(self, value):
        self.selected = set(range(len(self.rows))) if value else set()
        self.paint()

    # ----------------------------------------------------------------- 3b. Confirm
    def _on_delete(self, e=None):
        sel = self._sel_items()
        if not sel:
            return
        size_str = gb(sum(i["bytes"] for i in sel))
        blocked = self.deletion_enabled is not True and any(i["kind"] == "channel" for i in sel)
        lines = []
        for k in KIND:
            its = [i for i in sel if i["kind"] == k]
            if its:
                label = {"channel": "archived channels", "dm": "DMs of deactivated users", "orphan": "orphaned files"}[k]
                lines.append(ft.Container(ft.Row([chip(k), T(f"{len(its):,} {label}", color=INK), ft.Container(expand=True),
                                                  B(gb(sum(i["bytes"] for i in its)), 14, INK)], spacing=12),
                                          padding=ft.Padding.symmetric(horizontal=14, vertical=11),
                                          border=ft.Border.only(bottom=ft.BorderSide(1, LINE2))))
        lines.append(ft.Container(ft.Row([ft.Icon(ft.Icons.FUNCTIONS_ROUNDED, size=20, color=INK),
                                          T(f"Total · {sum(len(i['paths']) for i in sel):,} files", weight=ft.FontWeight.W_800, color=INK),
                                          ft.Container(expand=True), T(size_str, weight=ft.FontWeight.W_800, color=INK)], spacing=12),
                                  padding=ft.Padding.symmetric(horizontal=14, vertical=11), bgcolor=SURF2))
        confirm = btn(f"Delete {size_str}", ft.Icons.DELETE_FOREVER_ROUNDED, None, "danger", disabled=True)
        ok_icon = ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, color=OK, visible=False)
        typed = field("Type DELETE to confirm")
        typed.disabled = blocked
        typed.suffix = ok_icon
        typed.autofocus = True

        def on_type(ev):
            match = typed.value.strip() == "DELETE"
            ok_icon.visible = match
            confirm.disabled = blocked or not match
            self.page.update()

        def do_delete(ev):
            self.page.pop_dialog()
            self._start_collect(sel)

        typed.on_change, confirm.on_click = on_type, do_delete
        warn = [self._check_row(None, "Channel deletion is off on the server",
                                "Untick archived channels, or set EnableAPIChannelDeletion = true and reconnect.")] if blocked else []
        self.page.show_dialog(ft.AlertDialog(
            modal=True, bgcolor=SURF, barrier_color=ft.Colors.with_opacity(0.46, C.SCRIM),
            shape=ft.RoundedRectangleBorder(radius=22), content_padding=ft.Padding.only(left=28, right=28, top=26, bottom=8),
            content=ft.Container(width=484, content=ft.Column(tight=True, spacing=16, controls=[
                ft.Row(spacing=14, controls=[
                    ft.Container(ft.Icon(ft.Icons.WARNING_ROUNDED, color=C.ON_ERROR_CONTAINER, size=28), width=48, height=48,
                                 border_radius=14, bgcolor=C.ERROR_CONTAINER, alignment=ft.Alignment(0, 0)),
                    ft.Column([T(f"Delete {len(sel):,} items for good?", 22, ft.FontWeight.W_800, INK),
                               T(f"From {self.server_name} · {self.f_data.value}", color=INK2)], spacing=2, expand=True)]),
                ft.Container(ft.Column(lines, spacing=0), border=ft.Border.all(1, LINE), border_radius=14,
                             clip_behavior=ft.ClipBehavior.ANTI_ALIAS),
                ft.Text(spans=[ft.TextSpan("Archived channels are deleted with mmctl, DM messages are permanently deleted, and orphaned file "
                                           "records are removed, along with every file on disk. ", ft.TextStyle(color=INK2)),
                               ft.TextSpan("This can't be undone.", ft.TextStyle(color=INK, weight=ft.FontWeight.W_700)),
                               ft.TextSpan(" Back up the database and the data directory first.", ft.TextStyle(color=INK2))], size=14),
                *warn, ft.Container(typed, margin=ft.Margin.only(top=6))])),
            actions=[btn("Cancel", None, lambda ev: self.page.pop_dialog(), "text"), confirm],
            actions_padding=ft.Padding.only(left=28, right=28, bottom=22, top=4)))

    # ----------------------------------------------------------------- 4. Collecting / Results
    def _start_collect(self, sel):
        plan_rows = sorted({it["plan"] for it in sel})
        self.route = [[it for it in sel if it["plan"] == p] for p in plan_rows]
        self.route_status = {p: {"status": "queued", "freed": 0, "deleted": 0, "failed": 0, "skipped": 0, "errors": []} for p in plan_rows}
        self.current, self.result, self.results_filter = None, None, "problems"
        self.collect_started, self.vacuum_ran = time.monotonic(), False
        self.locked, self.step, self.max_step, self.server_state = True, 3, 3, "deleting"
        self.paint()
        ordered = [it for row in self.route for it in row]
        self.page.run_thread(self._collect_worker, ordered, self.vacuum_after)

    def _collect_worker(self, ordered, run_vacuum):
        def progress(i, n, label):
            if i < n:
                self.current = ordered[i]["plan"]
                self.route_status[self.current]["status"] = "now"
            self.paint(force=False)

        def on_item(it, status, freed, errors):
            st = self.route_status[it["plan"]]
            st[status] += 1
            st["freed"] += freed
            st["errors"] += errors
            done = st["deleted"] + st["failed"] + st["skipped"]
            if done == len(self.route[[r[0]["plan"] for r in self.route].index(it["plan"])]):
                st["status"] = "failed" if st["failed"] else ("skipped" if st["skipped"] and not st["deleted"] else "done")
            self.paint(force=False)

        self.sess.errors.clear()
        try:
            self.result = core.apply(self.sess, ordered, progress=progress, on_item=on_item)
            if run_vacuum:
                self.current = "vacuum"
                self.paint()
                core.vacuum(self.sess)
                self.vacuum_ran = True
            self.storage_after = core.storage_usage(self.sess)
        except Exception as ex:
            logging.exception("Collect failed")
            self.sess.error(f"Stopped: {ex}")
            self.result = self.result or {"deleted": 0, "skipped": 0, "bytes": 0}
        self.locked, self.current, self.server_state = False, None, "connected"
        self.result["seconds"] = int(time.monotonic() - self.collect_started)
        self.max_step = 3
        self.paint()

    def _bar_and_legend(self, planned):
        by_kind = {k: [it for it in planned if it["kind"] == k] for k in KIND}
        total = sum(it["bytes"] for it in planned) or 1
        segs, entries = [], []
        for k, its in by_kind.items():
            if not its:
                continue
            kb = sum(i["bytes"] for i in its) or 1
            plans = sorted({i["plan"] for i in its})
            freed = sum(self.route_status[p]["freed"] for p in plans)
            failed_b = sum(i["bytes"] for i in its if self.route_status[i["plan"]]["status"] == "failed") - \
                sum(self.route_status[p]["freed"] for p in plans if self.route_status[p]["status"] == "failed")
            skipped_b = sum(i["bytes"] for i in its if self.route_status[i["plan"]]["status"] == "skipped")
            segs.append((kb / total, LINE2, None, [(freed / kb, KIND[k][1], None), (max(0, skipped_b) / kb, None, HATCH),
                                                   (max(0, failed_b) / kb, None, XHATCH),
                                                   (max(0, 1 - (freed + max(0, skipped_b) + max(0, failed_b)) / kb), None, None)]))
            done_n = sum(self.route_status[p]["deleted"] for p in plans)
            entries.append((KIND[k][1], None, {"channel": "Archived channels", "dm": "Deactivated DMs", "orphan": "Orphaned files"}[k],
                            f"{done_n:,}" + ("" if self.result else f" of {len(its):,}"), gb(freed) if self.result else ""))
        skipped = sum(s["skipped"] for s in self.route_status.values())
        failed = sum(s["failed"] for s in self.route_status.values())
        if skipped:
            entries.append((None, HATCH, "Not collected: skipped", skipped, ""))
        if failed:
            entries.append((None, XHATCH, "Not collected: failed", failed, ""))
        return load_bar(segs), legend(entries)

    def _status_cell(self, plan) -> ft.Control:
        st = self.route_status[plan]
        s = st["status"]
        if s == "now" or plan == self.current:
            return ft.Row([spinner(16, PRIMARY), T("Deleting…", color=INK)], spacing=6)
        if s == "queued":
            return ft.Row([ft.Icon(ft.Icons.SCHEDULE_ROUNDED, size=18, color=INK2), T("Queued", color=INK2)], spacing=6)
        parts = []
        if st["deleted"]:
            parts += [ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, size=18, color=OK), T("Deleted" if st["deleted"] == 1 and not (st["failed"] or st["skipped"]) else f"{st['deleted']:,} deleted", color=INK)]
        if st["skipped"]:
            parts += [ft.Icon(ft.Icons.SKIP_NEXT_ROUNDED, size=18, color=INK), T(f"{st['skipped']} skipped" if st["deleted"] else "Skipped", color=INK)]
        if st["failed"]:
            parts += [ft.Icon(ft.Icons.CANCEL_OUTLINED, size=18, color=INK), T(f"{st['failed']} failed" if st["deleted"] else "Failed", color=INK)]
        return ft.Row(parts, spacing=6)

    def _collect(self) -> ft.Control:
        planned = [it for row in self.route for it in row]
        planned_b = sum(it["bytes"] for it in planned)
        freed = sum(s["freed"] for s in self.route_status.values())
        done_items = sum(s["deleted"] + s["failed"] + s["skipped"] for s in self.route_status.values())
        bar, leg = self._bar_and_legend(planned)
        running = self.result is None

        if running:
            elapsed = int(time.monotonic() - self.collect_started)
            head = title_row("Collecting the load…", "Keep this window open. Each item is checked again right before it is deleted.")
            right = T(f"{done_items:,} of {len(planned):,} items · {elapsed // 60} min {elapsed % 60:02d} s", color=INK2)
            sub = "collected of " + gb(planned_b)
        else:
            problems = sum(1 for s in self.route_status.values() if s["status"] in ("failed", "skipped"))
            secs = self.result.get("seconds", 0)
            head = title_row("Done" if not problems else f"Finished, with {problems} problem{'s' if problems > 1 else ''}",
                             f"{time.strftime('%H:%M')} · took {secs // 60} min {secs % 60:02d} s · {self.result['deleted']:,} deleted, "
                             f"{sum(s['skipped'] for s in self.route_status.values())} skipped, "
                             f"{sum(s['failed'] for s in self.route_status.values())} failed")
            right = ft.Row([T("Data directory", color=INK2), B(gb(self.storage_before), 14, INK),
                            ft.Icon(ft.Icons.ARROW_FORWARD_ROUNDED, size=16, color=INK2), B(gb(self.storage_after), 14, INK)], spacing=6) \
                if self.storage_after else T("")
            sub = f"collected of {gb(planned_b)} planned"
        load = panel(ft.Column(spacing=12, controls=[
            ft.Row([T(gb(freed), 32, ft.FontWeight.W_800, INK), T(sub, color=INK2), ft.Container(expand=True), right],
                   vertical_alignment=ft.CrossAxisAlignment.END), bar, leg]),
            padding=ft.Padding.only(left=22, right=22, top=18, bottom=16))

        if running:
            cur_row = next((r for r in self.route if r[0]["plan"] == self.current), None)
            cur_text = "Running VACUUM FULL on FileInfo…" if self.current == "vacuum" else \
                (f"#{self.current:03d} · {cur_row[0]['label'] if cur_row[0]['kind'] != 'orphan' else f'{len(cur_row)} orphaned files'}" if cur_row else "Starting…")
            pct = done_items / len(planned) if planned else 1
            rows = []
            for r in self.route:
                p = r[0]["plan"]
                name = f"{len(r):,} orphaned files" if r[0]["kind"] == "orphan" else r[0]["label"]
                st = self.route_status[p]
                rows.append(ft.DataRow(color=ft.Colors.with_opacity(0.09, PRIMARY) if p == self.current else None, cells=[
                    ft.DataCell(T(f"#{p:03d}", 12, ft.FontWeight.W_600, INK3)),
                    ft.DataCell(T(name, weight=ft.FontWeight.W_600, color=INK if st["status"] != "queued" else INK2)),
                    ft.DataCell(bin_label(r[0]["kind"])), ft.DataCell(self._status_cell(p)),
                    ft.DataCell(T(gb(st["freed"]) if st["freed"] else "–", color=INK))]))
            queued = sum(1 for s in self.route_status.values() if s["status"] == "queued")
            route = panel(ft.Column(spacing=0, expand=True, controls=[
                ft.Container(ft.Row([B("Route", 14, INK), T("in plan order", color=INK2), ft.Container(expand=True),
                                     T(cur_text, 13, color=INK2),
                                     ft.ProgressBar(value=pct, width=160, color=PRIMARY, bgcolor=LINE2, bar_height=6, border_radius=3)], spacing=10),
                             padding=ft.Padding.symmetric(horizontal=16, vertical=12), border=ft.Border.only(bottom=ft.BorderSide(1, LINE))),
                ft.Column([ft.Row([table(["Plan", "Item", "Bin", "Status", "Freed"], rows)])], scroll=ft.ScrollMode.AUTO, expand=True),
                footer(ft.Icons.SWAP_VERT_ROUNDED, f"{queued} rows queued · scroll for more")]), padding=0, expand=True)
            return screen(head, [load, route], left=[note(ft.Icons.LOCK_OUTLINE_ROUNDED,
                          "Navigation is locked while collecting. Closing the window stops after the current item.")])

        # results
        problems = [(r, self.route_status[r[0]["plan"]]) for r in self.route
                    if self.route_status[r[0]["plan"]]["status"] in ("failed", "skipped")]
        deleted = [(r, self.route_status[r[0]["plan"]]) for r in self.route if self.route_status[r[0]["plan"]]["status"] == "done"]
        show = {"problems": problems, "deleted": deleted, "all": [(r, self.route_status[r[0]["plan"]]) for r in self.route]}[self.results_filter]
        rows = []
        for r, st in show:
            name = f"{len(r):,} orphaned files" if r[0]["kind"] == "orphan" else r[0]["label"]
            if st["status"] in ("failed", "skipped"):
                what = (st["errors"][0].split(":")[-1].strip() if st["errors"] else "changed after the scan")
                n_bad = st["failed"] or st["skipped"]
                word = st["status"].title() if len(r) == 1 else f"{n_bad:,} of {len(r):,} {st['status']}"
                happened = ft.Row([ft.Icon(ft.Icons.CANCEL_OUTLINED if st["status"] == "failed" else ft.Icons.SKIP_NEXT_ROUNDED, size=18, color=INK),
                                   ft.Text(spans=[ft.TextSpan(word, ft.TextStyle(weight=ft.FontWeight.W_700)),
                                                  ft.TextSpan(f": {what}")], color=INK, size=14, max_lines=1,
                                           overflow=ft.TextOverflow.ELLIPSIS, width=380)], spacing=6)
                todo = what_to_do(st["status"], st["errors"])
            else:
                happened, todo = self._status_cell(r[0]["plan"]), "–"
            rows.append(ft.DataRow(cells=[ft.DataCell(T(f"#{r[0]['plan']:03d}", 12, ft.FontWeight.W_600, INK3)),
                                          ft.DataCell(bin_label(r[0]["kind"], name)), ft.DataCell(happened),
                                          ft.DataCell(T(todo, 13, color=INK2))]))
        filters = ft.Row(spacing=8, controls=[
            filter_chip(f"Problems {len(problems)}", self.results_filter == "problems", lambda e: self._set_results_filter("problems")),
            filter_chip(f"Deleted {len(deleted):,}", self.results_filter == "deleted", lambda e: self._set_results_filter("deleted")),
            filter_chip(f"All {len(self.route):,}", self.results_filter == "all", lambda e: self._set_results_filter("all"))])
        body = ft.Column([ft.Row([table(["Plan", "Item", "What happened", "What to do"], rows)])], scroll=ft.ScrollMode.AUTO) if rows else \
            ft.Container(T("No problems. Everything in the plan was collected.", color=INK2), padding=16)
        problem_panel = panel(body, padding=0)
        blocks = [load, filters, problem_panel]
        if not self.vacuum_ran:
            blocks.append(panel(ft.Row(spacing=16, controls=[
                ft.Icon(ft.Icons.STORAGE_ROUNDED, size=28, color=PRIMARY),
                ft.Column([B("The database file hasn't shrunk yet", 14, INK),
                           T("Deleted rows free space inside PostgreSQL, but the file on disk only shrinks after VACUUM FULL. "
                             "It locks the FileInfo table while it runs, so do it in a maintenance window.", color=INK2)], spacing=2, expand=True),
                btn("Run VACUUM FULL…", ft.Icons.COMPRESS_ROUNDED, self._on_vacuum, "outline")]),
                padding=20))
        return screen(head, blocks, scroll=True, left=[note(ft.Icons.DESCRIPTION_OUTLINED, f"Full details for every item: {LOG_SHOWN}")],
                      right=[btn("Open log", ft.Icons.DESCRIPTION_OUTLINED, lambda e: webbrowser.open(LOG_FILE.as_uri()), "outline"),
                             btn("Scan again", ft.Icons.RADAR_ROUNDED, self._scan_again)])

    def _set_results_filter(self, f):
        self.results_filter = f
        self.paint()

    def _scan_again(self, e=None):
        self.route, self.route_status, self.result, self.items, self.rows, self.found = [], {}, None, [], [], []
        self.scan_done_kinds, self.scan_msg = set(), ""
        self.max_step, self.step = 1, 1
        self.storage_before = self.storage_after or self.storage_before
        self.paint()

    def _on_vacuum(self, e=None):
        def run(ev):
            self.page.pop_dialog()
            self.toast("Running VACUUM FULL… the FileInfo table is locked until it finishes.")
            self.page.run_thread(worker)

        def worker():
            n = len(self.sess.errors)
            core.vacuum(self.sess)
            self.vacuum_ran = len(self.sess.errors) == n
            self.storage_after = core.storage_usage(self.sess)
            self.paint()
            self.toast("VACUUM FULL finished." if self.vacuum_ran else "VACUUM FULL failed. See the log.")

        self.page.show_dialog(ft.AlertDialog(
            modal=True, bgcolor=SURF, shape=ft.RoundedRectangleBorder(radius=22),
            title=T("Run VACUUM FULL now?", 22, ft.FontWeight.W_800, INK),
            content=T("It rewrites the FileInfo table and locks it until it finishes. Mattermost can't attach or show files "
                      "during that time. Only do this in a maintenance window.", color=INK2, width=440),
            actions=[btn("Cancel", None, lambda ev: self.page.pop_dialog(), "text"),
                     btn("Run VACUUM FULL", ft.Icons.COMPRESS_ROUNDED, run)]))


def main(page: ft.Page, demo: bool = False):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")], force=True)
    page.title = "Binday"
    page.fonts = {"Public Sans": FONT_FILE}
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.theme = ft.Theme(color_scheme=ft.ColorScheme(**LIGHT), font_family="Public Sans", use_material3=True)
    page.dark_theme = ft.Theme(color_scheme=ft.ColorScheme(**DARK), font_family="Public Sans", use_material3=True)
    page.bgcolor = GROUND
    page.padding = 0
    page.window.width, page.window.height = 1280, 800
    page.window.min_width, page.window.min_height = 1000, 680
    theme = _arg("--theme")
    if theme in ("light", "dark"):
        page.theme_mode = ft.ThemeMode.LIGHT if theme == "light" else ft.ThemeMode.DARK
    app = PurgeApp(page, demo=demo)
    page.on_close = lambda e: app.sess and app.sess.close()
    page.on_resize = lambda e: app.paint(force=False)
    if demo and _arg("--demo-screen"):
        page.run_thread(_autoplay, app, _arg("--demo-screen"))


def _arg(name):
    """Value of a `--name=value` command-line flag, or None."""
    return next((a.split("=", 1)[1] for a in sys.argv if a.startswith(name + "=")), None)


def _autoplay(app, screen):
    """Demo only: walk to a screen with synthetic data, for README screenshots (`--demo --demo-screen=review`)."""
    app.f_ssh.value = "root@chat.example.org"
    app.f_profile.value = "prod-chat"
    app._connect_worker(app._form())
    if screen == "connect":
        return
    app.step = 1
    app._scan_worker(set(app.kinds))
    keep = next((i for i, r in enumerate(app.rows) if r[0]["label"] == "payroll-2023"), None)
    if keep is not None:
        app.selected.discard(keep)  # show a kept row
    app.paint()
    if screen == "review":
        return
    app._start_collect(app._sel_items())
    while app.result is None:
        time.sleep(0.1)
    app.result["seconds"] = 552  # synthetic run; the demo skips its delays when autoplaying
    app.paint()


if __name__ == "__main__":
    demo = "--demo" in sys.argv
    if demo:
        import demo as _demo  # patches app.Session & friends with a synthetic server
        _demo.install(fast=bool(_arg("--demo-screen")))
    target = lambda page: main(page, demo)  # noqa: E731
    if "--web" in sys.argv:
        os.environ["FLET_FORCE_WEB_SERVER"] = "true"  # serve only; don't pop open a browser
        print("Serving on http://127.0.0.1:8550", flush=True)
        ft.run(target, view=ft.AppView.WEB_BROWSER, port=8550)
    else:
        ft.run(target)
