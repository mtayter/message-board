package com.slick.walkmode.v3;

/**
 * Holds the active WSS connection shared between MainActivity (send)
 * and RelayService (receive). The connection is opened on talk-button
 * press and kept open for the reply window.
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
