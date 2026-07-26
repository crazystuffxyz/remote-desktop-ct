// server.js — signaling + binary relay for the remote desktop
// roles: "host" (client.py, offers) and "controller" (browser, answers)
// fix: handshake order — host offers on peer-joined OR room-state (no deadlock)

'use strict';

const express = require('express');
const { WebSocketServer, WebSocket } = require('ws');
const path = require('path');

const PORT = process.env.PORT || 3000;
const MAX_BINARY_BUFFER = 1 * 1024 * 1024; // drop stale media frames if client is backed up
const HEARTBEAT_MS = 30_000;

const app = express();
const server = app.listen(PORT, () => console.log(`[Server] Listening on :${PORT}`));

const wss = new WebSocketServer({
    server,
    maxPayload: 16 * 1024 * 1024,
    perMessageDeflate: false,
});

app.use(express.static('public'));
app.get('/desktop/:key', (req, res) =>
    res.sendFile(path.join(__dirname, 'public', 'control.html')));

// rooms: key -> Set<ws>
const rooms = new Map();

function roomAdd(key, ws) {
    if (!rooms.has(key)) rooms.set(key, new Set());
    rooms.get(key).add(ws);
}

function roomRemove(key, ws) {
    if (!rooms.has(key)) return;
    rooms.get(key).delete(ws);
    if (rooms.get(key).size === 0) rooms.delete(key);
}

function roomPeers(key) {
    return rooms.get(key) || new Set();
}

function countRole(key, role) {
    let n = 0;
    for (const c of roomPeers(key)) {
        if (c._role === role && c.readyState === WebSocket.OPEN) n++;
    }
    return n;
}

// JSON/control: always send. Binary: drop if peer is congested.
function safeSend(client, data, isBinary) {
    if (client.readyState !== WebSocket.OPEN) return;
    if (isBinary && client.bufferedAmount > MAX_BINARY_BUFFER) return;
    try { client.send(data, { binary: isBinary }); }
    catch (_) {} // socket may have died between check and send
}

function roomBroadcast(key, senderWs, data, isBinary) {
    for (const client of roomPeers(key)) {
        if (client === senderWs) continue;
        safeSend(client, data, isBinary);
    }
}

function roomSendToRole(key, senderWs, role, data) {
    for (const client of roomPeers(key)) {
        if (client === senderWs) continue;
        if (client._role !== role) continue;
        safeSend(client, data, false);
    }
}

wss.on('connection', (ws) => {
    ws._id = Math.random().toString(36).slice(2, 9);
    ws._key = null;
    ws._role = null; // "host" | "controller"
    ws.isAlive = true;
    ws.missedPongs = 0;

    ws.on('pong', () => { ws.isAlive = true; ws.missedPongs = 0; });

    ws.on('message', (data, isBinary) => {
        if (isBinary) {
            if (ws._key) roomBroadcast(ws._key, ws, data, true);
            return;
        }

        let msg;
        try { msg = JSON.parse(data.toString()); }
        catch { return; }

        if (msg.type === 'join' && msg.key) {
            ws._key = String(msg.key);
            // default to controller for backward compat
            ws._role = (msg.role === 'host') ? 'host' : 'controller';

            roomAdd(ws._key, ws);

            const hasHost = countRole(ws._key, 'host') > 0;
            const hasController = countRole(ws._key, 'controller') > 0;

            console.log(`[Join]  id=${ws._id} room=${ws._key} role=${ws._role} ` +
                        `size=${roomPeers(ws._key).size} host=${hasHost} ctrl=${hasController}`);

            try {
                ws.send(JSON.stringify({
                    type: 'room-state',
                    hasHost,
                    hasController,
                    you: ws._role,
                }));
            } catch (_) {}

            // tell the others a new peer arrived — host uses this as its offer trigger
            roomBroadcast(ws._key, ws, JSON.stringify({ type: 'peer-joined', role: ws._role }), false);
            return;
        }

        if (!ws._key) return;

        if (msg.type === 'input') {
            roomSendToRole(ws._key, ws, 'host', data);
            return;
        }

        if (msg.type === 'audio-swallow') {
            roomSendToRole(ws._key, ws, 'host', data);
            return;
        }

        if (msg.type === 'rtc-signal') {
            // host's offer/candidate -> controller; controller's answer/cand. -> host
            const targetRole = (ws._role === 'host') ? 'controller' : 'host';
            roomSendToRole(ws._key, ws, targetRole, data);
            return;
        }
    });

    ws.on('close', () => {
        if (ws._key) {
            const key = ws._key;
            roomRemove(key, ws);
            console.log(`[Leave] id=${ws._id} room=${key} role=${ws._role}`);
            roomBroadcast(key, ws, JSON.stringify({ type: 'peer-left', role: ws._role }), false);
        }
    });

    ws.on('error', (err) => console.error(`[WS Error] id=${ws._id}: ${err.message}`));
});

// need TWO consecutive missed pings to kill — single delayed pong through ngrok
// shouldn't nuke a healthy socket (this used to cause the 1011 timeouts)
const heartbeat = setInterval(() => {
    wss.clients.forEach((ws) => {
        if (ws.missedPongs === undefined) ws.missedPongs = 0;
        if (!ws.isAlive) {
            ws.missedPongs++;
            if (ws.missedPongs >= 2) {
                console.log(`[HB] Terminating dead socket id=${ws._id}`);
                ws.terminate();
                return;
            }
        } else {
            ws.missedPongs = 0;
        }
        ws.isAlive = false;
        try { ws.ping(); } catch (_) {}
    });
}, HEARTBEAT_MS);

server.on('close', () => clearInterval(heartbeat));
