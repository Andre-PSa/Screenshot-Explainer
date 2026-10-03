"""Drag a box on the screen -> Claude explains what is in it.

Pick a region with the mouse, and a readable explanation of whatever is in it
appears in a panel: what it is, what the jargon means, and anything that looks
wrong or needs a decision.

    Ctrl+Alt+E   pick a region and explain it
    Ctrl+Alt+R   explain the same region again
    Ctrl+Alt+H   show/hide the panel
    Ctrl+Alt+Q   quit

Run `python screen_explainer.py --help` for the options.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes
import io
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path

import mss
from PIL import Image
from pynput import keyboard

# --------------------------------------------------------------------------- #
# Defaults (every one of these is also a command-line flag)
# --------------------------------------------------------------------------- #

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
MAX_IMAGE_EDGE = 1568
SHOTS_DIR = Path(__file__).resolve().parent / "screenshots"

# Claude-ish palette: warm near-black, coral accent, soft bone text.
ACCENT = "#D97757"
BG = "#1B1A19"
FG = "#D8D4C9"
ERROR_FG = "#E4766B"


def mix(fg: str, bg: str, amount: float) -> str:
    """Blend two #rrggbb colours - `amount` is how much of `fg` to keep."""
    a = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(
        round(x * amount + y * (1 - amount)) for x, y in zip(a, b)
    )


HOTKEY_SELECT = "<ctrl>+<alt>+e"
HOTKEY_REPEAT = "<ctrl>+<alt>+r"
HOTKEY_TOGGLE = "<ctrl>+<alt>+h"
HOTKEY_QUIT = "<ctrl>+<alt>+q"

SYSTEM_PROMPT = (
    "You explain what is on someone's screen. They have cropped out the part "
    "they care about, so every pixel you were given is relevant - read it "
    "closely and explain the substance, not the window decoration. Be concrete "
    "and specific: quote the actual labels, numbers, identifiers and terms that "
    "are visible rather than describing them in the abstract. Never say 'the "
    "image shows' or 'this screenshot contains' - just explain the thing "
    "itself. If you cannot read something, say so plainly instead of guessing."
)

USER_PROMPT = """Explain what is in this part of my screen.

Format your reply exactly like this, as plain text:

A single sentence saying what this is and why it matters.

- Four to six short bullets covering what is going on.
- Explain any jargon, symbols, notation or abbreviations that are visible.
- Work through the reasoning for anything being asked or calculated - do not
  just state a conclusion.
- Translate the key parts if the text is not in English.
- End with anything that looks wrong, risky, or that needs a decision.

Use **bold** for terms worth highlighting. No headings, no preamble, no
sign-off. Keep the whole thing under 200 words."""

# --------------------------------------------------------------------------- #
# Screen geometry and capture
# --------------------------------------------------------------------------- #

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
SPI_GETWORKAREA = 0x0030


def work_area() -> tuple[int, int, int, int] | None:
    """Primary monitor rect with the taskbar excluded: (left, top, right, bottom)."""
    rect = ctypes.wintypes.RECT()
    ok = ctypes.windll.user32.SystemParametersInfoW(
        SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
    )
    return (rect.left, rect.top, rect.right, rect.bottom) if ok else None


def virtual_screen() -> dict:
    """Bounding box covering every monitor, in absolute screen coordinates."""
    with mss.MSS() as sct:
        return dict(sct.monitors[0])


def grab_region(region: dict) -> Image.Image:
    with mss.MSS() as sct:
        shot = sct.grab(region)
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def encode_png(img: Image.Image, max_edge: int = MAX_IMAGE_EDGE) -> bytes:
    # Small crops are left alone: upscaling never adds detail, and shrinking a
    # region that is already small would throw away the text we need to read.
    scale = min(1.0, max_edge / max(img.size))
    if scale < 1.0:
        img = img.resize(
            (round(img.width * scale), round(img.height * scale)), Image.LANCZOS
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Region selector
# --------------------------------------------------------------------------- #


class RegionSelector:
    """Dims every monitor and lets the user drag out a rectangle.

    Runs modally on the Tk thread via wait_window, so the caller simply gets a
    region back (or None if it was cancelled with Escape or right-click).
    """

    MIN_SIDE = 8  # a click rather than a drag

    def __init__(self, root: tk.Tk, tint: str, opacity: float,
                 accent: str = ACCENT) -> None:
        self.root = root
        self.tint = tint
        self.opacity = opacity
        self.accent = accent

    def select(self) -> dict | None:
        bounds = virtual_screen()
        self.origin = (bounds["left"], bounds["top"])
        self.start: tuple[int, int] | None = None
        self.region: dict | None = None

        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", self.opacity)
        self.win.configure(bg=self.tint, cursor="crosshair")
        self.win.geometry(
            f"{bounds['width']}x{bounds['height']}+{bounds['left']}+{bounds['top']}"
        )

        self.canvas = tk.Canvas(
            self.win, bg=self.tint, highlightthickness=0, cursor="crosshair"
        )
        self.canvas.pack(fill="both", expand=True)
        self.rect = None
        self.readout = None
        self.hint = self.canvas.create_text(
            bounds["width"] // 2,
            36,
            text="drag over the part you want explained    ·    Esc to cancel",
            fill=self.accent,
            font=("Segoe UI", 13),
        )

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Escape>", self._cancel)
        self.win.bind("<Button-3>", self._cancel)

        self.win.focus_force()
        self.win.grab_set()  # keep every key and click inside the selector
        self.root.wait_window(self.win)
        return self.region

    # -- mouse ------------------------------------------------------------- #

    def _on_press(self, event) -> None:
        self.start = (event.x, event.y)
        self.canvas.delete(self.hint)
        self.rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline=self.accent, width=2
        )
        self.readout = self.canvas.create_text(
            event.x, event.y, text="", fill=self.accent,
            font=("Segoe UI", 11, "bold"), anchor="sw",
        )

    def _on_drag(self, event) -> None:
        if not self.start:
            return
        x0, y0 = self.start
        self.canvas.coords(self.rect, x0, y0, event.x, event.y)
        w, h = abs(event.x - x0), abs(event.y - y0)
        self.canvas.coords(
            self.readout, min(x0, event.x), min(y0, event.y) - 6
        )
        self.canvas.itemconfigure(self.readout, text=f"{w} x {h}")

    def _on_release(self, event) -> None:
        if self.start:
            x0, y0 = self.start
            left, top = min(x0, event.x), min(y0, event.y)
            width, height = abs(event.x - x0), abs(event.y - y0)
            if width >= self.MIN_SIDE and height >= self.MIN_SIDE:
                self.region = {
                    "left": self.origin[0] + left,
                    "top": self.origin[1] + top,
                    "width": width,
                    "height": height,
                }
        self._close()

    def _cancel(self, _event=None) -> None:
        self.region = None
        self._close()

    def _close(self) -> None:
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        self.win.destroy()


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #


def build_prompt(instruction: str | None) -> str:
    if not instruction:
        return USER_PROMPT
    return f"{USER_PROMPT}\n\nOn top of that, the user specifically asks: {instruction}"


class ApiBackend:
    """Sends the crop straight to the Claude API."""

    name = "api"

    def __init__(self, args: argparse.Namespace) -> None:
        import anthropic  # imported lazily so --backend cli needs no API key

        self.client = anthropic.Anthropic()
        self.args = args

    def ask(self, png: bytes) -> str:
        response = self.client.messages.create(
            model=self.args.model,
            max_tokens=self.args.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.standard_b64encode(png).decode(),
                            },
                        },
                        {"type": "text", "text": build_prompt(self.args.instruction)},
                    ],
                }
            ],
            thinking={"type": "adaptive"},
            output_config={"effort": self.args.effort},
        )

        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "category", None)
            raise RuntimeError(f"model declined ({detail})")

        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise RuntimeError(f"empty reply (stop_reason={response.stop_reason})")
        return text


class CliBackend:
    """Shells out to the Claude Code CLI, which reads the crop off disk.

    Slower than the API backend, but it uses the CLI's own login, so there is
    no API key to configure.
    """

    name = "cli"

    def __init__(self, args: argparse.Namespace) -> None:
        self.exe = args.claude_path or find_claude_cli()
        if not self.exe:
            raise RuntimeError(
                "could not find the `claude` CLI - pass --claude-path C:\\...\\claude.exe"
            )
        self.args = args
        self.tmpdir = Path(tempfile.gettempdir()) / "screen-explainer"
        self.tmpdir.mkdir(exist_ok=True)

    def ask(self, png: bytes) -> str:
        crop = self.tmpdir / "crop.png"
        crop.write_bytes(png)
        prompt = (
            f"{SYSTEM_PROMPT}\n\nRead the image at {crop}.\n"
            f"{build_prompt(self.args.instruction)}"
        )
        proc = subprocess.run(
            [self.exe, "-p", prompt, "--allowedTools", "Read",
             "--model", self.args.model],
            capture_output=True,
            text=True,
            # The CLI writes UTF-8; without this Python decodes it with the
            # system codepage (cp1252 here) and mangles every accent and dash.
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "claude CLI failed").strip()[:300])
        text = proc.stdout.strip()
        if not text:
            raise RuntimeError("the CLI returned nothing")
        return text


def find_claude_cli() -> str | None:
    """PATH first, then the usual Windows install spots.

    The VSCode extension ships its own binary under a version-stamped folder
    that changes on every update, so that one is globbed rather than hardcoded.
    """
    found = shutil.which("claude")
    if found:
        return found

    home = Path.home()
    candidates = [
        *sorted(
            home.glob(
                ".vscode/extensions/anthropic.claude-code-*/resources/"
                "native-binary/claude.exe"
            ),
            reverse=True,  # newest extension version first
        ),
        home / ".local" / "bin" / "claude.exe",
        home / ".claude" / "local" / "claude.exe",
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
    ]
    return next((str(p) for p in candidates if p.is_file()), None)


# --------------------------------------------------------------------------- #
# Hotkeys
# --------------------------------------------------------------------------- #


def char_to_vk(ch: str) -> int | None:
    """Virtual-key code for a character on the *current* keyboard layout."""
    user32 = ctypes.windll.user32
    res = user32.VkKeyScanExW(ord(ch), user32.GetKeyboardLayout(0))
    return None if res == -1 else res & 0xFF


class Hotkeys:
    """Global hotkeys, matched by virtual-key code.

    Matching the character pynput reports breaks on non-US layouts: on a
    Portuguese keyboard '~' and '´' are dead keys that produce no character
    until you press a second key, so they never match. The vk is what the
    hardware sends and is layout-proof, so single-key bindings use that.
    Specs containing '+' or '<' still use pynput's combination syntax.
    """

    def __init__(self, bindings: dict) -> None:
        self.by_vk: dict[int, object] = {}
        combos = {}
        for spec, fn in bindings.items():
            if "+" in spec or "<" in spec:
                combos[spec] = fn
            elif spec.lower().startswith("vk:"):
                self.by_vk[int(spec[3:])] = fn
            elif len(spec) == 1:
                vk = char_to_vk(spec)
                if vk is None:
                    raise RuntimeError(
                        f"{spec!r} is not typeable on the current keyboard "
                        f"layout - bind it as vk:<number> instead"
                    )
                self.by_vk[vk] = fn
            else:
                raise RuntimeError(f"cannot understand the hotkey {spec!r}")

        self.single = keyboard.Listener(on_press=self._on_press) if self.by_vk else None
        self.combos = keyboard.GlobalHotKeys(combos) if combos else None

    def _on_press(self, key) -> None:
        fn = self.by_vk.get(getattr(key, "vk", None))
        if fn:
            fn()

    def start(self) -> None:
        for listener in (self.single, self.combos):
            if listener:
                listener.start()

    def stop(self) -> None:
        for listener in (self.single, self.combos):
            if listener:
                try:
                    listener.stop()
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# Explanation panel
# --------------------------------------------------------------------------- #


class Panel:
    """A dark card with an accent border that renders the explanation.

    The accent carries the structure - border, summary line, bullet markers
    and **bold** terms - while the running prose stays in a soft off-white.
    Body text set entirely in the accent reads badly at this size, so it is
    used for emphasis rather than for everything.
    """

    def __init__(self, root: tk.Tk, args: argparse.Namespace) -> None:
        self.root = root
        self.args = args
        self.last: str | None = None
        self.anim_job = None
        self.type_job = None
        self.segments: list[tuple[str, tuple]] = []
        self.needs_typing = False
        self.cursor_shown = False
        accent, bg, fg = args.accent, args.bg, args.fg
        self._line_h = tkfont.Font(
            root=root, font=(args.font, args.font_size)
        ).metrics("linespace")

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", args.opacity)
        self.win.configure(bg=accent)  # the border, showing through the padding
        if args.transparent:
            # Windows knocks out every pixel of exactly this colour, so the
            # background disappears and only the border and glyphs remain.
            # Those holes are click-through as well.
            self.win.attributes("-transparentcolor", bg)

        frame = tk.Frame(self.win, bg=bg)
        frame.pack(fill="both", expand=True, padx=args.border, pady=args.border)

        self.header = tk.Label(
            frame,
            text="",
            bg=bg,
            fg=accent,
            font=(args.font, 8),
            anchor="w",
            padx=16,
            pady=7,
        )
        self.header.pack(fill="x")
        tk.Frame(frame, bg=mix(accent, bg, 0.35), height=1).pack(fill="x")

        body = tk.Frame(frame, bg=bg)
        body.pack(fill="both", expand=True, pady=(10, 12))

        self.text = tk.Text(
            body,
            bg=bg,
            fg=fg,
            font=(args.font, args.font_size),
            wrap="word",
            relief="flat",
            highlightthickness=0,
            padx=16,
            pady=0,
            spacing1=2,
            spacing3=6,
            cursor="arrow",
            insertwidth=0,
            selectbackground=mix(accent, bg, 0.45),
            selectforeground=fg,
            width=1,   # the window width is driven by --width, not by columns
            height=1,
        )
        self.scroll = tk.Scrollbar(
            body,
            command=self.text.yview,
            width=10,
            relief="flat",
            borderwidth=0,
            troughcolor=bg,
            bg=mix(accent, bg, 0.5),
            activebackground=accent,
            highlightthickness=0,
        )
        self.text.configure(yscrollcommand=self._on_scroll)
        self.text.pack(side="left", fill="both", expand=True)

        f = (args.font, args.font_size)
        self.text.tag_configure(
            "summary", font=(args.font, args.font_size + 1, "bold"),
            foreground=accent, spacing3=12, lmargin1=0, lmargin2=0,
        )
        self.text.tag_configure("body", font=f, foreground=fg)
        self.text.tag_configure("bold", font=(args.font, args.font_size, "bold"),
                                foreground=accent)
        self.text.tag_configure("marker", foreground=accent,
                                font=(args.font, args.font_size, "bold"))
        self.text.tag_configure("bullet", font=f, foreground=fg,
                                lmargin1=10, lmargin2=26, spacing3=7)
        self.text.tag_configure("error", font=f, foreground=args.error_fg)
        # Tk resolves clashing tags by creation order, not by the order they
        # are applied, so the emphasis tags have to outrank the block tags.
        self.text.tag_raise("bold")
        self.text.tag_raise("marker")

        # Wheel scrolling without the panel having to hold keyboard focus.
        for widget in (self.text, self.header, frame):
            widget.bind("<MouseWheel>", self._on_wheel)
        for widget in (self.header, frame):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        self.win.withdraw()
        self._make_unobtrusive()

    def _make_unobtrusive(self) -> None:
        """Never steal keyboard focus, stay out of the taskbar and Alt+Tab.

        Deliberately NOT click-through: the text needs to be scrollable and the
        panel draggable, so it has to receive the mouse.
        """
        try:
            hwnd = self.win.winfo_id()
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            )
        except Exception:
            pass  # cosmetic only - the panel still works without it

    # -- rendering ---------------------------------------------------------- #

    def render(self, text: str, header: str = "", error: bool = False) -> None:
        """Lay the whole thing out at once.

        The full text is drawn even when it is about to be typed out, so the
        panel can be measured and sized before a single character is shown -
        otherwise it would grow line by line as it types.
        """
        self.last = text
        self.header.configure(text=header)
        self.segments = (
            self._inline_segments(text, "error") if error
            else self._markdown_segments(text)
        )
        self._draw_all()
        self.needs_typing = self.args.type_cps > 0

    def _draw_all(self) -> None:
        self._cancel_typing()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for chunk, tags in self.segments:
            self.text.insert("end", chunk, tags)
        self.text.configure(state="disabled")

    def _markdown_segments(self, text: str) -> list[tuple[str, tuple]]:
        """Light markdown: first line is the summary, '-' lines are bullets."""
        segments: list[tuple[str, tuple]] = []
        summary_done = False
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue

            if stripped.startswith(("- ", "* ", "• ")):
                block = "bullet"
                segments.append(("▸  ", ("bullet", "marker")))
                segments += self._inline_segments(stripped[2:].strip(), block)
            elif stripped.startswith("#"):
                block = "summary"
                segments += self._inline_segments(stripped.lstrip("# ").strip(), block)
                summary_done = True
            elif not summary_done:
                block = "summary"
                segments += self._inline_segments(stripped, block)
                summary_done = True
            else:
                block = "body"
                segments += self._inline_segments(stripped, block)
            # The newline carries the block tag so its spacing3 applies.
            segments.append(("\n", (block,)))
        return segments

    def _inline_segments(self, line: str, tag: str) -> list[tuple[str, tuple]]:
        """Split a line into runs, turning **bold** into the bold tag."""
        return [
            (part, (tag, "bold") if i % 2 else (tag,))
            for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", line))
            if part
        ]

    # -- typewriter ---------------------------------------------------------- #

    CURSOR = "▌"

    def _start_typing(self) -> None:
        self.needs_typing = False
        self._cancel_typing()
        self.seg = self.pos = 0
        self.cursor_shown = False
        self.per_tick = max(1, round(self.args.type_cps * 0.016))
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._type_step()

    def _type_step(self) -> None:
        self.text.configure(state="normal")
        self._drop_cursor()

        budget = self.per_tick
        while budget > 0 and self.seg < len(self.segments):
            chunk, tags = self.segments[self.seg]
            take = min(budget, len(chunk) - self.pos)
            self.text.insert("end", chunk[self.pos:self.pos + take], tags)
            self.pos += take
            budget -= take
            if self.pos >= len(chunk):
                self.seg += 1
                self.pos = 0

        done = self.seg >= len(self.segments)
        if not done:
            self.text.insert("end", self.CURSOR, ("marker",))
            self.cursor_shown = True
        self.text.configure(state="disabled")
        self.text.see("end")
        self.type_job = None if done else self.root.after(16, self._type_step)

    def _drop_cursor(self) -> None:
        if not self.cursor_shown:
            return
        try:
            self.text.delete("end-2c", "end-1c")  # end-1c is the implicit \n
        except tk.TclError:
            pass
        self.cursor_shown = False

    def finish_typing(self) -> None:
        """Jump straight to the finished text."""
        if self.type_job is None and not self.needs_typing:
            return
        self._draw_all()
        self.text.see("1.0")

    def _cancel_typing(self) -> None:
        if getattr(self, "type_job", None) is not None:
            self.root.after_cancel(self.type_job)
            self.type_job = None
        self.cursor_shown = False

    def _fit(self) -> int:
        """Total pixel height that fits the content, capped; scrollbar past that.

        The window must already be at its final width - how the text wraps is a
        function of the pixel width, and the Text is one character wide until
        the toplevel has been sized. Sizing the Text in *lines* also gets this
        wrong: `height` counts base-font lines and ignores the per-tag
        paragraph spacing, leaving a band of dead space under the last bullet.
        So measure the rendered text in pixels and size the window directly.
        """
        content = self._content_pixels()
        shown = max(self._line_h, min(content, self.args.max_lines * self._line_h))
        if content > shown:
            self.scroll.pack(side="right", fill="y")
        else:
            self.scroll.pack_forget()
        return self._chrome_pixels() + shown

    def _content_pixels(self) -> int:
        value = self.text.count("1.0", "end", "ypixels")
        if isinstance(value, tuple):
            value = value[0]
        return max(self._line_h, int(value or self._line_h))

    def _chrome_pixels(self) -> int:
        """Everything that is not the text: header, rule, padding, border."""
        self.text.configure(height=1)
        self.win.update_idletasks()
        return max(0, self.win.winfo_reqheight() - self._line_h)

    # -- visibility --------------------------------------------------------- #

    def is_visible(self) -> bool:
        return self.win.state() != "withdrawn"

    def show(self) -> None:
        was_hidden = not self.is_visible()
        self._cancel_animation()
        self.win.deiconify()
        # Re-assert topmost before positioning: on Windows this call moves the
        # window, so doing it afterwards snaps the panel back to 0,0.
        self.win.attributes("-topmost", True)
        self.win.lift()
        # Width first, then measure, then the real height - see _fit.
        self.win.geometry(f"{self.args.width}x400")
        self.win.update_idletasks()
        height = self._fit()
        x, y = self._place(height)

        # Clear it now so the unroll reveals an empty card, not a flash of the
        # finished text that is about to be typed out.
        if self.needs_typing:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")

        if was_hidden and self.args.animate_ms > 0:
            self._animate_in(x, y, height, on_done=self._after_reveal)
        else:
            # Already open - resizing in place, so don't replay the animation.
            self.win.attributes("-alpha", self.args.opacity)
            self.win.geometry(f"{self.args.width}x{height}+{x}+{y}")
            self._after_reveal()
        self.win.update_idletasks()

    def _after_reveal(self) -> None:
        if self.needs_typing:
            self._start_typing()

    # -- appear animation ---------------------------------------------------- #

    def _animate_in(self, x: int, y: int, height: int, on_done=None) -> None:
        """Unroll downwards from the panel's top edge, fading in as it goes."""
        frames = max(2, self.args.animate_ms // 16)
        fade = not self.args.transparent  # alpha fights -transparentcolor

        def step(i: int) -> None:
            t = min(1.0, (i + 1) / frames)
            eased = 1 - (1 - t) ** 3  # ease-out cubic
            self.win.geometry(
                f"{self.args.width}x{max(1, round(height * eased))}+{x}+{y}"
            )
            if fade:
                self.win.attributes(
                    "-alpha", self.args.opacity * (0.25 + 0.75 * eased)
                )
            if t < 1.0:
                self.anim_job = self.root.after(16, step, i + 1)
            else:
                self.anim_job = None
                if on_done:
                    on_done()

        if fade:
            self.win.attributes("-alpha", 0.0)
        step(0)

    def _cancel_animation(self) -> None:
        if self.anim_job is not None:
            self.root.after_cancel(self.anim_job)
            self.anim_job = None

    def _place(self, height: int) -> tuple[int, int]:
        if self.args.position:
            return self.args.position
        w, h = self.args.width, height
        left, top, right, bottom = work_area() or (
            0, 0, self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        )
        m = self.args.margin
        x = {
            "l": left + m,
            "c": left + (right - left - w) // 2,
            "r": right - w - m,
        }[self.args.corner[1]]
        y = top + m if self.args.corner[0] == "t" else bottom - h - m
        return x, y

    def hide(self) -> None:
        self._cancel_animation()
        self._cancel_typing()
        self.win.withdraw()

    # -- interaction -------------------------------------------------------- #

    def _on_scroll(self, first, last) -> None:
        self.scroll.set(first, last)

    def _on_wheel(self, event) -> None:
        self.text.yview_scroll(-event.delta // 120, "units")

    def _drag_start(self, event) -> None:
        self._drag_from = (event.x_root, event.y_root,
                           self.win.winfo_x(), self.win.winfo_y())

    def _drag_move(self, event) -> None:
        x0, y0, wx, wy = self._drag_from
        self.args.position = (wx + event.x_root - x0, wy + event.y_root - y0)
        self.win.geometry("+{}+{}".format(*self.args.position))


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #


class App:
    def __init__(self, args: argparse.Namespace, backend) -> None:
        self.args = args
        self.backend = backend
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.stopping = False
        self.selecting = False
        self.last_region: dict | None = None
        self.spinner_job = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = Panel(self.root, args)
        self.selector = RegionSelector(
            self.root, args.tint, args.tint_opacity, args.accent
        )

        self.listener = Hotkeys(
            {
                args.hotkey: lambda: self.events.put(("select", None)),
                args.hotkey_repeat: lambda: self.events.put(("repeat", None)),
                args.hotkey_toggle: lambda: self.events.put(("toggle", None)),
                args.hotkey_quit: lambda: self.events.put(("quit", None)),
            }
        )

    # -- event pump (runs on the Tk thread) --------------------------------- #

    def run(self) -> None:
        self.listener.start()
        self.log(
            f"ready - {self.args.hotkey} to pick a region, "
            f"{self.args.hotkey_repeat} to repeat it, "
            f"{self.args.hotkey_toggle} to show/hide, {self.args.hotkey_quit} to quit "
            f"[{self.backend.name}: {self.args.model}]"
        )
        if self.args.save_shots:
            self.log(f"crops -> {self.args.save_shots}")
        self.root.after(40, self._pump)
        self.root.mainloop()

    def _pump(self) -> None:
        try:
            while not self.stopping:
                kind, payload = self.events.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        if not self.stopping:
            self.root.after(40, self._pump)

    def _handle(self, kind: str, payload) -> None:
        if kind == "select":
            self._start_select()
        elif kind == "repeat":
            self._start_repeat()
        elif kind == "thinking":
            self._start_spinner()
        elif kind == "result":
            self._show_result(payload)
        elif kind == "error":
            self._stop_spinner()
            self.busy = False
            self.log(f"error: {payload}")
            self.panel.render(str(payload), "failed", error=True)
            self.panel.show()
        elif kind == "toggle":
            if self.panel.is_visible() and self.panel.type_job is not None:
                self.panel.finish_typing()  # first press skips the typing
            elif self.panel.is_visible():
                self.panel.hide()
            elif self.panel.last:
                self.panel.show()
        elif kind == "quit":
            self.stopping = True  # stops the pump touching destroyed widgets
            self.listener.stop()
            self.root.destroy()

    # -- capture ------------------------------------------------------------ #

    def _start_select(self) -> None:
        if self.busy or self.selecting:
            return
        self.selecting = True
        was_visible = self.panel.is_visible()
        self.panel.hide()  # keep the panel out of the crop
        self.root.update_idletasks()
        try:
            region = self.selector.select()
        finally:
            self.selecting = False
        if not region:
            self.log("selection cancelled")
            if was_visible:
                self.panel.show()
            return
        self.last_region = region
        self._dispatch(region)

    def _start_repeat(self) -> None:
        if self.busy or self.selecting:
            return
        if not self.last_region:
            self.log("no region picked yet - use " + self.args.hotkey)
            return
        self.panel.hide()
        self.root.update_idletasks()
        self._dispatch(self.last_region)

    def _dispatch(self, region: dict) -> None:
        self.busy = True
        self.log(
            f"explaining {region['width']}x{region['height']} at "
            f"({region['left']}, {region['top']})"
        )
        # Let the selector's dim layer actually disappear before grabbing.
        self.root.after(120, lambda: threading.Thread(
            target=self._worker, args=(region,), daemon=True
        ).start())

    def _worker(self, region: dict) -> None:
        started = time.monotonic()
        try:
            png = encode_png(grab_region(region), self.args.max_edge)
            if self.args.save_shots:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
                (Path(self.args.save_shots) / f"{stamp}.png").write_bytes(png)
            self.events.put(("thinking", None))
            text = self.backend.ask(png)
            self.events.put(("result", {
                "text": text,
                "elapsed": time.monotonic() - started,
                "region": region,
            }))
        except Exception as exc:
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _show_result(self, payload: dict) -> None:
        self._stop_spinner()
        self.busy = False
        region = payload["region"]
        text = payload["text"]
        header = (f"{region['width']}x{region['height']}  ·  "
                  f"{payload['elapsed']:.1f}s  ·  {self.args.model}")
        self.log(f"done in {payload['elapsed']:.1f}s, {len(text)} chars")
        self.panel.render(text, header)
        self.panel.show()

    # -- progress ----------------------------------------------------------- #

    def _start_spinner(self) -> None:
        self.spin_started = time.monotonic()
        self.panel.render("Reading the screen…", "working", error=False)
        self.panel.show()
        self._tick_spinner()

    def _tick_spinner(self) -> None:
        if not self.busy:
            return
        dots = "." * (1 + int(time.monotonic() * 2) % 3)
        self.panel.header.configure(
            text=f"working{dots}  ·  {time.monotonic() - self.spin_started:.0f}s"
        )
        self.spinner_job = self.root.after(400, self._tick_spinner)

    def _stop_spinner(self) -> None:
        if self.spinner_job is not None:
            self.root.after_cancel(self.spinner_job)
            self.spinner_job = None

    def log(self, message: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def coords(value: str) -> tuple[int, int]:
    x, y = value.split(",")
    return int(x), int(y)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drag a box on the screen -> Claude explains what is in it.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--backend", choices=["api", "cli"], default="cli",
                   help="cli = the claude CLI (no API key); api = the Claude API")
    p.add_argument("--claude-path", metavar="EXE",
                   help="path to claude.exe, if it is not found automatically")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                   default="high", help="how hard the model thinks (api backend)")
    p.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    p.add_argument("--instruction", metavar="TEXT",
                   help="extra standing instruction, e.g. \"answer in Portuguese\"")

    p.add_argument("--hotkey", default=HOTKEY_SELECT,
                   help="a single key ('.'), a vk code ('vk:190'), or a pynput "
                        "combination ('<ctrl>+<alt>+e')")
    p.add_argument("--hotkey-repeat", default=HOTKEY_REPEAT,
                   help="re-explain the last region without picking it again")
    p.add_argument("--hotkey-toggle", default=HOTKEY_TOGGLE)
    p.add_argument("--hotkey-quit", default=HOTKEY_QUIT)

    p.add_argument("--max-edge", type=int, default=MAX_IMAGE_EDGE,
                   help="downscale the crop to this long edge before sending")
    p.add_argument("--save-shots", metavar="DIR", default=str(SHOTS_DIR),
                   help="folder every crop is kept in")
    p.add_argument("--no-save-shots", dest="save_shots", action="store_const",
                   const=None, help="don't keep the crops")

    p.add_argument("--corner", choices=["tl", "tc", "tr", "bl", "bc", "br"],
                   default="br",
                   help="where the panel sits: [t]op/[b]ottom + [l]eft/[c]entre/[r]ight")
    p.add_argument("--position", type=coords, metavar="X,Y",
                   help="exact pixel position, overrides --corner")
    p.add_argument("--margin", type=int, default=16)
    p.add_argument("--width", type=int, default=520, help="panel width in pixels")
    p.add_argument("--max-lines", type=int, default=26,
                   help="panel height cap, in lines; it scrolls past this")
    p.add_argument("--animate-ms", type=int, default=220, metavar="MS",
                   help="how long the panel takes to unroll; 0 to appear instantly")
    p.add_argument("--type-cps", type=int, default=320, metavar="CPS",
                   help="typewriter speed in characters per second; 0 shows the "
                        "text all at once")
    p.add_argument("--font", default="Segoe UI")
    p.add_argument("--font-size", type=int, default=10)
    p.add_argument("--opacity", type=float, default=0.97)

    p.add_argument("--accent", default=ACCENT, metavar="#RRGGBB",
                   help="border, summary, bullets and bold terms")
    p.add_argument("--bg", default=BG, metavar="#RRGGBB", help="panel background")
    p.add_argument("--fg", default=FG, metavar="#RRGGBB", help="body text")
    p.add_argument("--error-fg", default=ERROR_FG, metavar="#RRGGBB")
    p.add_argument("--border", type=int, default=2, help="accent border thickness")
    p.add_argument("--light", action="store_true",
                   help="light theme instead: dark text on white")
    p.add_argument("--transparent", action="store_true",
                   help="knock the background out entirely, leaving the border "
                        "and the text floating over whatever is behind")

    p.add_argument("--tint", default="#0C0B0A", help="colour the screen dims to")
    p.add_argument("--tint-opacity", type=float, default=0.45)

    args = p.parse_args(argv)
    if args.light:
        defaults = {"accent": ACCENT, "bg": BG, "fg": FG}
        # Only override colours the user did not set explicitly.
        for name, light in (("bg", "#FFFFFF"), ("fg", "#1B1A19"),
                            ("accent", "#B7492A")):
            if getattr(args, name) == defaults[name]:
                setattr(args, name, light)
    return args


def main() -> int:
    args = parse_args()

    # Match Tk's coordinates to the physical pixels mss captures.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    if args.save_shots:
        Path(args.save_shots).mkdir(parents=True, exist_ok=True)

    if args.backend == "api" and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        print(
            "ANTHROPIC_API_KEY is not set.\n"
            "  set it for this window:  $env:ANTHROPIC_API_KEY = 'sk-ant-...'\n"
            "  or just use the default --backend cli, which needs no key.",
            file=sys.stderr,
        )
        return 1

    try:
        backend = ApiBackend(args) if args.backend == "api" else CliBackend(args)
    except Exception as exc:
        print(f"could not start the {args.backend} backend: {exc}", file=sys.stderr)
        return 1

    App(args, backend).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
