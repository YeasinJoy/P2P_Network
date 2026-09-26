"""
=============================================================================
p2p_node.py  —  Core P2P Networking Layer
=============================================================================

PURPOSE
-------
This module contains the P2PNode class — the "heart" of the application.
A P2PNode is simultaneously:

  • A TCP SERVER  – It opens a listening socket and accepts connections
                    from other peers who want to talk to us.

  • A TCP CLIENT  – It can initiate connections to other peers by their
                    IP address and port number.

This is what makes it "peer-to-peer": there is no dedicated central server.
Every peer can both accept AND initiate connections.

THREADING MODEL
---------------
TCP is blocking by default: when we call sock.recv() the program waits
until data arrives.  If we only had one thread the entire application
would freeze while waiting for a message from peer A, and we would miss
messages from peer B entirely.

Solution → one thread per connection:

  Main Thread
      │
      ├─ Accept Loop Thread      (runs forever, accepts new connections)
      │       │
      │       ├─ Handler Thread  (one per accepted connection)
      │       ├─ Handler Thread
      │       └─ ...
      │
      └─ Connect Worker Thread   (one per outgoing connection attempt)

All threads are "daemon" threads, which means Python will not wait for
them to finish when the main program exits.

PEER REGISTRY
-------------
self._peers is a dictionary mapping peer_id → PeerInfo object.
A threading.Lock() (self._peers_lock) protects all reads and writes to
this dictionary so that multiple threads can safely add/remove peers
at the same time without corrupting the data structure.

PUBLIC CALLBACKS
----------------
The UI layer (main.py) sets these attributes to its own functions:

  on_peer_connected(info)        called when a handshake completes
  on_peer_disconnected(peer_id)  called when a peer's connection drops
  on_text_received(peer_id, txt) called when a text message arrives
  on_file_received(peer_id, fn)  called when a file has been saved
  on_log(message)                called for any status/error string

Using callbacks keeps the networking layer completely independent of the
UI layer: p2p_node.py does not import tkinter at all.

Dependencies: socket, threading, os, uuid  (all standard library)
             protocol  (our own module)
=============================================================================
"""

import os          # for file-system operations (makedirs, path checks)
import socket      # for TCP socket creation and communication
import threading   # for running multiple connections concurrently
import uuid        # for generating a unique peer ID at startup

import protocol    # our own message framing / encoding module


# =============================================================================
# SECTION 1 – PeerInfo  (lightweight record describing one connected peer)
# =============================================================================

class PeerInfo:
    """
    Stores all the information we need about a single connected peer.

    Every time a new connection is fully established (after the HELLO
    handshake) we create one PeerInfo object and store it in the
    P2PNode's peer registry (_peers dict).

    Attributes
    ----------
    peer_id      : unique 32-char hex string sent by the remote peer
    peer_name    : human-readable name chosen by the remote user
    address      : (ip, port) tuple of the remote TCP endpoint
    sock         : the live TCP socket for this connection
    listen_port  : the port on which the remote peer is *listening*
                   (different from the ephemeral source port of the socket)
    lock         : a per-peer mutex that serialises all send() calls on
                   this socket so that concurrent sends (e.g. text + file)
                   don't interleave their bytes
    """

    def __init__(self, peer_id: str, peer_name: str, address: tuple,
                 sock: socket.socket, listen_port: int):
        self.peer_id     = peer_id
        self.peer_name   = peer_name
        self.address     = address       # (remote_ip, remote_ephemeral_port)
        self.listen_port = listen_port   # port the remote peer is listening on
        self.sock        = sock
        # Lock ensures only one thread sends on this socket at a time
        self.lock        = threading.Lock()

    @property
    def display_name(self) -> str:
        """
        A short, readable string for log messages.
        Example: "Alice [a83f21c4]"
        We only show the first 8 characters of the 32-char peer ID.
        """
        return f"{self.peer_name} [{self.peer_id[:8]}]"


# =============================================================================
# SECTION 2 – P2PNode  (the main networking class)
# =============================================================================

class P2PNode:
    """
    The core P2P node.

    Usage
    -----
    1. Instantiate:  node = P2PNode(peer_name="Alice", listen_port=5000)
    2. Set callbacks: node.on_log = my_log_function  (etc.)
    3. Start server:  node.start()
    4. Connect out:   node.connect_to_peer("192.168.1.5", 5001)
    5. Send text:     node.send_text(peer_id, "Hello!")
    6. Send file:     node.send_file(peer_id, "/path/to/photo.jpg")
    7. Stop:          node.stop()
    """

    # Folder where received files will be saved
    DOWNLOADS_DIR = "downloads"

    def __init__(self, peer_name: str, listen_port: int):
        """
        Initialise the node.  Does NOT open any sockets yet — call start().

        Parameters
        ----------
        peer_name   : the name the user chose for this peer (e.g. "Alice")
        listen_port : the TCP port this peer will listen on (e.g. 5000)
        """
        self.peer_name   = peer_name
        self.listen_port = listen_port

        # Generate a universally unique ID for this peer session.
        # uuid4() is random; .hex gives a 32-character hexadecimal string.
        self.peer_id = uuid.uuid4().hex

        # --- Peer registry ---
        # Keys   : peer_id strings
        # Values : PeerInfo objects
        # Protected by _peers_lock because multiple threads add/remove peers.
        self._peers: dict[str, PeerInfo] = {}
        self._peers_lock = threading.Lock()

        # The server-side listening socket (created in start())
        self._server_sock: socket.socket | None = None

        # Flag used to signal all threads that the node is shutting down
        self._running = False

        # --- Callbacks ---
        # Default no-op lambdas so the node works even without a UI attached.
        # The UI (main.py) replaces these with real functions after creating
        # the node.
        self.on_peer_connected    = lambda info: None
        self.on_peer_disconnected = lambda peer_id: None
        self.on_text_received     = lambda peer_id, text: None
        self.on_file_received     = lambda peer_id, filename: None
        self.on_log               = lambda msg: None

        # Make sure the downloads folder exists before we need it
        os.makedirs(self.DOWNLOADS_DIR, exist_ok=True)


    # =========================================================================
    # PUBLIC API
    # =========================================================================

    def start(self) -> None:
        """
        Open the TCP listening socket and start the accept loop thread.

        Steps
        -----
        1. Create a TCP socket  (AF_INET = IPv4, SOCK_STREAM = TCP)
        2. Set SO_REUSEADDR so we can restart the app quickly without
           waiting for the OS to release the port.
        3. bind() — reserve the port on this machine.
        4. listen() — tell the OS to queue up to 16 incoming connections.
        5. Start a daemon thread that loops calling accept().
        """
        if self._running:
            raise RuntimeError("Node is already running.")

        # Step 1 & 2: Create and configure the socket
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Step 3: Bind to all available network interfaces ("0.0.0.0") so
        # peers on the LAN can reach us, not just localhost.
        self._server_sock.bind(("0.0.0.0", self.listen_port))

        # Step 4: Start listening; allow up to 16 queued connection attempts
        self._server_sock.listen(16)

        self._running = True

        # Step 5: Start the accept loop in the background.
        # daemon=True means this thread will be killed automatically when
        # the main program exits — we don't have to manage it manually.
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()

        self.on_log(
            f"[INFO] Peer '{self.peer_name}' started on port {self.listen_port} "
            f"(id={self.peer_id[:8]})"
        )

    def stop(self) -> None:
        """
        Gracefully shut down the node.

        Steps
        -----
        1. Set _running = False so all background threads notice and exit.
        2. Send a DISCONNECT notice to every connected peer.
        3. Close all peer sockets.
        4. Close the server listening socket.
        """
        self._running = False

        # Grab a snapshot of current peers so we can iterate safely
        with self._peers_lock:
            peers = list(self._peers.values())

        # Politely notify each peer before dropping the connection
        for p in peers:
            try:
                protocol.send_message(
                    p.sock,
                    protocol.make_disconnect(self.peer_id, self.peer_name)
                )
            except Exception:
                pass  # ignore errors — we are shutting down anyway
            try:
                p.sock.close()
            except Exception:
                pass

        # Clear the registry
        with self._peers_lock:
            self._peers.clear()

        # Close the listening socket (this will cause accept() to raise
        # an OSError, which the accept loop catches and then exits)
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass

        self.on_log("[INFO] Node stopped.")

    def connect_to_peer(self, ip: str, port: int) -> None:
        """
        Initiate an outgoing TCP connection to the peer at ip:port.

        This spawns a background thread (_connect_worker) so the UI
        does not freeze while waiting for the TCP handshake to complete.

        Parameters
        ----------
        ip   : IP address of the remote peer (e.g. "192.168.1.10")
        port : listening port of the remote peer (e.g. 5001)
        """
        t = threading.Thread(
            target=self._connect_worker,
            args=(ip, port),
            daemon=True,
        )
        t.start()

    def send_text(self, peer_id: str, message: str) -> None:
        """
        Send a plain-text message to a specific connected peer.

        Parameters
        ----------
        peer_id : the unique ID of the destination peer
        message : the text string to send
        """
        peer = self._get_peer(peer_id)
        if peer is None:
            self.on_log(f"[ERROR] Peer {peer_id[:8]} not found.")
            return

        # Build the JSON message dict and send it
        msg = protocol.make_text(self.peer_id, self.peer_name, message)
        self._send_message(peer, msg)

    def send_file(self, peer_id: str, file_path: str) -> None:
        """
        Send a file to a specific connected peer.

        The actual transfer is handled in a background thread so the UI
        remains responsive during long transfers (e.g. a video file).

        Parameters
        ----------
        peer_id   : unique ID of the destination peer
        file_path : absolute or relative path to the local file to send
        """
        peer = self._get_peer(peer_id)
        if peer is None:
            self.on_log(f"[ERROR] Peer {peer_id[:8]} not found.")
            return

        if not os.path.isfile(file_path):
            self.on_log(f"[ERROR] File not found: {file_path}")
            return

        # Offload the transfer to a daemon thread
        t = threading.Thread(
            target=self._send_file_worker,
            args=(peer, file_path),
            daemon=True,
        )
        t.start()

    def get_peers(self) -> list[PeerInfo]:
        """
        Return a snapshot (copy) of all currently connected peers.

        Using a copy prevents race conditions: the original dict might
        change while the caller is iterating over the list.
        """
        with self._peers_lock:
            return list(self._peers.values())


    # =========================================================================
    # INTERNAL – SERVER-SIDE: accept loop
    # =========================================================================

    def _accept_loop(self) -> None:
        """
        Runs in a background thread.  Continuously waits for and accepts
        incoming TCP connections.

        For each accepted connection a new thread is spawned to handle
        the HELLO handshake and subsequent message exchange independently.

        We set a 1-second timeout on the server socket so the loop can
        check self._running once per second and exit cleanly when stop()
        sets it to False.
        """
        self.on_log("[INFO] Listening for incoming connections …")

        while self._running:
            try:
                # Set a short timeout so we don't block here forever.
                # After 1 second we go back to the top of the loop and
                # check if _running is still True.
                self._server_sock.settimeout(1.0)

                try:
                    # accept() blocks until a peer connects, then returns
                    # a NEW socket for that specific connection plus the
                    # remote address.
                    conn, addr = self._server_sock.accept()
                except socket.timeout:
                    # No connection arrived in the last 1 second — loop again
                    continue
                except OSError:
                    # Server socket was closed (i.e. stop() was called)
                    break

                self.on_log(f"[INFO] Incoming connection from {addr[0]}:{addr[1]}")

                # Handle this connection in its own thread so we can go back
                # to accept() immediately and not block other peers.
                t = threading.Thread(
                    target=self._handle_incoming,
                    args=(conn, addr),
                    daemon=True,
                )
                t.start()

            except Exception as exc:
                if self._running:
                    self.on_log(f"[ERROR] Accept loop: {exc}")

    def _handle_incoming(self, conn: socket.socket, addr: tuple) -> None:
        """
        Server-side handshake for an incoming connection.

        Called in a new thread for each accepted connection.

        Steps
        -----
        1. Wait for the remote peer's HELLO message.
        2. Send back our HELLO_ACK.
        3. Register the peer.
        4. Enter the message loop.
        """
        try:
            # Step 1: The connecting peer always sends HELLO first.
            # If we don't receive a valid HELLO we reject the connection.
            msg = protocol.recv_message(conn)
            if msg is None or msg.get("type") != protocol.MSG_HELLO:
                self.on_log(f"[WARN] No valid HELLO from {addr}; closing.")
                conn.close()
                return

            # Extract the remote peer's identity from the HELLO message
            remote_id   = msg["peer_id"]
            remote_name = msg["peer_name"]
            remote_port = msg["port"]   # the port THEY are listening on

            # Step 2: Reply with our own HELLO_ACK so they know who we are
            ack = protocol.make_hello_ack(
                self.peer_id, self.peer_name, self.listen_port
            )
            protocol.send_message(conn, ack)

            # Step 3: Create a PeerInfo record and add it to the registry
            peer = PeerInfo(remote_id, remote_name, addr, conn, remote_port)
            self._register_peer(peer)

            # Step 4: Start the message loop for this connection.
            # This call blocks until the connection is closed.
            self._message_loop(peer)

        except Exception as exc:
            self.on_log(f"[ERROR] Incoming handler: {exc}")
            try:
                conn.close()
            except Exception:
                pass


    # =========================================================================
    # INTERNAL – CLIENT-SIDE: outgoing connection worker
    # =========================================================================

    def _connect_worker(self, ip: str, port: int) -> None:
        """
        Client-side connection and handshake.  Runs in a background thread.

        Steps
        -----
        1. Create a TCP socket and connect() to the remote peer.
        2. Send our HELLO message.
        3. Wait for their HELLO_ACK.
        4. Register the peer.
        5. Enter the message loop.
        """
        try:
            # Step 1: Create a fresh socket for this outgoing connection.
            # We set a 10-second timeout so connect() doesn't hang forever
            # if the remote peer is unreachable.
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((ip, port))   # Three-way TCP handshake happens here
            sock.settimeout(None)      # Switch back to blocking mode

            # Step 2: Introduce ourselves with a HELLO
            hello = protocol.make_hello(
                self.peer_id, self.peer_name, self.listen_port
            )
            protocol.send_message(sock, hello)

            # Step 3: Wait for the remote peer's HELLO_ACK
            msg = protocol.recv_message(sock)
            if msg is None or msg.get("type") != protocol.MSG_HELLO_ACK:
                self.on_log(f"[ERROR] No valid HELLO_ACK from {ip}:{port}")
                sock.close()
                return

            remote_id   = msg["peer_id"]
            remote_name = msg["peer_name"]
            remote_port = msg["port"]

            # Step 4: Register the peer
            peer = PeerInfo(remote_id, remote_name, (ip, port), sock, remote_port)
            self._register_peer(peer)

            # Step 5: Enter the shared message loop
            self._message_loop(peer)

        except ConnectionRefusedError:
            # The remote peer is not running or the port is wrong
            self.on_log(f"[ERROR] Connection refused: {ip}:{port}")
        except socket.timeout:
            # connect() took longer than 10 seconds
            self.on_log(f"[ERROR] Connection timed out: {ip}:{port}")
        except OSError as exc:
            self.on_log(f"[ERROR] Connection failed: {exc}")
        except Exception as exc:
            self.on_log(f"[ERROR] Unexpected error connecting to {ip}:{port}: {exc}")


    # =========================================================================
    # INTERNAL – shared message loop (used by both server and client sides)
    # =========================================================================

    def _message_loop(self, peer: PeerInfo) -> None:
        """
        Continuously read and dispatch messages from *peer*.

        This function runs in its own thread (one per connection) and
        blocks on recv_message() until a message arrives.  When the
        connection is closed (by either side) recv_message() returns None
        and we exit the loop.

        The finally block ensures the peer is always unregistered —
        even if an unexpected exception occurs.
        """
        try:
            while self._running:
                # Blocking call: waits here until a message arrives
                msg = protocol.recv_message(peer.sock)

                if msg is None:
                    # None means EOF — the peer closed the connection
                    break

                # Route the message to the correct handler based on its type
                self._dispatch(peer, msg)

        except Exception as exc:
            # A socket error (e.g. network drop) will land here
            if self._running:
                self.on_log(
                    f"[ERROR] Message loop ({peer.display_name}): {exc}"
                )
        finally:
            # Always clean up the peer entry when the loop ends,
            # regardless of whether it ended cleanly or with an error.
            self._unregister_peer(peer)

    def _dispatch(self, peer: PeerInfo, msg: dict) -> None:
        """
        Route an incoming message to the appropriate handler.

        This is the "demultiplexer": it reads the "type" field of the
        JSON message and calls the right function.

        Supported types: text, file, disconnect
        """
        msg_type = msg.get("type")

        if msg_type == protocol.MSG_TEXT:
            # ── Text message ─────────────────────────────────────────
            text = msg.get("message", "")
            self.on_log(f"{peer.peer_name} -> You: {text}")
            # Notify the UI layer
            self.on_text_received(peer.peer_id, text)

        elif msg_type == protocol.MSG_FILE:
            # ── File transfer ─────────────────────────────────────────
            # The JSON metadata has already arrived; now read the raw bytes.
            filename = msg.get("filename", "unknown")
            filesize = int(msg.get("filesize", 0))
            # _receive_file() reads exactly <filesize> bytes from the socket
            self._receive_file(peer, filename, filesize)

        elif msg_type == protocol.MSG_DISCONNECT:
            # ── Graceful disconnect notice ────────────────────────────
            # The remote peer told us it is closing the connection.
            # We log it; _unregister_peer() will be called in the finally
            # block of _message_loop() when recv_message() returns None.
            self.on_log(
                f"[INFO] {peer.display_name} disconnected gracefully."
            )

        else:
            # Unknown message type — log a warning and keep going
            self.on_log(
                f"[WARN] Unknown message type '{msg_type}' "
                f"from {peer.display_name}"
            )


    # =========================================================================
    # INTERNAL – file receive
    # =========================================================================

    def _receive_file(self, peer: PeerInfo, filename: str, filesize: int) -> None:
        """
        Receive raw file bytes from *peer* and save them to the downloads folder.

        Called immediately after the file-metadata JSON message is dispatched.
        At this point the NEXT bytes arriving on the socket are the raw file
        content — we read exactly <filesize> of them.

        Collision handling
        ------------------
        If a file with the same name already exists in downloads/ we append
        _1, _2, etc. so no file is ever overwritten silently.
        """
        # Build the destination path
        dest = os.path.join(self.DOWNLOADS_DIR, filename)

        # Avoid overwriting existing files with the same name
        base, ext = os.path.splitext(dest)
        counter = 1
        while os.path.exists(dest):
            dest = f"{base}_{counter}{ext}"
            counter += 1

        self.on_log(
            f"[FILE] Receiving '{filename}' ({filesize:,} bytes) "
            f"from {peer.display_name} …"
        )

        try:
            # Progress callback: log at 0 %, 25 %, 50 %, 75 %, 100 %
            def progress(received, total):
                pct = received * 100 // total
                if pct % 25 == 0:
                    self.on_log(
                        f"[FILE] {os.path.basename(dest)} – {pct}%"
                    )

            # Delegate the actual byte reading to protocol.recv_file_bytes()
            protocol.recv_file_bytes(peer.sock, filesize, dest, progress)

            self.on_log(f"[FILE] Saved: {dest}")
            # Notify the UI so it can display "File received: photo.jpg"
            self.on_file_received(peer.peer_id, os.path.basename(dest))

        except Exception as exc:
            self.on_log(f"[ERROR] File receive failed: {exc}")
            # Delete the partial (incomplete) file so we don't leave garbage
            try:
                os.remove(dest)
            except OSError:
                pass


    # =========================================================================
    # INTERNAL – file send worker
    # =========================================================================

    def _send_file_worker(self, peer: PeerInfo, file_path: str) -> None:
        """
        Send a file to *peer*.  Runs in a background thread.

        Two-phase transfer
        ------------------
        Phase 1 – Send the metadata JSON message (framed with 4-byte header).
                  The receiver's _dispatch() sees this and calls _receive_file().

        Phase 2 – Send the raw file bytes in 64 KiB chunks.
                  The receiver's _receive_file() reads exactly <filesize> bytes.

        The peer.lock mutex is held for the ENTIRE transfer so that no other
        thread can interleave bytes between the metadata and the file data.
        """
        filename = os.path.basename(file_path)
        filesize = os.path.getsize(file_path)

        # Build the file metadata message
        meta = protocol.make_file_meta(
            self.peer_id, self.peer_name, filename, filesize
        )

        self.on_log(
            f"[FILE] Sending '{filename}' ({filesize:,} bytes) "
            f"to {peer.display_name} …"
        )

        # Acquire the send lock so no other thread sends on this socket
        # while we are in the middle of a file transfer.
        with peer.lock:
            try:
                # Phase 1: send the JSON metadata
                protocol.send_message(peer.sock, meta)

                # Progress callback for the sender side
                def progress(sent, total):
                    pct = sent * 100 // total
                    if pct % 25 == 0:
                        self.on_log(
                            f"[FILE] {filename} → {peer.peer_name}: {pct}%"
                        )

                # Phase 2: stream the actual file bytes
                protocol.send_file_bytes(peer.sock, file_path, progress)

                self.on_log(
                    f"[FILE] '{filename}' sent successfully to "
                    f"{peer.display_name}."
                )

            except Exception as exc:
                self.on_log(f"[ERROR] File send failed: {exc}")


    # =========================================================================
    # INTERNAL – peer registry helpers
    # =========================================================================

    def _send_message(self, peer: PeerInfo, msg: dict) -> None:
        """
        Thread-safe wrapper around protocol.send_message().

        Acquires peer.lock before sending so that simultaneous sends from
        different threads (e.g. text from the UI thread while a file send
        is in progress) don't corrupt the byte stream.
        """
        with peer.lock:
            try:
                protocol.send_message(peer.sock, msg)
            except Exception as exc:
                self.on_log(
                    f"[ERROR] Send to {peer.display_name} failed: {exc}"
                )

    def _register_peer(self, peer: PeerInfo) -> None:
        """
        Add *peer* to the registry and fire the on_peer_connected callback.

        The lock ensures the dict is not modified simultaneously by two
        threads that both just completed a handshake.
        """
        with self._peers_lock:
            self._peers[peer.peer_id] = peer

        self.on_log(
            f"[INFO] Connected: {peer.display_name} "
            f"({peer.address[0]}:{peer.listen_port})"
        )

        # Tell the UI to add this peer to the connected-peers list
        self.on_peer_connected({
            "peer_id":     peer.peer_id,
            "peer_name":   peer.peer_name,
            "address":     peer.address[0],
            "listen_port": peer.listen_port,
        })

    def _unregister_peer(self, peer: PeerInfo) -> None:
        """
        Remove *peer* from the registry, close its socket, and fire
        the on_peer_disconnected callback.

        Called from the finally block of _message_loop(), so it always
        executes when a connection ends — regardless of the reason.
        """
        with self._peers_lock:
            self._peers.pop(peer.peer_id, None)  # remove; ignore if already gone

        # Close the socket (ignore errors — it may already be closed)
        try:
            peer.sock.close()
        except Exception:
            pass

        self.on_log(f"[INFO] Disconnected: {peer.display_name}")

        # Tell the UI to remove this peer from the connected-peers list
        self.on_peer_disconnected(peer.peer_id)

    def _get_peer(self, peer_id: str) -> PeerInfo | None:
        """
        Look up and return the PeerInfo for *peer_id*, or None if not found.
        Thread-safe via _peers_lock.
        """
        with self._peers_lock:
            return self._peers.get(peer_id)
