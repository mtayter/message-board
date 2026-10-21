package com.slick.walkmode.v3;

/**
 * Holds the watch's single shared WSS connection. RelayService owns it:
 * it opens the connection, publishes it here once the hello handshake
 * completes, reads on it, and clears it when the listen session ends.
 * MainActivity only sends through the published client — it never opens
 * a connection of its own. There is only ever one connection: a second
 * one would double-deliver (and double-speak) every reply.
 */
public class WssHolder {
    private static volatile BoardWssClient sClient = null;
    private static final Object sLock = new Object();

    public static void set(BoardWssClient client) {
        synchronized (sLock) {
            // Close old if replacing
            if (sClient != null && sClient != client) {
                try { sClient.stop(); } catch (Exception e) {}
            }
            sClient = client;
        }
    }

    public static BoardWssClient get() {
        synchronized (sLock) {
            return sClient;
        }
    }

    public static void clear() {
        synchronized (sLock) {
            if (sClient != null) {
                try { sClient.stop(); } catch (Exception e) {}
                sClient = null;
            }
        }
    }
}
