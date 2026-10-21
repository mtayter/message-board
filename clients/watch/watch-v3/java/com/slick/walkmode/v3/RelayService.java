package com.slick.walkmode.v3;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.media.AudioManager;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkRequest;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;
import android.view.KeyEvent;

import java.util.ArrayList;
import java.util.List;

/**
 * Foreground service that listens for the peer agent's reply after the user sends a
 * message. There is no manual start/stop: every successful send arms a
 * listen window (LISTEN_WINDOW_MS — 2 minutes, the battery constraint);
 * each reply spoken extends it by REPLY_GRACE_MS; then the service stops
 * itself. The peer never initiates, so listening only ever follows a send.
 *
 * This service owns the watch's single WSS connection (published in
 * WssHolder once connected): MainActivity sends through that same
 * connection and never opens its own, and this service's handler is the
 * only place replies are spoken.
 *
 * Session semantics: sinceId is persisted, so each reply is spoken at most
 * once. Replies older than 15 minutes are skipped (stale backlog after a
 * dead zone is not read aloud).
 */
public class RelayService extends Service {
    /** Broadcast when listen state changes (service started/stopped). */
    public static final String ACTION_STATE = "com.slick.walkmode.v3.STATE";
    /** Broadcast when a reply is spoken. EXTRA_TEXT carries the reply. */
    public static final String ACTION_REPLY = "com.slick.walkmode.v3.REPLY";
    public static final String EXTRA_TEXT = "text";

    private static final int NOTIF_ID = 1;
    private static final String CHANNEL = "slick_walk";
    private static final String PREFS = "walkmode_v3";
    private static final long STALE_SECS = 15 * 60;
    /** How long to listen for a reply after a send with no response.
     *  Two minutes is the battery constraint — not a quarter hour. */
    private static final long LISTEN_WINDOW_MS = 2 * 60 * 1000;
    /** Extra listen time after each reply (covers multi-part replies). */
    private static final long REPLY_GRACE_MS = 90 * 1000;

    /** True while the poll loop is active (same process, read by MainActivity). */
    public static volatile boolean isRunning = false;
    private static volatile long watchUntil = 0;

    private volatile boolean polling = false;
    private Thread worker;
    private PowerManager.WakeLock wakeLock;
    /** Reset the cached board token whenever the network changes, so a
     *  stale token never survives a network switch. */
    private ConnectivityManager.NetworkCallback networkCallback;

    /** Persisted set of already-spoken message IDs (dedup). */
    private static final String KEY_PLAYED_IDS = "played_ids";
    private static final int MAX_PLAYED_IDS = 300;
    private final java.util.Set<Long> playedIds = new java.util.HashSet<Long>();
    private boolean playedIdsLoaded = false;

    /**
     * Persistent played-ID dedup: returns true the first time an ID is
     * seen (the caller should speak it) and false for any redelivery —
     * reconnect backlog, service restart — so a reply is never spoken
     * twice. The set is persisted in prefs, trimmed to the newest
     * MAX_PLAYED_IDS. Messages without an ID (id &lt;= 0) are never deduped.
     */
    private boolean markPlayedIfNew(SharedPreferences prefs, long id) {
        if (id <= 0) return true;
        synchronized (playedIds) {
            if (!playedIdsLoaded) {
                String saved = prefs.getString(KEY_PLAYED_IDS, "");
                if (saved != null && !saved.isEmpty()) {
                    for (String part : saved.split(",")) {
                        try { playedIds.add(Long.parseLong(part.trim())); }
                        catch (NumberFormatException ignored) {}
                    }
                }
                playedIdsLoaded = true;
            }
            if (playedIds.contains(id)) return false;
            playedIds.add(id);
            java.util.List<Long> sorted = new java.util.ArrayList<Long>(playedIds);
            java.util.Collections.sort(sorted);
            if (sorted.size() > MAX_PLAYED_IDS) {
                sorted = sorted.subList(sorted.size() - MAX_PLAYED_IDS, sorted.size());
                playedIds.clear();
                playedIds.addAll(sorted);
            }
            StringBuilder sb = new StringBuilder();
            for (Long v : sorted) {
                if (sb.length() > 0) sb.append(',');
                sb.append(v.longValue());
            }
            prefs.edit().putString(KEY_PLAYED_IDS, sb.toString()).apply();
            return true;
        }
    }

    /**
     * Arm (or restart) the reply listen window and make sure the service
     * is running. MainActivity calls this right before sending through
     * the shared connection, so the service is up — and its connection
     * published in WssHolder — when the send goes out.
     */
    public static void extendWatch(Context c) {
        long until = System.currentTimeMillis() + LISTEN_WINDOW_MS;
        watchUntil = until;
        c.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putLong("watch_until", until)
                .putLong("watch_armed_at", System.currentTimeMillis()).apply();
        c.startForegroundService(new Intent(c, RelayService.class));
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.createNotificationChannel(new NotificationChannel(
                    CHANNEL, "Slick Walk", NotificationManager.IMPORTANCE_LOW));
        }
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "SlickWalk::relay");
        ConnectivityManager cm = getSystemService(ConnectivityManager.class);
        try {
            if (cm != null) {
                networkCallback = new ConnectivityManager.NetworkCallback() {
                    @Override public void onAvailable(Network network) {
                        BoardClient.resetToken();
                    }
                    @Override public void onLost(Network network) {
                        BoardClient.resetToken();
                    }
                };
                cm.registerDefaultNetworkCallback(networkCallback);
            }
        } catch (Exception ignored) {
            // Non-fatal: token reset on network change forces re-login.
            networkCallback = null;
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (isRunning) return START_STICKY; // extendWatch updated watchUntil
        long until = getSharedPreferences(PREFS, MODE_PRIVATE)
                .getLong("watch_until", 0);
        if (System.currentTimeMillis() > until) {
            stopSelf();
            return START_NOT_STICKY;
        }
        watchUntil = until;

        Notification notif = new Notification.Builder(this, CHANNEL)
                .setContentTitle("Slick Walk")
                .setContentText("Listening for " + Defaults.BOARD_PEER_USERNAME + "'s reply")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .build();
        startForeground(NOTIF_ID, notif,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);

        if (!wakeLock.isHeld()) wakeLock.acquire();
        isRunning = true;
        broadcastState();
        polling = true;
        final long startSince = getSharedPreferences(PREFS, MODE_PRIVATE)
                .getLong("since_id", 0);
        worker = new Thread(new Runnable() {
            @Override public void run() { pollLoop(startSince); }
        });
        worker.start();
        return START_STICKY;
    }

    /**
     * Main listen loop: WSS-only for push replies (no HTTP polling).
     * Each send arms a 2-minute WSS window; replies extend it by REPLY_GRACE_MS.
     */
    private void pollLoop(long startSince) {
        final SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        final String username = Defaults.BOARD_USERNAME;
        final String password = Defaults.BOARD_PASSWORD;
        long sinceId = startSince;

        // One shared WSS session at a time for the listen window
        // (watchUntil). If WSS fails or drops, retry until the window expires.
        while (polling && System.currentTimeMillis() <= watchUntil) {
            // WSS-only (no HTTP). If WSS fails, wait and retry.
            boolean wssOk = wssListen(username, password, sinceId, prefs);
            if (!wssOk) {
                // WSS failed; wait before retry
                try { Thread.sleep(4000); } catch (InterruptedException ie) { break; }
            } else {
                // WSS completed (watchUntil reached or error); update sinceId
                sinceId = prefs.getLong("since_id", sinceId);
                // If watchUntil not yet reached, loop to reconnect
                if (System.currentTimeMillis() < watchUntil) {
                    try { Thread.sleep(2000); } catch (InterruptedException ie) { break; }
                }
            }
        }
        stopSelf();
    }

    /**
     * WSS push listener: opens the watch's single shared WSS connection
     * for this listen session and publishes it in WssHolder once the hello
     * handshake completes. This service owns the connection — MainActivity
     * sends through the holder reference and never opens its own — and
     * this handler is the only place replies are spoken. Receives push
     * messages until the listen window (watchUntil) expires or the
     * connection drops. Returns true if the session connected, false if
     * it failed to connect.
     */
    private boolean wssListen(String username, String password, long startSince,
                              SharedPreferences prefs) {
        final long[] sinceId = {startSince};

        String token = BoardClient.getToken(username, password);
        if (token == null) {
            android.util.Log.w("SlickWalk", "WSS: no token");
            return false;
        }

        // Split-horizon DNS (if you run one) handles LAN vs WAN: the
        // board host resolves to your LAN IP on Wi-Fi, public IP
        // elsewhere. No LAN probe needed.
        String host = Defaults.BOARD_HOST;
        int port = Defaults.BOARD_PORT;
        boolean useTls = Defaults.BOARD_USE_TLS;
        String path = Defaults.BOARD_WS_PATH;

        BoardWssClient.MessageHandler handler = new BoardWssClient.MessageHandler() {
            @Override public void onMessage(org.json.JSONObject msg) {
                try {
                    long id = msg.optLong("id", 0);
                    String from = msg.optString("from", "");
                    String to = msg.optString("to", "");
                    String body = msg.optString("body", "");
                    long timestamp = msg.optLong("timestamp", 0);

                    if (id > sinceId[0]) {
                        sinceId[0] = id;
                        prefs.edit().putLong("since_id", id).apply();
                    }
                    if (!Defaults.BOARD_PEER_USERNAME.equals(from)) return;
                    // Only speak messages addressed to this watch user.
                    // Messages from the peer to others are not for us.
                    if (!Defaults.BOARD_USERNAME.equals(to)) return;
                    long nowSecs = System.currentTimeMillis() / 1000;
                    if (timestamp > 0 && nowSecs - timestamp > STALE_SECS) return;
                    String spoken = body.trim();
                    if (spoken.isEmpty()) return;
                    // Persistent dedup: never speak the same reply twice,
                    // even if a reconnect redelivers it from the backlog.
                    if (!markPlayedIfNew(prefs, id)) return;

                    prefs.edit()
                            .putString("last_reply", spoken)
                            .putLong("last_reply_ts", System.currentTimeMillis())
                            .apply();
                    Intent b = new Intent(ACTION_REPLY);
                    b.setPackage(getPackageName());
                    b.putExtra(EXTRA_TEXT, spoken);
                    sendBroadcast(b);

                    dispatchMediaKey(KeyEvent.KEYCODE_MEDIA_PAUSE);
                    TtsHelper tts = TtsHelper.get(RelayService.this);
                    tts.speak(spoken, new Runnable() {
                        @Override public void run() {
                            dispatchMediaKey(KeyEvent.KEYCODE_MEDIA_PLAY);
                        }
                    });

                    // Extend the listen window on reply (90s grace,
                    // battery-friendly). watchUntil is the single window
                    // source of truth (static + persisted).
                    long newUntil = System.currentTimeMillis() + REPLY_GRACE_MS;
                    if (newUntil > watchUntil) {
                        watchUntil = newUntil;
                        prefs.edit().putLong("watch_until", newUntil).apply();
                    }
                } catch (Exception e) {
                    android.util.Log.w("SlickWalk", "WSS handler: " + e.getMessage());
                }
            }
            @Override public void onClose() {}
        };

        BoardWssClient client = new BoardWssClient(host, port, useTls, path,
                token, sinceId[0], handler);
        final BoardWssClient wssClient = client;
        final boolean[] connected = {false};

        Thread wssThread = new Thread(new Runnable() {
            @Override public void run() {
                try {
                    wssClient.connectAndHello();
                    connected[0] = true;
                    // Publish the shared connection: from here on, sends
                    // go through this same client via WssHolder.
                    WssHolder.set(wssClient);
                    wssClient.startReading();
                } catch (Exception e) {
                    android.util.Log.w("SlickWalk", "WSS session: " + e.getMessage());
                } finally {
                    if (WssHolder.get() == wssClient) WssHolder.clear();
                }
            }
        });
        wssThread.start();

        while (wssThread.isAlive() && polling
                && System.currentTimeMillis() <= watchUntil) {
            try { Thread.sleep(500); } catch (InterruptedException e) { break; }
        }

        wssClient.stop();
        try { wssThread.join(2000); } catch (InterruptedException e) {}
        return connected[0];
    }

    private void broadcastState() {
        Intent b = new Intent(ACTION_STATE);
        b.setPackage(getPackageName());
        sendBroadcast(b);
    }

    /** Dispatch a media key (pause/resume around replies) to the active player. */
    private void dispatchMediaKey(int key) {
        try {
            AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            if (am == null) return;
            long now = SystemClock.uptimeMillis();
            am.dispatchMediaKeyEvent(new KeyEvent(now, now, KeyEvent.ACTION_DOWN, key, 0));
            am.dispatchMediaKeyEvent(new KeyEvent(now, now, KeyEvent.ACTION_UP, key, 0));
        } catch (Exception e) {
            android.util.Log.i("SlickWalk", "dispatchMediaKey: " + e);
        }
    }

    @Override
    public void onDestroy() {
        polling = false;
        isRunning = false;
        if (worker != null) worker.interrupt();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        ConnectivityManager cm = getSystemService(ConnectivityManager.class);
        if (cm != null && networkCallback != null) {
            try { cm.unregisterNetworkCallback(networkCallback); }
            catch (Exception ignored) { }
            networkCallback = null;
        }
        broadcastState();
        super.onDestroy();
    }
}
