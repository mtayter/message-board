package com.slick.walkmode.v3;

import android.util.Base64;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;

import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/**
 * Minimal WebSocket (RFC 6455) client for the board's WSS endpoint.
 * Used by the watch to receive the peer agent's replies via push instead of HTTP polling.
 *
 * Protocol:
 *   - Connect to wss://host/board/ws (or ws:// for LAN).
 *   - Send {"type":"hello","token":"...","since":123}
 *   - Receive {"type":"backlog","messages":[...]} then {"type":"message","message":{...}}
 *
 * This is a blocking client: call connect() then loop on readMessage().
 * NEVER call from the main thread.
 */
public class BoardWssClient {
    private static final String TAG = "BoardWss";

    /** Callback for received board messages. */
    public interface MessageHandler {
        void onMessage(JSONObject msg);
        void onClose();
    }

    private final String mHost;
    private final int mPort;
    private final boolean mUseTls;
    private final String mPath;
    private final String mToken;
    private final long mSince;
    private volatile MessageHandler mHandler;

    private Socket mSocket;
    private InputStream mIn;
    private OutputStream mOut;
    private volatile boolean mRunning = false;
    /** Serializes frame writes: with one shared connection, sends (from
     *  the activity's send thread) and pongs (from the read thread) must
     *  never interleave bytes on the wire. */
    private final Object mWriteLock = new Object();

    public BoardWssClient(String host, int port, boolean useTls, String path,
                          String token, long since, MessageHandler handler) {
        mHost = host;
        mPort = port;
        mUseTls = useTls;
        mPath = path;
        mToken = token;
        mSince = since;
        mHandler = handler;
    }

    /** Update the message handler (e.g., when RelayService takes over from MainActivity). */
    public void setHandler(MessageHandler handler) {
        mHandler = handler;
    }

    /** Send a message via WSS. Returns true if the send was written successfully.
     *  Does NOT wait for the sent-ack (the read loop handles it asynchronously).
     *  This avoids consuming push messages while waiting for the ack.
     *  Location is optional metadata: {"lat": ..., "lon": ...}
     *  RecordedAt is optional metadata: wall-clock ms when recording ended. */
    public boolean sendMessage(String to, String body, Long replyTo) {
        return sendMessage(to, body, replyTo, null, 0);
    }

    public boolean sendMessage(String to, String body, Long replyTo, org.json.JSONObject location) {
        return sendMessage(to, body, replyTo, location, 0);
    }

    public boolean sendMessage(String to, String body, Long replyTo,
                               org.json.JSONObject location, long recordedAt) {
        try {
            JSONObject send = new JSONObject();
            send.put("type", "send");
            send.put("to", to);
            send.put("body", body);
            if (replyTo != null) send.put("reply_to", replyTo);
            if (location != null) send.put("location", location);
            if (recordedAt > 0) send.put("recorded_at", recordedAt / 1000.0);
            sendText(send.toString());
            return true;
        } catch (Exception e) {
            Log.w(TAG, "WSS send failed: " + e.getMessage());
            return false;
        }
    }

    /** Connect and send hello. Call before sendMessage or startReading. */
    public void connectAndHello() throws Exception {
        connect();
        sendHello();
        mRunning = true;
    }

    /** Start the read loop with a handler. Blocks until closed. */
    public void startReading() {
        try {
            readLoop();
        } catch (Exception e) {
            Log.w(TAG, "WSS read error: " + e.getMessage());
        } finally {
            mRunning = false;
            closeQuietly();
            if (mHandler != null) mHandler.onClose();
        }
    }

    /** Connect, send hello, and start reading. Blocks until closed or error. */
    public void run() {
        try {
            connectAndHello();
            startReading();
        } catch (Exception e) {
            Log.w(TAG, "WSS error: " + e.getMessage());
            mRunning = false;
            closeQuietly();
            if (mHandler != null) mHandler.onClose();
        }
    }

    public void stop() {
        mRunning = false;
        closeQuietly();
    }

    private void connect() throws Exception {
        if (mUseTls) {
            SSLSocketFactory factory = (SSLSocketFactory) SSLSocketFactory.getDefault();
            SSLSocket ssl = (SSLSocket) factory.createSocket(mHost, mPort);
            ssl.startHandshake();
            mSocket = ssl;
        } else {
            mSocket = new Socket(mHost, mPort);
        }
        mSocket.setSoTimeout(30000); // 30s read timeout for ping/pong
        mIn = mSocket.getInputStream();
        mOut = mSocket.getOutputStream();

        // WebSocket handshake
        String key = generateKey();
        String request = "GET " + mPath + " HTTP/1.1\r\n" +
                "Host: " + mHost + "\r\n" +
                "Upgrade: websocket\r\n" +
                "Connection: Upgrade\r\n" +
                "Sec-WebSocket-Key: " + key + "\r\n" +
                "Sec-WebSocket-Version: 13\r\n" +
                "\r\n";
        mOut.write(request.getBytes(StandardCharsets.UTF_8));
        mOut.flush();

        // Read HTTP response headers manually (byte-by-byte) to avoid
        // BufferedReader swallowing WebSocket frame bytes.
        ByteArrayOutputStream headerBuf = new ByteArrayOutputStream();
        int b;
        int match = 0; // tracks "\r\n\r\n" sequence
        while ((b = mIn.read()) != -1) {
            headerBuf.write(b);
            if (b == '\r' || b == '\n') {
                // Simple check for end of headers: look for \r\n\r\n
                byte[] hb = headerBuf.toByteArray();
                int len = hb.length;
                if (len >= 4 && hb[len-4] == '\r' && hb[len-3] == '\n'
                        && hb[len-2] == '\r' && hb[len-1] == '\n') {
                    break;
                }
            }
            if (headerBuf.size() > 8192) {
                throw new Exception("Headers too large");
            }
        }
        String headers = headerBuf.toString("UTF-8");
        if (!headers.contains("101")) {
            throw new Exception("WSS handshake failed: " + headers.split("\r\n")[0]);
        }
        Log.i(TAG, "WSS connected to " + mHost);
    }

    private void sendHello() throws Exception {
        JSONObject hello = new JSONObject();
        hello.put("type", "hello");
        hello.put("token", mToken);
        hello.put("since", mSince);
        sendText(hello.toString());
    }

    private void readLoop() throws Exception {
        Log.i(TAG, "WSS readLoop started");
        while (mRunning) {
            String text = readTextFrame();
            if (text == null) {
                Log.i(TAG, "WSS readLoop: connection closed by server");
                break;
            }
            Log.i(TAG, "WSS received: " + text.substring(0, Math.min(100, text.length())));

            JSONObject obj = new JSONObject(text);
            String type = obj.optString("type", "");

            if ("backlog".equals(type)) {
                JSONArray msgs = obj.optJSONArray("messages");
                if (msgs != null) {
                    for (int i = 0; i < msgs.length(); i++) {
                        mHandler.onMessage(msgs.getJSONObject(i));
                    }
                }
            } else if ("message".equals(type)) {
                // Live push: fields are at top-level (not nested under "message")
                mHandler.onMessage(obj);
            } else if ("error".equals(type)) {
                Log.w(TAG, "WSS error from server: " + obj.optString("error"));
                break;
            }
            // "hello-ok" and "sent-ack" are ignored for receive-only
        }
    }

    // --- WebSocket framing (RFC 6455) ---

    private void sendText(String text) throws Exception {
        synchronized (mWriteLock) {
            byte[] payload = text.getBytes(StandardCharsets.UTF_8);
            ByteArrayOutputStream frame = new ByteArrayOutputStream();

            frame.write(0x81); // FIN + text opcode

            // Masked, with length
            int len = payload.length;
            if (len < 126) {
                frame.write(0x80 | len);
            } else if (len < 65536) {
                frame.write(0x80 | 126);
                frame.write((len >> 8) & 0xFF);
                frame.write(len & 0xFF);
            } else {
                frame.write(0x80 | 127);
                for (int i = 7; i >= 0; i--) {
                    frame.write((len >> (8 * i)) & 0xFF);
                }
            }

            // Masking key
            byte[] mask = new byte[4];
            new Random().nextBytes(mask);
            frame.write(mask);

            // Masked payload
            for (int i = 0; i < payload.length; i++) {
                frame.write(payload[i] ^ mask[i % 4]);
            }

            mOut.write(frame.toByteArray());
            mOut.flush();
        }
    }

    private String readTextFrame() throws Exception {
        while (true) {
            int b0 = mIn.read();
            if (b0 == -1) return null;
            int b1 = mIn.read();
            if (b1 == -1) return null;

            boolean fin = (b0 & 0x80) != 0;
            int opcode = b0 & 0x0F;
            boolean masked = (b1 & 0x80) != 0;
            long payloadLen = b1 & 0x7F;

            if (payloadLen == 126) {
                payloadLen = ((mIn.read() & 0xFF) << 8) | (mIn.read() & 0xFF);
            } else if (payloadLen == 127) {
                payloadLen = 0;
                for (int i = 0; i < 8; i++) {
                    payloadLen = (payloadLen << 8) | (mIn.read() & 0xFF);
                }
            }

            byte[] mask = null;
            if (masked) {
                mask = new byte[4];
                readFully(mask);
            }

            byte[] payload = new byte[(int) payloadLen];
            readFully(payload);

            if (masked && mask != null) {
                for (int i = 0; i < payload.length; i++) {
                    payload[i] ^= mask[i % 4];
                }
            }

            if (opcode == 0x8) { // close
                return null;
            } else if (opcode == 0x9) { // ping
                sendPong(payload);
                continue;
            } else if (opcode == 0xA) { // pong
                continue;
            } else if (opcode == 0x1 || opcode == 0x2) { // text or binary
                // For simplicity, assume single-frame messages (FIN set)
                // The board server sends small messages, so this is safe.
                return new String(payload, StandardCharsets.UTF_8);
            }
            // Continuation frames (0x0) not handled — board doesn't use them
        }
    }

    private void sendPong(byte[] payload) throws Exception {
        synchronized (mWriteLock) {
            ByteArrayOutputStream frame = new ByteArrayOutputStream();
            frame.write(0x8A); // FIN + pong opcode
            frame.write(0x80 | payload.length); // masked
            byte[] mask = new byte[4];
            new Random().nextBytes(mask);
            frame.write(mask);
            for (int i = 0; i < payload.length; i++) {
                frame.write(payload[i] ^ mask[i % 4]);
            }
            mOut.write(frame.toByteArray());
            mOut.flush();
        }
    }

    private void readFully(byte[] buf) throws Exception {
        int offset = 0;
        while (offset < buf.length) {
            int n = mIn.read(buf, offset, buf.length - offset);
            if (n == -1) throw new Exception("Connection closed");
            offset += n;
        }
    }

    private String generateKey() {
        byte[] key = new byte[16];
        new Random().nextBytes(key);
        return Base64.encodeToString(key, Base64.NO_WRAP);
    }

    private void closeQuietly() {
        try { if (mIn != null) mIn.close(); } catch (Exception e) {}
        try { if (mOut != null) mOut.close(); } catch (Exception e) {}
        try { if (mSocket != null) mSocket.close(); } catch (Exception e) {}
    }
}
