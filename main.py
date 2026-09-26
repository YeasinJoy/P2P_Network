"""
=============================================================================
main.py  —  Graphical User Interface (Tkinter)
=============================================================================

PURPOSE
-------
This file is the entry point of the application.  It builds the graphical
window using Python's built-in Tkinter library and wires every button,
input field, and list widget to the P2PNode networking layer defined in
p2p_node.py.

SEPARATION OF CONCERNS
-----------------------
main.py  ←→  p2p_node.py  ←→  protocol.py

  • main.py        : knows about the UI — windows, buttons, labels, colours.
                     It does NOT know how TCP sockets work.
  • p2p_node.py    : knows about networking — sockets, threads, handshakes.
                     It does NOT know about Tkinter.
  • protocol.py    : knows about message format — JSON encoding, framing.
                     It does NOT know about the UI or business logic.

THREAD SAFETY — VERY IMPORTANT
-------------------------------
Tkinter is NOT thread-safe.  Only the main thread is allowed to call
Tkinter functions.  But p2p_node.py runs many background threads that need
to update the UI (e.g. add a peer to the list, append a log line).

Solution: every callback from P2PNode uses self.after(0, function, args)
          which schedules the function to run on the main thread at the
          next opportunity — safely, without blocking the networking thread.

LAYOUT (ASCII art)
------------------
┌──────────────────────────────────────────────────────────────────────┐
│  My Peer    Name:[Alice]  Port:[5000]  [▶ Start Peer]  [■ Stop]      │
├──────────────────────────────────────────────────────────────────────┤
│  Connect    IP:[127.0.0.1]  Port:[5001]  [⇌ Connect]                 │
├─────────────────────────┬────────────────────────────────────────────┤
│  Connected Peers        │  Messages / Events                         │
│  ┌───────────────────┐  │  ┌──────────────────────────────────────┐  │
│  │ Alice [a83f21c4]  │  │  │ [INFO] Peer 'Alice' started …        │  │
│  │ Bob   [92bd71e3]  │  │  │ Bob -> You: Hello!                   │  │
│  └───────────────────┘  │  │ [FILE] photo.jpg saved to downloads/ │  │
│                         │  └──────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────────────────┤
│  Send Text: [__________________________________]  [Send ➤]            │
├──────────────────────────────────────────────────────────────────────┤
│  [📁 Choose File & Send]                                             │
└──────────────────────────────────────────────────────────────────────┘

Dependencies: tkinter (standard library), p2p_node, protocol
=============================================================================
"""

import os          # used for path operations when opening files
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

from p2p_node import P2PNode   # our networking layer


# =============================================================================
# SECTION 1 – COLOUR PALETTE  (dark "Catppuccin Mocha"-inspired theme)
# =============================================================================
# Keeping colours as named constants makes it easy to restyle the whole app
# by changing values here rather than hunting through the widget code.

BG         = "#1e1e2e"   # main background (very dark blue-grey)
BG_PANEL   = "#2a2a3e"   # panel / bar background (slightly lighter)
BG_INPUT   = "#313145"   # text entry / listbox background
FG         = "#cdd6f4"   # primary foreground text (off-white)
FG_MUTED   = "#6c7086"   # muted text (status bar)
ACCENT     = "#89b4fa"   # blue — used for info messages and buttons
ACCENT2    = "#a6e3a1"   # green — used for "Start" button and file messages
WARN       = "#f38ba8"   # red/pink — used for errors and the "Stop" button
BORDER     = "#45475a"   # separator lines
FONT_MONO  = ("Consolas", 10)    # monospaced font for the event log
FONT_UI    = ("Segoe UI", 10)    # proportional font for labels and inputs
FONT_TITLE = ("Segoe UI", 10, "bold")  # bold labels for section headers


# =============================================================================
# SECTION 2 – WIDGET FACTORY HELPERS
# =============================================================================
# Small functions that create consistently styled widgets.
# Using helpers avoids repeating the same styling kwargs everywhere.

def _entry(parent, width=18, **kw) -> tk.Entry:
    """Create a dark-themed single-line text entry widget."""
    return tk.Entry(
        parent,
        width=width,
        bg=BG_INPUT,           # dark background inside the field
        fg=FG,                 # light text
        insertbackground=FG,   # cursor colour (matches text so it is visible)
        relief="flat",         # no 3-D border
        font=FONT_UI,
        **kw,
    )


def _button(parent, text, command, color=ACCENT, **kw) -> tk.Button:
    """Create a flat, coloured button."""
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=color,
        fg="#11111b",            # very dark text on the coloured background
        activebackground=color,  # keep colour when mouse is pressed
        activeforeground="#11111b",
        relief="flat",
        font=FONT_TITLE,
        padx=8,
        pady=4,
        cursor="hand2",          # pointer cursor on hover (UX touch)
        **kw,
    )


def _label(parent, text, font=FONT_UI, **kw) -> tk.Label:
    """
    Create a plain dark-themed label.

    'font' is an explicit default parameter so callers can override it
    (e.g. font=FONT_TITLE) without triggering a "multiple values"
    TypeError from **kw.
    """
    return tk.Label(
        parent,
        text=text,
        bg=BG_PANEL,   # must match the parent panel background
        fg=FG,
        font=font,     # uses the passed-in value or FONT_UI by default
        **kw,
    )


# =============================================================================
# SECTION 3 – MAIN APPLICATION WINDOW
# =============================================================================

class P2PApp(tk.Tk):
    """
    The root Tkinter window and the glue that connects the UI to P2PNode.

    Inherits from tk.Tk so this object IS the Tkinter root window.
    Calling app.mainloop() starts the event loop that processes user
    interactions and schedules callbacks from background threads.
    """

    def __init__(self):
        super().__init__()

        # --- Window configuration ---
        self.title("P2P Network — UAP CSE 433")
        self.configure(bg=BG)
        self.resizable(True, True)   # allow the user to resize the window
        self.minsize(820, 560)       # but not smaller than this

        # --- Application state ---
        # _node holds the running P2PNode instance, or None when stopped.
        self._node: P2PNode | None = None

        # _peer_map maps peer_id → peer_name for quick name lookups.
        self._peer_map: dict[str, str] = {}

        # _peer_id_order is a list that mirrors the order of items in
        # the peer listbox so we can convert a listbox index to a peer_id.
        self._peer_id_order: list[str] = []

        # --- Build the UI ---
        self._build_ui()

        # Handle the window close button (X) gracefully
        self.protocol("WM_DELETE_WINDOW", self._on_close)


    # =========================================================================
    # UI CONSTRUCTION
    # =========================================================================

    def _build_ui(self) -> None:
        """
        Create and arrange all widgets.

        Tkinter uses a geometry manager to lay out widgets.  We use .pack()
        which stacks widgets top-to-bottom (or left-to-right with side=).

        'padx' and 'pady' add spacing around each widget.
        'fill="x"' makes a widget stretch horizontally to fill its container.
        'expand=True' lets a widget grow to fill remaining space.
        """
        pad = {"padx": 8, "pady": 6}   # shorthand used repeatedly below

        # ── ROW 0: "My Peer" — name, port, start, stop ───────────────────────
        # This is a horizontal bar at the top of the window.
        row0 = tk.Frame(self, bg=BG_PANEL, relief="flat")
        row0.pack(fill="x", padx=10, pady=(10, 2))

        _label(row0, "My Peer", font=FONT_TITLE).pack(side="left", **pad)

        # Name input: the human-readable nickname for this peer
        _label(row0, "Name:").pack(side="left", padx=(12, 2))
        self._name_var = tk.StringVar(value="Alice")  # default value
        _entry(row0, width=12, textvariable=self._name_var).pack(side="left", **pad)

        # Port input: which TCP port this peer will listen on
        _label(row0, "Port:").pack(side="left", padx=(12, 2))
        self._port_var = tk.StringVar(value="5000")
        _entry(row0, width=7, textvariable=self._port_var).pack(side="left", **pad)

        # Start button — green to signal "go"
        self._start_btn = _button(row0, "▶  Start Peer", self._start_peer,
                                  color=ACCENT2)
        self._start_btn.pack(side="left", padx=(12, 4))

        # Stop button — red, initially disabled (no peer running yet)
        self._stop_btn = _button(row0, "■  Stop", self._stop_peer, color=WARN)
        self._stop_btn.pack(side="left", padx=4)
        self._stop_btn.config(state="disabled")

        # ── ROW 1: "Connect to Peer" — IP, port, connect button ──────────────
        row1 = tk.Frame(self, bg=BG_PANEL, relief="flat")
        row1.pack(fill="x", padx=10, pady=2)

        _label(row1, "Connect to Peer", font=FONT_TITLE).pack(side="left", **pad)

        # Remote IP address input
        _label(row1, "IP:").pack(side="left", padx=(12, 2))
        self._ip_var = tk.StringVar(value="127.0.0.1")  # loopback for local testing
        _entry(row1, width=16, textvariable=self._ip_var).pack(side="left", **pad)

        # Remote listening port input
        _label(row1, "Port:").pack(side="left", padx=(12, 2))
        self._rport_var = tk.StringVar(value="5001")
        _entry(row1, width=7, textvariable=self._rport_var).pack(side="left", **pad)

        # Connect button — disabled until we have a running peer
        self._connect_btn = _button(row1, "⇌  Connect", self._connect_peer)
        self._connect_btn.pack(side="left", padx=(12, 4))
        self._connect_btn.config(state="disabled")

        # ── ROW 2: Peer list (left) + Event log (right) ───────────────────────
        # A horizontal container frame splits this row into two panels.
        mid = tk.Frame(self, bg=BG)
        mid.pack(fill="both", expand=True, padx=10, pady=4)

        # --- Left panel: Connected Peers listbox ---
        left = tk.Frame(mid, bg=BG_PANEL, width=200)
        left.pack(side="left", fill="y", padx=(0, 4))
        left.pack_propagate(False)   # keep fixed width even if content is narrow

        _label(left, "Connected Peers", font=FONT_TITLE).pack(
            anchor="w", padx=8, pady=(6, 2)
        )
        # A thin horizontal separator line for visual polish
        tk.Frame(left, bg=BORDER, height=1).pack(fill="x", padx=4)

        # The listbox shows one entry per connected peer.
        # Clicking a peer selects it as the target for text/file sends.
        self._peer_listbox = tk.Listbox(
            left,
            bg=BG_INPUT,
            fg=FG,
            selectbackground=ACCENT,    # highlight colour for selected item
            selectforeground="#11111b",
            font=FONT_MONO,
            relief="flat",
            highlightthickness=0,
            borderwidth=0,
            activestyle="none",         # no underline on the active (hovered) item
        )
        self._peer_listbox.pack(fill="both", expand=True, padx=4, pady=4)

        # --- Right panel: Event / message log ---
        right = tk.Frame(mid, bg=BG_PANEL)
        right.pack(side="left", fill="both", expand=True)

        _label(right, "Messages / Events", font=FONT_TITLE).pack(
            anchor="w", padx=8, pady=(6, 2)
        )
        tk.Frame(right, bg=BORDER, height=1).pack(fill="x", padx=4)

        # ScrolledText is a Text widget with an automatic vertical scrollbar.
        # state="disabled" prevents the user from typing into the log.
        self._log = scrolledtext.ScrolledText(
            right,
            bg=BG_INPUT,
            fg=FG,
            font=FONT_MONO,
            state="disabled",     # read-only
            relief="flat",
            highlightthickness=0,
            borderwidth=0,
            wrap="word",          # wrap long lines at word boundaries
        )
        self._log.pack(fill="both", expand=True, padx=4, pady=4)

        # Named text tags let us colour different types of log entries.
        # Tkinter text widgets support rich formatting via tags.
        self._log.tag_config("info",  foreground=ACCENT)   # blue  → [INFO] lines
        self._log.tag_config("msg",   foreground=FG)       # white → chat messages
        self._log.tag_config("file",  foreground=ACCENT2)  # green → file events
        self._log.tag_config("error", foreground=WARN)     # red   → [ERROR] lines
        self._log.tag_config("warn",  foreground="#fab387") # orange→ [WARN] lines

        # ── ROW 3: Send Text ──────────────────────────────────────────────────
        row3 = tk.Frame(self, bg=BG_PANEL)
        row3.pack(fill="x", padx=10, pady=2)

        _label(row3, "Send Text:").pack(side="left", padx=(8, 4))

        # The text input field stretches to fill available horizontal space
        self._text_var = tk.StringVar()
        self._text_entry = _entry(row3, width=55, textvariable=self._text_var)
        self._text_entry.pack(side="left", padx=4, pady=6, fill="x", expand=True)

        # Pressing Enter in the text field also triggers send (convenience)
        self._text_entry.bind("<Return>", lambda e: self._send_text())

        self._send_btn = _button(row3, "Send ➤", self._send_text)
        self._send_btn.pack(side="left", padx=(4, 8))
        self._send_btn.config(state="disabled")  # enabled after peer starts

        # ── ROW 4: File Send ──────────────────────────────────────────────────
        row4 = tk.Frame(self, bg=BG_PANEL)
        row4.pack(fill="x", padx=10, pady=(2, 10))

        # Purple colour distinguishes file action from text/control buttons
        self._file_btn = _button(
            row4, "📁  Choose File & Send",
            self._send_file, color="#cba6f7"
        )
        self._file_btn.pack(side="left", padx=8, pady=6)
        self._file_btn.config(state="disabled")

        # ── STATUS BAR ────────────────────────────────────────────────────────
        # A thin bar at the very bottom showing the current peer state.
        self._status_var = tk.StringVar(value="Peer not started.")
        tk.Label(
            self,
            textvariable=self._status_var,
            bg=BG_PANEL,
            fg=FG_MUTED,
            font=("Segoe UI", 9),
            anchor="w",        # align text to the left
        ).pack(fill="x", padx=0, pady=0)


    # =========================================================================
    # EVENT HANDLERS  (called by Tkinter when the user interacts with widgets)
    # =========================================================================

    def _start_peer(self) -> None:
        """
        Called when the user clicks "▶ Start Peer".

        Validates the inputs, creates a P2PNode, sets up callbacks,
        and starts the node's TCP server.
        """
        # Read and validate the peer name
        name = self._name_var.get().strip()
        if not name:
            messagebox.showerror("Error", "Please enter a peer name.")
            return

        # Read and validate the port number
        port_str = self._port_var.get().strip()
        try:
            port = int(port_str)
            if not (1024 <= port <= 65535):
                # Ports below 1024 are reserved by the OS and need admin rights
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Error", "Port must be a number between 1024 and 65535."
            )
            return

        # Create the P2PNode and wire up the callbacks
        try:
            self._node = P2PNode(peer_name=name, listen_port=port)

            # These callbacks will be called from background threads, so they
            # must schedule their UI work via self.after() (see below).
            self._node.on_peer_connected    = self._cb_peer_connected
            self._node.on_peer_disconnected = self._cb_peer_disconnected
            self._node.on_text_received     = self._cb_text_received
            self._node.on_file_received     = self._cb_file_received
            self._node.on_log               = self._cb_log

            # This opens the server socket and starts the accept loop thread
            self._node.start()

        except OSError as exc:
            # For example: "Address already in use" if the port is taken
            messagebox.showerror("Error", f"Failed to start peer:\n{exc}")
            self._node = None
            return

        # Update button states: disable Start, enable Stop/Connect/Send
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._connect_btn.config(state="normal")
        self._send_btn.config(state="normal")
        self._file_btn.config(state="normal")

        # Show the peer ID in the status bar for reference
        self._status_var.set(
            f"Running as '{name}' (id={self._node.peer_id[:8]}) on port {port}"
        )

    def _stop_peer(self) -> None:
        """
        Called when the user clicks "■ Stop".

        Stops the P2PNode (sends DISCONNECT to all peers, closes sockets)
        and resets the UI back to its initial state.
        """
        if self._node:
            self._node.stop()
            self._node = None

        # Clear the peer list and the internal tracking structures
        self._peer_listbox.delete(0, "end")
        self._peer_map.clear()
        self._peer_id_order.clear()

        # Re-enable Start, disable everything else
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._connect_btn.config(state="disabled")
        self._send_btn.config(state="disabled")
        self._file_btn.config(state="disabled")
        self._status_var.set("Peer stopped.")

    def _connect_peer(self) -> None:
        """
        Called when the user clicks "⇌ Connect".

        Reads the remote IP and port fields, validates them, and tells
        the P2PNode to initiate an outgoing TCP connection.

        The actual connection runs in a background thread inside P2PNode
        so the UI remains responsive.
        """
        if self._node is None:
            messagebox.showerror("Error", "Start your peer first.")
            return

        ip = self._ip_var.get().strip()
        if not ip:
            messagebox.showerror("Error", "Please enter a remote IP address.")
            return

        port_str = self._rport_var.get().strip()
        try:
            port = int(port_str)
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Error", "Remote port must be a number between 1 and 65535."
            )
            return

        # Delegate the connection to P2PNode (runs in a background thread)
        self._node.connect_to_peer(ip, port)

    def _send_text(self) -> None:
        """
        Called when the user clicks "Send ➤" or presses Enter.

        Reads the text field, validates that a peer is selected, and
        tells the P2PNode to send a text message to that peer.
        """
        if self._node is None:
            return

        text = self._text_var.get().strip()
        if not text:
            return  # don't send empty messages

        # Find out which peer the user has selected in the listbox
        peer_id = self._selected_peer_id()
        if peer_id is None:
            messagebox.showwarning(
                "No Peer Selected",
                "Select a peer from the list before sending."
            )
            return

        # Tell the networking layer to send the message
        self._node.send_text(peer_id, text)

        # Show the sent message in the local log immediately (don't wait
        # for a network echo — TCP is reliable so we know it will arrive)
        peer_name = self._peer_map.get(peer_id, "?")
        self._append_log(f"You → {peer_name}: {text}", tag="msg")

        # Clear the input field ready for the next message
        self._text_var.set("")

    def _send_file(self) -> None:
        """
        Called when the user clicks "📁 Choose File & Send".

        Opens a system file-picker dialog, lets the user browse and
        select any file, then tells the P2PNode to transfer it.
        """
        if self._node is None:
            return

        peer_id = self._selected_peer_id()
        if peer_id is None:
            messagebox.showwarning(
                "No Peer Selected",
                "Select a peer from the list before sending a file."
            )
            return

        # Open the native OS file picker dialog.
        # Returns an empty string if the user cancelled.
        file_path = filedialog.askopenfilename(
            title="Select file to send",
            filetypes=[("All files", "*.*")],  # allow any file type
        )
        if not file_path:
            return  # user cancelled the dialog

        # Delegate the file transfer to P2PNode (runs in a background thread)
        self._node.send_file(peer_id, file_path)

    def _on_close(self) -> None:
        """
        Called when the user clicks the window's X button.

        Makes sure the P2PNode is shut down cleanly before the window closes
        so that all peer sockets are closed and DISCONNECT messages are sent.
        """
        if self._node:
            self._node.stop()
        self.destroy()   # close the Tkinter window


    # =========================================================================
    # P2PNODE CALLBACKS
    # (called from BACKGROUND THREADS — must use self.after() for all UI work)
    # =========================================================================

    def _cb_peer_connected(self, info: dict) -> None:
        """
        Fired by P2PNode when a new peer completes the HELLO handshake.

        'info' contains: peer_id, peer_name, address, listen_port.

        We store the peer in our local maps and then schedule the UI update
        on the main thread via self.after(0, ...).
        """
        peer_id   = info["peer_id"]
        peer_name = info["peer_name"]
        address   = info["address"]
        lport     = info["listen_port"]

        # Build the display string shown in the listbox
        display = f"{peer_name} [{peer_id[:8]}]  {address}:{lport}"

        # Store name for quick lookup when logging sent messages
        self._peer_map[peer_id] = peer_name

        # Schedule the listbox update on the main thread (thread-safe)
        self.after(0, self._add_peer_to_list, peer_id, display)

    def _cb_peer_disconnected(self, peer_id: str) -> None:
        """
        Fired by P2PNode when a peer's connection drops.

        Removes the peer from our local maps and schedules UI cleanup.
        """
        self._peer_map.pop(peer_id, None)
        self.after(0, self._remove_peer_from_list, peer_id)

    def _cb_text_received(self, peer_id: str, text: str) -> None:
        """
        Fired by P2PNode when a text message is received.

        P2PNode already calls on_log with the formatted "Name -> You: text"
        string, so we don't need to do anything extra here.  This callback
        exists as a hook in case the UI wants to do something more
        sophisticated (e.g. play a sound or show a notification badge).
        """
        pass  # handled by on_log → _cb_log

    def _cb_file_received(self, peer_id: str, filename: str) -> None:
        """
        Fired by P2PNode after a file has been fully saved to disk.

        Appends a success message to the event log on the main thread.
        """
        self.after(
            0,
            self._append_log,
            f"[FILE] Received: {filename} → saved to downloads/",
            "file",
        )

    def _cb_log(self, message: str) -> None:
        """
        Fired by P2PNode for every informational, warning, or error string.

        Determines the correct colour tag based on keywords in the message
        and then schedules the log append on the main thread.

        Why self.after(0, ...)?
        -----------------------
        This callback is called from background networking threads.
        Directly calling Tkinter widgets from a non-main thread causes
        random crashes.  self.after(0, func) puts func on the Tkinter
        event queue so it runs safely on the main thread.
        """
        # Choose a colour tag based on the message content
        tag = "info"                          # default: blue
        if "[ERROR]" in message:
            tag = "error"                     # red
        elif "[WARN]" in message:
            tag = "warn"                      # orange
        elif "[FILE]" in message:
            tag = "file"                      # green
        elif " -> " in message or "→" in message:
            tag = "msg"                       # white (chat messages)

        # Schedule on the main (Tkinter) thread
        self.after(0, self._append_log, message, tag)


    # =========================================================================
    # UI HELPER METHODS
    # =========================================================================

    def _append_log(self, text: str, tag: str = "info") -> None:
        """
        Append *text* to the event log with the given colour *tag*.

        Steps
        -----
        1. Temporarily enable the read-only ScrolledText widget.
        2. Insert the text with a newline.
        3. Scroll to the bottom so the latest message is always visible.
        4. Disable the widget again.
        """
        self._log.config(state="normal")           # unlock for writing
        self._log.insert("end", text + "\n", tag)  # append with colour tag
        self._log.see("end")                        # scroll to newest entry
        self._log.config(state="disabled")          # lock again (read-only)

    def _add_peer_to_list(self, peer_id: str, display: str) -> None:
        """
        Add a peer entry to the listbox and to our tracking list.

        We maintain _peer_id_order as a parallel list to the listbox items
        so that we can convert a listbox index (0, 1, 2, …) back to a
        peer_id string when the user clicks on a list item.
        """
        self._peer_listbox.insert("end", display)
        self._peer_id_order.append(peer_id)

    def _remove_peer_from_list(self, peer_id: str) -> None:
        """
        Remove the listbox entry for *peer_id* when a peer disconnects.

        We find the index of peer_id in _peer_id_order, delete the
        corresponding listbox item by index, and remove it from the order list.
        """
        try:
            idx = self._peer_id_order.index(peer_id)
            self._peer_listbox.delete(idx)    # remove by index from listbox
            self._peer_id_order.pop(idx)       # keep tracking list in sync
        except ValueError:
            pass  # peer_id wasn't in the list — nothing to do

    def _selected_peer_id(self) -> str | None:
        """
        Return the peer_id of the currently selected listbox item,
        or None if nothing is selected.

        curselection() returns a tuple of selected indices.
        We only care about the first (and only) selected item.
        """
        sel = self._peer_listbox.curselection()
        if not sel:
            return None  # no item selected

        idx = sel[0]  # index of the selected listbox entry

        if idx < len(self._peer_id_order):
            return self._peer_id_order[idx]  # look up the peer_id

        return None  # safety guard (shouldn't happen)


# =============================================================================
# SECTION 4 – ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    # This block only runs when the file is executed directly:
    #   python main.py
    # It does NOT run when this file is imported as a module.

    app = P2PApp()   # create the root window and build the UI
    app.mainloop()   # hand control to Tkinter's event loop
                     # (the program runs until the window is closed)
