
#!/usr/bin/env python3
"""
vt100_viewer_live.py

A textual ANSI / VT100 byte stream viewer with:
- top pane: emulated screen
- bottom pane: decoded event log
- built-in demo stream
- file input
- hex-text input
- live stdin input (pipe data in)
- ASCII side view for the selected event
- basic CSI handling useful for debugging terminal traffic

Keys:
    q      quit
    s      step one parsed token
    n      step one raw byte
    g      run continuously
    p      pause
    r      reset/replay from buffered data
    j/k    move selected log line down/up
    G      jump to end / parse all buffered data
    h      toggle help footer

Examples:
    python vt100_viewer_live.py
    python vt100_viewer_live.py capture.bin
    python vt100_viewer_live.py capture.bin --head-lines 24
    python vt100_viewer_live.py --hex "48 65 6C 6C 6F 0D 0A 1B 5B 32 4A"
    telnet towel.blinkenlights.nl 23 | tee capture.bin | python vt100_viewer_live.py --stdin
"""

from __future__ import annotations

import argparse
import curses
import fcntl
import locale
import os
import select
import string
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional


ESC = 0x1B


@dataclass
class Event:
    offset: int
    raw: bytes
    kind: str
    description: str


@dataclass
class Cell:
    ch: str = " "
    raw_byte: Optional[int] = 0x20
    fg: Optional[int] = None
    bg: Optional[int] = None
    bold: bool = False


@dataclass
class ScreenBuffer:
    rows: int
    cols: int
    cursor_row: int = 0
    cursor_col: int = 0
    current_fg: Optional[int] = None
    current_bg: Optional[int] = None
    current_bold: bool = False
    cells: List[List[Cell]] = field(init=False)

    def __post_init__(self) -> None:
        self.cells = [self._blank_row() for _ in range(self.rows)]

    def _blank_cell(self) -> Cell:
        return Cell(raw_byte=0x20, fg=self.current_fg, bg=self.current_bg, bold=self.current_bold)

    def _blank_row(self) -> List[Cell]:
        return [self._blank_cell() for _ in range(self.cols)]

    def reset(self) -> None:
        self.cursor_row = 0
        self.cursor_col = 0
        self.current_fg = None
        self.current_bg = None
        self.current_bold = False
        self.cells = [self._blank_row() for _ in range(self.rows)]

    def clamp_cursor(self) -> None:
        self.cursor_row = max(0, min(self.rows - 1, self.cursor_row))
        self.cursor_col = max(0, min(self.cols - 1, self.cursor_col))

    def put_char(self, ch: str, raw_byte: Optional[int] = None) -> None:
        if len(ch) != 1:
            return
        self.cells[self.cursor_row][self.cursor_col] = Cell(
            ch=ch,
            raw_byte=raw_byte,
            fg=self.current_fg,
            bg=self.current_bg,
            bold=self.current_bold,
        )
        if self.cursor_col < self.cols - 1:
            self.cursor_col += 1
        else:
            self.cursor_col = 0
            if self.cursor_row < self.rows - 1:
                self.cursor_row += 1
            else:
                self.scroll_up()

    def scroll_up(self) -> None:
        self.cells.pop(0)
        self.cells.append(self._blank_row())
        self.cursor_row = self.rows - 1
        self.cursor_col = 0

    def carriage_return(self) -> None:
        self.cursor_col = 0

    def line_feed(self) -> None:
        if self.cursor_row < self.rows - 1:
            self.cursor_row += 1
        else:
            self.scroll_up()

    def backspace(self) -> None:
        if self.cursor_col > 0:
            self.cursor_col -= 1

    def tab(self, width: int = 8) -> None:
        next_tab = ((self.cursor_col // width) + 1) * width
        self.cursor_col = min(next_tab, self.cols - 1)

    def cursor_up(self, n: int = 1) -> None:
        self.cursor_row -= max(1, n)
        self.clamp_cursor()

    def cursor_down(self, n: int = 1) -> None:
        self.cursor_row += max(1, n)
        self.clamp_cursor()

    def cursor_right(self, n: int = 1) -> None:
        self.cursor_col += max(1, n)
        self.clamp_cursor()

    def cursor_left(self, n: int = 1) -> None:
        self.cursor_col -= max(1, n)
        self.clamp_cursor()

    def cursor_position(self, row: int, col: int) -> None:
        self.cursor_row = max(0, min(self.rows - 1, row - 1))
        self.cursor_col = max(0, min(self.cols - 1, col - 1))

    def erase_display(self, mode: int = 0) -> None:
        if mode == 2:
            self.cells = [self._blank_row() for _ in range(self.rows)]
            self.cursor_row = 0
            self.cursor_col = 0
        elif mode == 0:
            for c in range(self.cursor_col, self.cols):
                self.cells[self.cursor_row][c] = self._blank_cell()
            for r in range(self.cursor_row + 1, self.rows):
                for c in range(self.cols):
                    self.cells[r][c] = self._blank_cell()
        elif mode == 1:
            for r in range(0, self.cursor_row):
                for c in range(self.cols):
                    self.cells[r][c] = self._blank_cell()
            for c in range(0, self.cursor_col + 1):
                self.cells[self.cursor_row][c] = self._blank_cell()

    def erase_line(self, mode: int = 0) -> None:
        if mode == 2:
            for c in range(self.cols):
                self.cells[self.cursor_row][c] = self._blank_cell()
        elif mode == 0:
            for c in range(self.cursor_col, self.cols):
                self.cells[self.cursor_row][c] = self._blank_cell()
        elif mode == 1:
            for c in range(0, self.cursor_col + 1):
                self.cells[self.cursor_row][c] = self._blank_cell()

    def apply_sgr(self, params: List[int]) -> None:
        if not params:
            params = [0]

        for param in params:
            if param == 0:
                self.current_fg = None
                self.current_bg = None
                self.current_bold = False
            elif param == 1:
                self.current_bold = True
            elif param == 22:
                self.current_bold = False
            elif 30 <= param <= 37:
                self.current_fg = param - 30
            elif param == 39:
                self.current_fg = None
            elif 40 <= param <= 47:
                self.current_bg = param - 40
            elif param == 49:
                self.current_bg = None
            elif 90 <= param <= 97:
                self.current_fg = param - 90
                self.current_bold = True
            elif 100 <= param <= 107:
                self.current_bg = param - 100


class ByteSource:
    def __init__(self, initial_data: bytes = b"", live_fd: Optional[int] = None) -> None:
        self.buffer = bytearray(initial_data)
        self.live_fd = live_fd
        self.eof = live_fd is None

    def __len__(self) -> int:
        return len(self.buffer)

    def get_byte(self, pos: int) -> Optional[int]:
        if pos < len(self.buffer):
            return self.buffer[pos]
        return None

    def get_slice(self, start: int, end: int) -> bytes:
        return bytes(self.buffer[start:end])

    def is_complete(self, pos: int) -> bool:
        return pos < len(self.buffer)

    def append_live_data(self) -> int:
        if self.live_fd is None or self.eof:
            return 0

        added = 0
        while True:
            readable, _, _ = select.select([self.live_fd], [], [], 0)
            if not readable:
                break

            try:
                chunk = os.read(self.live_fd, 4096)
            except BlockingIOError:
                break

            if not chunk:
                self.eof = True
                break

            self.buffer.extend(chunk)
            added += len(chunk)

            if len(chunk) < 4096:
                break

        return added


class VT100Parser:
    def __init__(self, screen: ScreenBuffer, source: ByteSource) -> None:
        self.screen = screen
        self.source = source
        self.pos = 0
        self.events: List[Event] = []

    def reset(self) -> None:
        self.pos = 0
        self.events.clear()
        self.screen.reset()

    def done(self) -> bool:
        return self.pos >= len(self.source) and self.source.eof

    def has_buffered_data(self) -> bool:
        return self.pos < len(self.source)

    def step_byte(self) -> Optional[Event]:
        b = self.source.get_byte(self.pos)
        if b is None:
            return None
        start = self.pos
        self.pos += 1
        ev = Event(start, bytes([b]), "BYTE", f"byte 0x{b:02X}")
        self.events.append(ev)
        return ev

    def step_token(self) -> Optional[Event]:
        b = self.source.get_byte(self.pos)
        if b is None:
            return None

        start = self.pos

        ch = decode_display_byte(b)
        if ch is not None:
            self.screen.put_char(ch, raw_byte=b)
            self.pos += 1
            ev = Event(start, bytes([b]), "PRINT", f"print {safe_repr_char(ch)}")
            self.events.append(ev)
            return ev

        if b == 0x0D:
            self.screen.carriage_return()
            self.pos += 1
            ev = Event(start, b"\r", "CTRL", r"CR (\r)")
            self.events.append(ev)
            return ev

        if b == 0x0A:
            self.screen.line_feed()
            self.pos += 1
            ev = Event(start, b"\n", "CTRL", r"LF (\n)")
            self.events.append(ev)
            return ev

        if b == 0x08:
            self.screen.backspace()
            self.pos += 1
            ev = Event(start, b"\b", "CTRL", r"BS (\b)")
            self.events.append(ev)
            return ev

        if b == 0x09:
            self.screen.tab()
            self.pos += 1
            ev = Event(start, b"\t", "CTRL", r"TAB (\t)")
            self.events.append(ev)
            return ev

        if b == ESC:
            ev = self._parse_escape_sequence()
            if ev is None:
                return None
            self.events.append(ev)
            return ev

        self.pos += 1
        ev = Event(start, bytes([b]), "BYTE", f"raw 0x{b:02X}")
        self.events.append(ev)
        return ev

    def _parse_escape_sequence(self) -> Optional[Event]:
        start = self.pos
        if self.source.get_byte(self.pos) is None:
            return None
        self.pos += 1

        next_b = self.source.get_byte(self.pos)
        if next_b is None:
            self.pos = start
            return None

        if next_b == ord("["):
            self.pos += 1
            return self._parse_csi(start)

        if next_b == ord("7"):
            self.pos += 1
            return Event(start, self.source.get_slice(start, self.pos), "ESC", "DECSC save cursor (not applied)")
        if next_b == ord("8"):
            self.pos += 1
            return Event(start, self.source.get_slice(start, self.pos), "ESC", "DECRC restore cursor (not applied)")

        self.pos += 1
        return Event(start, self.source.get_slice(start, self.pos), "ESC", f"unhandled ESC {ascii_preview(self.source.get_slice(start, self.pos))}")

    def _parse_csi(self, start: int) -> Optional[Event]:
        param_bytes = bytearray()

        while True:
            b = self.source.get_byte(self.pos)
            if b is None:
                self.pos = start
                return None

            if 0x40 <= b <= 0x7E:
                final = chr(b)
                self.pos += 1
                params = self._parse_params(bytes(param_bytes))
                desc = self._apply_csi(final, params)
                return Event(start, self.source.get_slice(start, self.pos), "CSI", desc)

            param_bytes.append(b)
            self.pos += 1

    @staticmethod
    def _parse_params(param_bytes: bytes) -> List[int]:
        if not param_bytes:
            return []
        text = param_bytes.decode("ascii", errors="ignore")
        if not text:
            return []
        parts = text.split(";")
        out: List[int] = []
        for p in parts:
            if p == "":
                out.append(0)
            else:
                try:
                    out.append(int(p))
                except ValueError:
                    out.append(0)
        return out

    def _apply_csi(self, final: str, params: List[int]) -> str:
        if final == "A":
            n = params[0] if params else 1
            n = n or 1
            self.screen.cursor_up(n)
            return f"CUU cursor up {n}"

        if final == "B":
            n = params[0] if params else 1
            n = n or 1
            self.screen.cursor_down(n)
            return f"CUD cursor down {n}"

        if final == "C":
            n = params[0] if params else 1
            n = n or 1
            self.screen.cursor_right(n)
            return f"CUF cursor right {n}"

        if final == "D":
            n = params[0] if params else 1
            n = n or 1
            self.screen.cursor_left(n)
            return f"CUB cursor left {n}"

        if final in ("H", "f"):
            row = params[0] if len(params) >= 1 and params[0] != 0 else 1
            col = params[1] if len(params) >= 2 and params[1] != 0 else 1
            self.screen.cursor_position(row, col)
            return f"CUP cursor position row={row} col={col}"

        if final == "J":
            mode = params[0] if params else 0
            self.screen.erase_display(mode)
            return f"ED erase display mode={mode}"

        if final == "K":
            mode = params[0] if params else 0
            self.screen.erase_line(mode)
            return f"EL erase line mode={mode}"

        if final == "m":
            self.screen.apply_sgr(params)
            return f"SGR graphic rendition params={params or [0]}"

        return f"unhandled CSI final={final!r} params={params}"


def decode_display_byte(b: int) -> Optional[str]:
    if b in (ESC, 0x08, 0x09, 0x0A, 0x0D):
        return None
    if b < 0x20:
        return None
    return bytes([b]).decode("cp437")


def safe_repr_char(ch: str) -> str:
    if ch in string.printable and ch not in "\t\r\n\x0b\x0c":
        return repr(ch)
    return f"0x{ord(ch):02X}"


def format_raw(raw: bytes) -> str:
    return " ".join(f"{b:02X}" for b in raw)


def ascii_preview(raw: bytes) -> str:
    out = []
    for b in raw:
        if 32 <= b <= 126:
            out.append(chr(b))
        elif b == 0x1B:
            out.append("<ESC>")
        elif b == 0x0D:
            out.append("<CR>")
        elif b == 0x0A:
            out.append("<LF>")
        elif b == 0x09:
            out.append("<TAB>")
        else:
            out.append(".")
    return "".join(out)


def demo_stream() -> bytes:
    return (
        b"Welcome to demo mode"
        b"\r\n"
        b"0123456789"
        b"\r\n"
        b"\x1b[2A"
        b"\x1b[6C"
        b"^"
        b"\r\n"
        b"\x1b[2B"
        b"\x1b[2J"
        b"After clear"
        b"\r\n"
        b"Move right: "
        b"\x1b[C"
        b"X"
        b"\r\n"
        b"Move right 5: "
        b"\x1b[5C"
        b"Y"
        b"\r\n"
        b"\x1b[10;10HAt row 10 col 10"
        b"\r\n"
        b"\x1b[31mred?\x1b[0m"
        b"\r\n"
    )


def parse_hex_string(text: str) -> bytes:
    cleaned = text.replace(",", " ").replace("0x", "").replace("0X", "")
    parts = [p for p in cleaned.split() if p]
    try:
        return bytes(int(p, 16) for p in parts)
    except ValueError as exc:
        raise SystemExit(f"Invalid hex input: {exc}") from exc


def set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def head_lines(data: bytes, line_count: int) -> bytes:
    if line_count <= 0:
        return b""

    seen = 0
    for index, byte in enumerate(data):
        if byte == 0x0A:
            seen += 1
            if seen >= line_count:
                return data[: index + 1]
    return data


class ColorPalette:
    def __init__(self) -> None:
        self.enabled = False
        self.pairs: dict[tuple[Optional[int], Optional[int]], int] = {}
        self.next_pair = 1

    def initialize(self) -> None:
        if not curses.has_colors():
            return

        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        self.enabled = True

    def attr_for(self, cell: Cell) -> int:
        attr = curses.A_BOLD if cell.bold else curses.A_NORMAL
        if not self.enabled:
            return attr

        key = (cell.fg, cell.bg)
        pair_id = self.pairs.get(key)
        if pair_id is None:
            fg = -1 if cell.fg is None else cell.fg
            bg = -1 if cell.bg is None else cell.bg
            pair_id = self.next_pair
            try:
                curses.init_pair(pair_id, fg, bg)
            except curses.error:
                return attr
            self.pairs[key] = pair_id
            self.next_pair += 1

        return attr | curses.color_pair(pair_id)


def draw_border(win: curses.window) -> None:
    h, w = win.getmaxyx()
    if h < 2 or w < 2:
        return

    try:
        win.addch(0, 0, "+")
        win.addch(0, w - 1, "+")
        win.addch(h - 1, 0, "+")
        win.addch(h - 1, w - 1, "+")
        for x in range(1, w - 1):
            win.addch(0, x, "-")
            win.addch(h - 1, x, "-")
        for y in range(1, h - 1):
            win.addch(y, 0, "|")
            win.addch(y, w - 1, "|")
    except curses.error:
        pass


def add_text(win: curses.window, y: int, x: int, text: str, limit: int, attr: int = 0) -> None:
    if limit <= 0:
        return

    clipped = text[:limit]
    if len(clipped) == 1:
        try:
            win.addstr(y, x, clipped, attr)
        except curses.error:
            pass
        return

    try:
        win.addnstr(y, x, clipped, limit, attr)
    except curses.error:
        pass


ANSI_FG_MAP = {0: '30', 1: '31', 2: '32', 3: '33', 4: '34', 5: '35', 6: '36', 7: '37'}
ANSI_BG_MAP = {0: '40', 1: '41', 2: '42', 3: '43', 4: '44', 5: '45', 6: '46', 7: '47'}


def draw_screen_cells_direct(fd: int, screen: ScreenBuffer, top_row: int, left_col: int,
                             visible_rows: int, visible_cols: int) -> None:
    """Write screen cells directly to terminal as raw bytes, bypassing curses."""
    buf = bytearray()
    prev_sgr: Optional[str] = None
    for r in range(visible_rows):
        buf.extend(f"\033[{top_row + r};{left_col}H".encode('ascii'))
        prev_sgr = None
        for c in range(visible_cols):
            cell = screen.cells[r][c]
            is_cursor = (r == screen.cursor_row and c == screen.cursor_col)
            codes: List[str] = []
            if cell.bold:
                codes.append('1')
            if is_cursor:
                codes.append('7')
            if cell.fg is not None:
                codes.append(ANSI_FG_MAP.get(cell.fg, '37'))
            if cell.bg is not None:
                codes.append(ANSI_BG_MAP.get(cell.bg, '40'))
            sgr = ';'.join(codes) if codes else '0'
            if sgr != prev_sgr:
                buf.extend(f"\033[{sgr}m".encode('ascii'))
                prev_sgr = sgr
            buf.append(cell.raw_byte if cell.raw_byte is not None else 0x20)
    buf.extend(b'\033[0m')
    os.write(fd, bytes(buf))


def draw_screen(win: curses.window, screen: ScreenBuffer, term_fd: int) -> None:
    h, w = win.getmaxyx()
    win.erase()
    draw_border(win)
    title = f" Screen  cursor=({screen.cursor_row + 1},{screen.cursor_col + 1}) "
    if w > len(title) + 4:
        add_text(win, 0, 2, title, w - 4)

    win.noutrefresh()


def draw_log(
    win: curses.window,
    events: List[Event],
    parser_pos: int,
    buffered_len: int,
    live_mode: bool,
    eof: bool,
    running: bool,
    selected: int,
    show_help: bool,
) -> None:
    h, w = win.getmaxyx()
    win.erase()
    draw_border(win)

    source_state = "LIVE" if live_mode else "STATIC"
    if live_mode and eof:
        source_state = "LIVE-EOF"
    status = f" Log pos={parser_pos}/{buffered_len} source={source_state} mode={'RUN' if running else 'PAUSE'} events={len(events)} "
    if w > len(status) + 4:
        add_text(win, 0, 2, status, w - 4)

    body_h = max(1, h - 4)
    if events:
        selected = max(0, min(selected, len(events) - 1))
    else:
        selected = 0

    start = max(0, selected - body_h + 1)
    visible = events[start:start + body_h]

    for i, ev in enumerate(visible):
        idx = start + i
        line = f"{idx:05d} @{ev.offset:06d} {ev.kind:<5} {format_raw(ev.raw):<20} {ev.description}"
        attr = curses.A_REVERSE if idx == selected else curses.A_NORMAL
        add_text(win, 1 + i, 1, line, w - 2, attr)

    if events:
        ev = events[selected]
        detail = f"selected raw=[{format_raw(ev.raw)}] ascii=[{ascii_preview(ev.raw)}]"
    else:
        detail = "selected raw=[]"
    add_text(win, h - 2, 1, detail, w - 2)

    if show_help and h >= 5:
        help_text = "q quit  s token  n byte  g run  p pause  r reset  j/k select  G end  h help"
        add_text(win, h - 3, 1, help_text, w - 2)

    win.noutrefresh()


def run_ui(stdscr: curses.window, source: ByteSource, live_mode: bool) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.meta(1)
    except curses.error:
        pass
    stdscr.nodelay(True)
    stdscr.timeout(40)

    term_fd = sys.stdout.fileno()

    max_y, max_x = stdscr.getmaxyx()
    top_h = max(6, int(max_y * 0.55))
    bottom_h = max_y - top_h

    screen_rows = max(3, top_h - 2)
    screen_cols = max(10, max_x - 2)

    screen = ScreenBuffer(rows=screen_rows, cols=screen_cols)
    parser = VT100Parser(screen, source)

    top = stdscr.derwin(top_h, max_x, 0, 0)
    bottom = stdscr.derwin(bottom_h, max_x, top_h, 0)

    running = True if live_mode else False
    selected = 0
    show_help = True

    while True:
        if live_mode:
            source.append_live_data()

        ch = stdscr.getch()

        if ch == ord("q"):
            break
        elif ch == ord("n"):
            parser.step_byte()
            running = False
        elif ch == ord("s"):
            ev = parser.step_token()
            running = False
            if ev and parser.events:
                selected = len(parser.events) - 1
        elif ch == ord("g"):
            running = True
        elif ch == ord("p"):
            running = False
        elif ch == ord("r"):
            parser.reset()
            running = False if not live_mode else True
            selected = 0
        elif ch == ord("j"):
            if parser.events:
                selected = min(len(parser.events) - 1, selected + 1)
        elif ch == ord("k"):
            if parser.events:
                selected = max(0, selected - 1)
        elif ch == ord("G"):
            while parser.has_buffered_data():
                if parser.step_token() is None:
                    break
            running = False if not live_mode else True
            if parser.events:
                selected = len(parser.events) - 1
        elif ch == ord("h"):
            show_help = not show_help

        if running:
            for _ in range(100):
                ev = parser.step_token()
                if ev is None:
                    break
            if parser.events:
                selected = len(parser.events) - 1

        draw_screen(top, screen, term_fd)
        draw_log(
            bottom,
            parser.events,
            parser.pos,
            len(source),
            live_mode,
            source.eof,
            running,
            selected,
            show_help,
        )
        curses.doupdate()

        visible_rows = min(screen.rows, top_h - 2)
        visible_cols = min(screen.cols, max_x - 2)
        draw_screen_cells_direct(term_fd, screen, 2, 2, visible_rows, visible_cols)

        time.sleep(0.01)


def load_source(args: argparse.Namespace) -> tuple[ByteSource, bool]:
    if args.stdin:
        if sys.stdin.isatty():
            raise SystemExit("--stdin expects piped input on stdin")
        fd = sys.stdin.fileno()
        set_nonblocking(fd)
        return ByteSource(initial_data=b"", live_fd=fd), True

    if args.hex is not None:
        return ByteSource(initial_data=parse_hex_string(args.hex)), False

    if args.path is not None:
        with open(args.path, "rb") as f:
            data = f.read()
            if args.head_lines is not None:
                data = head_lines(data, args.head_lines)
            return ByteSource(initial_data=data), False

    data = demo_stream()
    if args.head_lines is not None:
        data = head_lines(data, args.head_lines)
    return ByteSource(initial_data=data), False


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Textual ANSI/VT100 byte stream viewer")
    p.add_argument("path", nargs="?", help="optional binary capture file")
    p.add_argument("--hex", help="hex bytes, e.g. '1B 5B 35 43'")
    p.add_argument("--stdin", action="store_true", help="read live byte stream from stdin")
    p.add_argument("--head-lines", type=int, help="truncate input after N newline-delimited lines before parsing")
    return p


def main() -> int:
    locale.setlocale(locale.LC_ALL, "")
    args = build_arg_parser().parse_args()
    source, live_mode = load_source(args)
    curses.wrapper(run_ui, source, live_mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
