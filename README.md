# P2P Network — CSE 433

A lightweight peer-to-peer communication application built in Python using TCP sockets.

University of Asia Pacific | Department of CSE | CSE 433 — Blockchain & Distributed Security Lab

---

## Project Description

Each running instance of `main.py` is one **peer**. Every peer acts as both:
- A **TCP server** — listens for and accepts incoming connections from other peers.
- A **TCP client** — can connect outward to other peers.

Peers communicate directly without any central server.

```
Peer A  <-->  Peer B  <-->  Peer C
  ^___________________________|
```

---

## File Structure

```
P2P_Network/
│
├── main.py          ← Terminal UI and menu loop
├── p2p_node.py      ← Core networking (server + client + file transfer)
├── protocol.py      ← Message framing and encoding/decoding
├── requirements.txt ← No third-party packages needed
├── README.md        ← This file
│
└── downloads/       ← Received files are saved here automatically
```

---

## Requirements

- Python **3.9 or later**
- No external packages — only Python's standard library is used

---

## How to Run

Open a terminal and run:

```bash
python main.py
```

You will see a numbered menu. Use the numbers `1`–`6` to navigate.

---

## How to Test on One Computer (Two Terminal Windows)

**Terminal 1 — Peer Alice:**
```
[1] Start this peer
    Name:  Alice
    Port:  5000
```

**Terminal 2 — Peer Bob:**
```
[1] Start this peer
    Name:  Bob
    Port:  5001

[2] Connect to another peer
    IP:    127.0.0.1
    Port:  5000
```

Both terminals will confirm the connection.

---

## How to Test on Two Computers (Same LAN/Wi-Fi)

1. Find Computer A's local IP: `ipconfig` (Windows) or `ip a` (Linux/Mac)
2. Run Peer A on Computer A (port 5000).
3. Run Peer B on Computer B (port 5001).
4. Peer B connects to Computer A's IP at port 5000.

---

## How to Connect Two Peers

1. Start Peer A → option **[1]**, name = Alice, port = 5000
2. Start Peer B → option **[1]**, name = Bob, port = 5001
3. From Peer B, select option **[2]**, enter Peer A's IP and port 5000
4. Both peers will show a "connected" message
5. Select option **[3]** on either peer to confirm the connection

---

## How to Send a Text Message

1. Select option **[4]** — Send a text message
2. Choose the peer number from the list
3. Type your message and press Enter
4. The message appears on the recipient's terminal immediately

---

## How to Transfer a File

1. Select option **[5]** — Send a file
2. Choose the peer number from the list
3. Enter the **full path** to the file, e.g.:
   - Windows: `C:\Users\User\Desktop\photo.jpg`
   - Linux/Mac: `/home/user/photo.jpg`
4. The file is sent in binary chunks (works for images, audio, video, PDF, ZIP, etc.)
5. The recipient sees a "File received" message and the file is saved in `downloads/`

---

## Technical Summary

| Concept        | Implementation                                      |
|----------------|-----------------------------------------------------|
| Transport      | TCP (reliable, ordered byte stream)                 |
| Concurrency    | One thread per peer connection                      |
| Message framing| 4-byte length prefix + JSON payload                 |
| Handshake      | `hello` / `hello_ack` messages exchanged on connect |
| File transfer  | Binary chunks (64 KB each), preceded by metadata   |
| Peer identity  | Short UUID hex ID generated at startup              |

---

## Menu Reference

| Option | Action                    |
|--------|---------------------------|
| 1      | Start this peer           |
| 2      | Connect to another peer   |
| 3      | List connected peers      |
| 4      | Send a text message       |
| 5      | Send a file               |
| 6      | Stop and exit             |
