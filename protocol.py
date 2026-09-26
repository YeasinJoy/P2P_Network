"""
=============================================================================
protocol.py  —  Application-Level Message Protocol
=============================================================================

PURPOSE
-------
This module defines HOW peers talk to each other once a TCP connection has
been established.  It covers two responsibilities:

  1. MESSAGE FRAMING
     TCP is a "byte-stream" protocol.  That means the network does NOT
     automatically tell the receiver where one message ends and the next
     one begins.  We must do that ourselves.

     Solution: before every JSON message we send a 4-byte header that
     contains the length (number of bytes) of the message body.

         ┌─────────────────────┬──────────────────────────────────┐
         │  4-byte length (N)  │  N bytes of UTF-8 encoded JSON   │
         └─────────────────────┴──────────────────────────────────┘

     The receiver first reads exactly 4 bytes, unpacks the integer N,
     then reads exactly N more bytes to get the complete JSON message.

  2. MESSAGE TYPES
     Every JSON message has a "type" field so the receiver knows what
     kind of message it got and how to react.

     Supported types:
       hello        → first message sent by the connecting peer (handshake)
       hello_ack    → acknowledgement sent back by the accepting peer
       text         → a plain-text chat message
       file         → file metadata (filename + size); raw bytes follow
       disconnect   → polite goodbye before closing the connection

  FILE TRANSFER OVERVIEW
  ----------------------
  Sending a file is a two-step process:

    Step 1 – Send a framed JSON "file" message with metadata:
               { "type": "file", "filename": "photo.jpg", "filesize": 2456789 }

    Step 2 – Send the raw binary bytes of the file (no framing needed here
             because the receiver already knows the exact byte count from
             the metadata).

  The receiver reads exactly <filesize> bytes and writes them to disk.

Dependencies: only Python standard-library modules.
=============================================================================
"""

import json    # for encoding/decoding JSON messages
import struct  # for packing/unpacking the 4-byte length header
import os      # for getting the file size before sending


# =============================================================================
# SECTION 1 – MESSAGE TYPE CONSTANTS
# =============================================================================
# Using named constants instead of raw strings avoids typos and makes the
# code self-documenting.

MSG_HELLO      = "hello"       # initial handshake message
MSG_HELLO_ACK  = "hello_ack"   # handshake reply
MSG_TEXT       = "text"        # chat message
MSG_FILE       = "file"        # file transfer (metadata + raw bytes)
MSG_DISCONNECT = "disconnect"  # graceful disconnection notice

# Size of each chunk when reading/writing a file.
# 64 KiB is a practical balance: small enough to avoid huge RAM usage,
# large enough to keep the transfer fast.
CHUNK_SIZE = 64 * 1024   # 64 KiB  (= 65 536 bytes)


# =============================================================================
# SECTION 2 – LOW-LEVEL FRAMING  (send / receive one JSON message)
# =============================================================================

def send_message(sock, payload: dict) -> None:
    """
    Encode *payload* as a UTF-8 JSON string and send it over *sock*
    with a 4-byte big-endian length prefix.

    Parameters
    ----------
    sock    : a connected TCP socket object
    payload : the Python dictionary to send as a JSON message

    How it works
    ------------
    1. json.dumps()  converts the dict to a JSON string.
    2. .encode()     converts the string to raw bytes (UTF-8).
    3. struct.pack() creates a 4-byte big-endian unsigned integer
                     that holds the byte count of the JSON data.
    4. sock.sendall() sends header + data in one call, guaranteeing
                      that all bytes are transmitted even on slow links.
    """
    # Convert the Python dictionary into a JSON-formatted byte string
    data = json.dumps(payload).encode("utf-8")

    # Pack the length as a 4-byte big-endian unsigned integer.
    # ">I" means: big-endian (>) unsigned int (I) — 4 bytes total.
    header = struct.pack(">I", len(data))

    # Send header and data together; sendall() loops internally until
    # every byte has been handed to the OS network stack.
    sock.sendall(header + data)


def recv_message(sock) -> dict | None:
    """
    Receive one framed message from *sock* and return it as a dict.

    Returns None if the connection was closed cleanly (EOF).
    Raises an exception on any other socket or decoding error
    (the caller is responsible for catching those).

    How it works
    ------------
    1. Read exactly 4 bytes → the length header.
    2. Unpack the integer N from those 4 bytes.
    3. Read exactly N bytes → the JSON body.
    4. Decode and parse the JSON, return the resulting dict.
    """
    # Step 1: Read the 4-byte length header
    header = _recv_exactly(sock, 4)
    if header is None:
        # None means the peer closed the connection (EOF)
        return None

    # Step 2: Unpack the 4 bytes into a plain Python integer
    length = struct.unpack(">I", header)[0]

    # Step 3: Read exactly that many bytes (the JSON message body)
    data = _recv_exactly(sock, length)
    if data is None:
        return None  # connection closed mid-message

    # Step 4: Decode UTF-8 bytes → string → Python dict
    return json.loads(data.decode("utf-8"))


def _recv_exactly(sock, n: int) -> bytes | None:
    """
    Internal helper: read exactly *n* bytes from *sock*.

    Why do we need this?
    --------------------
    A single call to sock.recv(n) is NOT guaranteed to return all n bytes,
    especially on a slow or congested network.  It may return fewer bytes
    (a "short read").  We loop until we have collected exactly n bytes.

    Returns None if the connection is closed before n bytes arrive.
    """
    buf = b""  # accumulator for received bytes

    while len(buf) < n:
        # Ask for however many bytes are still missing
        chunk = sock.recv(n - len(buf))

        if not chunk:
            # recv() returned an empty bytes object → the peer has closed
            # the connection (TCP FIN received).
            return None

        buf += chunk  # append the received bytes to our accumulator

    return buf  # we now have exactly n bytes


# =============================================================================
# SECTION 3 – FILE-LEVEL SEND / RECEIVE  (raw binary bytes, no framing)
# =============================================================================

def recv_file_bytes(sock, filesize: int, dest_path: str,
                    progress_cb=None) -> None:
    """
    Read exactly *filesize* raw bytes from *sock* and write them to
    the file at *dest_path*.

    The file is written in CHUNK_SIZE pieces to avoid loading the entire
    file into memory at once (important for large video files).

    Parameters
    ----------
    sock        : the TCP socket to read from
    filesize    : exact number of bytes expected (from the file metadata)
    dest_path   : local file path where the received data will be saved
    progress_cb : optional function(bytes_received, total_bytes) called
                  after each chunk so the UI can update a progress bar

    How it works
    ------------
    We keep a running total of how many bytes we have received.
    Each iteration we ask for min(CHUNK_SIZE, remaining) bytes.
    We stop when received == filesize.
    """
    received = 0  # total bytes received so far

    # Open the destination file in binary-write mode
    with open(dest_path, "wb") as f:
        while received < filesize:
            # Calculate how many bytes to request in this iteration.
            # On the last iteration this will be less than CHUNK_SIZE.
            to_read = min(CHUNK_SIZE, filesize - received)

            chunk = sock.recv(to_read)

            if not chunk:
                # Connection closed before the file was fully received
                raise ConnectionError("Connection closed during file transfer")

            f.write(chunk)            # write this chunk to disk immediately
            received += len(chunk)    # update the byte counter

            # Notify the UI layer if a callback was provided
            if progress_cb:
                progress_cb(received, filesize)


def send_file_bytes(sock, file_path: str, progress_cb=None) -> None:
    """
    Read the file at *file_path* in chunks and send all bytes over *sock*.

    This function is called AFTER the file-metadata JSON message has
    already been sent, so the receiver knows exactly how many bytes to expect.

    Parameters
    ----------
    sock        : the TCP socket to send on
    file_path   : path to the local file to send
    progress_cb : optional function(bytes_sent, total_bytes)

    How it works
    ------------
    We open the file in binary-read mode and loop, reading CHUNK_SIZE
    bytes at a time, until read() returns an empty bytes object (EOF).
    Each chunk is sent immediately with sendall().
    """
    filesize = os.path.getsize(file_path)  # used only for progress reporting
    sent = 0

    # Open the file in binary-read mode so we handle any file type correctly
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)  # read up to 64 KiB

            if not chunk:
                break  # end of file reached

            # sendall() guarantees all bytes in the chunk are sent
            sock.sendall(chunk)
            sent += len(chunk)

            if progress_cb:
                progress_cb(sent, filesize)


# =============================================================================
# SECTION 4 – MESSAGE CONSTRUCTORS
# =============================================================================
# These functions create the Python dictionaries that get serialised to JSON.
# Having one function per message type keeps the rest of the code clean and
# makes it easy to change the protocol format in one place.

def make_hello(peer_id: str, peer_name: str, port: int) -> dict:
    """
    Build the HELLO handshake message.

    Sent by the connecting peer immediately after the TCP connection is
    established.  It introduces this peer to the remote side.

    Fields
    ------
    type      : always "hello" so the receiver knows what this is
    peer_id   : a unique 32-character hex ID (generated once at startup)
    peer_name : the human-readable name the user chose
    port      : the port this peer is *listening* on (not the ephemeral
                source port of this connection)
    """
    return {
        "type":      MSG_HELLO,
        "peer_id":   peer_id,
        "peer_name": peer_name,
        "port":      port,
    }


def make_hello_ack(peer_id: str, peer_name: str, port: int) -> dict:
    """
    Build the HELLO_ACK (acknowledgement) message.

    Sent by the accepting peer in reply to a HELLO.  After both sides
    have exchanged HELLO / HELLO_ACK the handshake is complete and
    normal messaging can begin.
    """
    return {
        "type":      MSG_HELLO_ACK,
        "peer_id":   peer_id,
        "peer_name": peer_name,
        "port":      port,
    }


def make_text(sender_id: str, sender_name: str, message: str) -> dict:
    """
    Build a TEXT message carrying a plain chat string.

    Fields
    ------
    sender_id   : unique ID of the sender (helps the receiver identify who wrote it)
    sender_name : human-readable name (shown in the chat log)
    message     : the actual text content typed by the user
    """
    return {
        "type":        MSG_TEXT,
        "sender_id":   sender_id,
        "sender_name": sender_name,
        "message":     message,
    }


def make_file_meta(sender_id: str, sender_name: str,
                   filename: str, filesize: int) -> dict:
    """
    Build the FILE METADATA message.

    This is the framed JSON message sent BEFORE the raw file bytes.
    The receiver uses 'filesize' to know exactly how many raw bytes to
    read from the socket after this message.

    Fields
    ------
    filename : the original file name (e.g. "photo.jpg")
    filesize : exact byte count of the file — critical for the receiver
               to know when the file transfer is complete
    """
    return {
        "type":        MSG_FILE,
        "sender_id":   sender_id,
        "sender_name": sender_name,
        "filename":    filename,
        "filesize":    filesize,
    }


def make_disconnect(peer_id: str, peer_name: str) -> dict:
    """
    Build a DISCONNECT notice.

    Sent by a peer that is about to close the connection voluntarily
    (e.g. when the user clicks "Stop").  This lets the remote side
    display a clean "peer disconnected" message instead of an error.
    """
    return {
        "type":      MSG_DISCONNECT,
        "peer_id":   peer_id,
        "peer_name": peer_name,
    }
