package com.slick.walkmode.v3;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.TextView;

import java.util.ArrayList;

/**
 * Slick Walk v3: one screen, no scrolling, no setup. Hold-to-talk sends a
 * message to your agent through the message board over WSS (host and
 * credentials come from the generated Defaults.java — see install.sh);
 * the watch then listens for the reply automatically and speaks it aloud.
 * Failures are reported on the status line.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "walkmode_v3";
    /** Singleton WSS connection — ensures only one is open at a time. */
    private static BoardWssClient activeWss = null;

    private SpeechRecognizer recognizer;
    private boolean listening = false;
    private TextView status;
    private TextView listenState;
    /** Generation counter so a stale screen-on timeout can't clear a newer hold. */
    private int screenOnGen = 0;
    /** Latest GPS fix from background tracking. Updated async, never blocks send. */
    private volatile android.location.Location latestLocation = null;
    /** Wall-clock time (ms) when the user lifted their finger (recording ended). */
    private volatile long recordingEndedAt = 0;
    private android.location.LocationManager locationManager = null;
    private android.location.LocationListener locationListener = null;

    private final BroadcastReceiver receiver = new BroadcastReceiver() {
        @Override public void onReceive(Context c, Intent i) {
            String a = i.getAction();
            if (RelayService.ACTION_REPLY.equals(a)) {
                setStatus(Defaults.BOARD_PEER_USERNAME + ": " + i.getStringExtra(RelayService.EXTRA_TEXT));
                // First reply is in: the screen can sleep again. The service
                // keeps the short grace window for follow-ups in background.
                screenOnGen++;
                getWindow().clearFlags(
                        WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            } else if (RelayService.ACTION_STATE.equals(a)) {
                updateListenState();
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        status = findViewById(R.id.status);
        listenState = findViewById(R.id.listen_state);

        Button talk = findViewById(R.id.talk);
        talk.setOnTouchListener(new View.OnTouchListener() {
            @Override
            public boolean onTouch(View v, MotionEvent e) {
                if (e.getAction() == MotionEvent.ACTION_DOWN) {
                    startListening();
                } else if (e.getAction() == MotionEvent.ACTION_UP
                        || e.getAction() == MotionEvent.ACTION_CANCEL) {
                    stopListening();
                }
                return true;
            }
        });

        findViewById(R.id.stop_tts).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                TtsHelper.get(MainActivity.this).stop();
                setStatus("Voice stopped.");
            }
        });

        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1);
        }
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION}, 2);
        }
        // Warm up TTS engine.
        TtsHelper.get(this);
        // Start GPS tracking early so a fix is ready by send time.
        // Runs in background, never blocks sending.
        startLocationUpdates();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == 2 && grantResults.length > 0
                && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            // Location permission granted — start GPS tracking now.
            startLocationUpdates();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        IntentFilter f = new IntentFilter();
        f.addAction(RelayService.ACTION_REPLY);
        f.addAction(RelayService.ACTION_STATE);
        // Android 14+ requires an exported/not-exported flag; our
        // broadcasts are intra-app (setPackage), so not exported.
        if (Build.VERSION.SDK_INT >= 34) {
            registerReceiver(receiver, f, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(receiver, f);
        }
        updateListenState();
        // Show the last reply if one arrived while the screen was off.
        String last = getSharedPreferences(PREFS, MODE_PRIVATE)
                .getString("last_reply", "");
        if (!last.isEmpty() && status.getText().length() == 0) {
            status.setText(Defaults.BOARD_PEER_USERNAME + ": " + last);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        try {
            unregisterReceiver(receiver);
        } catch (IllegalArgumentException ignored) {}
    }

    @Override
    protected void onDestroy() {
        stopLocationUpdates();
        if (recognizer != null) {
            recognizer.destroy();
            recognizer = null;
        }
        super.onDestroy();
    }

    private void updateListenState() {
        listenState.setText(RelayService.isRunning ? "Listening for " + Defaults.BOARD_PEER_USERNAME + "..." : "");
    }

    private void startListening() {
        if (listening) return;
        // Ensure GPS is tracking — a fix may be ready by send time.
        startLocationUpdates();
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            setStatus("Mic permission needed.");
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1);
            return;
        }
        if (SpeechRecognizer.isRecognitionAvailable(this)) {
            recognizer = SpeechRecognizer.createSpeechRecognizer(this);
            recognizer.setRecognitionListener(new RecognitionListener() {
                @Override public void onReadyForSpeech(Bundle p) { setStatus("Listening..."); }
                @Override public void onBeginningOfSpeech() {}
                @Override public void onRmsChanged(float v) {}
                @Override public void onBufferReceived(byte[] b) {}
                @Override public void onEndOfSpeech() { setStatus("Heard you, sending..."); }
                @Override public void onError(int e) {
                    setStatus("Didn't catch that (" + e + "). Try again.");
                    listening = false;
                }
                @Override public void onResults(Bundle r) {
                    listening = false;
                    ArrayList<String> res = r.getStringArrayList(
                            SpeechRecognizer.RESULTS_RECOGNITION);
                    if (res != null && !res.isEmpty()) {
                        final String text = res.get(0);
                        setStatus("You: " + text);
                        handleTranscript(text);
                    } else {
                        setStatus("Didn't catch that. Try again.");
                    }
                }
                @Override public void onPartialResults(Bundle p) {}
                @Override public void onEvent(int t, Bundle p) {}
            });
            Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
            i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                    RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
            i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
            recognizer.startListening(i);
            listening = true;
        } else {
            setStatus("Speech recognition not available on this watch.");
        }
    }

    private void stopListening() {
        if (listening && recognizer != null) {
            recognizer.stopListening();
            listening = false;
            // Timestamp for latency measurement (recording end → server receipt).
            recordingEndedAt = System.currentTimeMillis();
        }
    }

    /**
     * Local commands (music, quiet) run instantly on the watch; everything
     * else is sent to your agent via the board, which arms the reply listener.
     */
    private void handleTranscript(final String text) {
        String local = LocalCommands.handle(text, this);
        if (local != null) {
            TtsHelper.get(this).speak(local);
            setStatus(local);
            if (LocalCommands.VOICE_TOUR_INTRO.equals(local)) {
                // Queued after the intro, so the intro is heard first.
                TtsHelper.get(this).tourVoices();
            }
            return;
        }
        new Thread(new Runnable() {
            @Override public void run() {
                // WSS-only: open one connection, send, then listen for replies
                // on the SAME connection. No HTTP, no sharing, no handoff.
                // Close any existing connection first (prevents duplicates).
                synchronized (MainActivity.class) {
                    if (activeWss != null) {
                        try { activeWss.stop(); } catch (Exception e) {}
                        activeWss = null;
                    }
                }
                BoardWssClient wss = openWssConnection();
                if (wss == null) {
                    setStatus("You: " + text + "\nCouldn't reach the server via WSS.");
                    return;
                }
                synchronized (MainActivity.class) {
                    activeWss = wss;
                }

                // Send via WSS — prefix marks it as from the watch so the
                // agent knows to send a quick "on it" for longer responses.
                // Location is latest async GPS fix (may be null if no fix yet).
                // RecordedAt is when the user lifted their finger (for latency).
                org.json.JSONObject location = getLatestLocation();
                if (!wss.sendMessage(Defaults.BOARD_PEER_USERNAME, "(from watch) " + text, null,
                        location, recordingEndedAt)) {
                    wss.stop();
                    synchronized (MainActivity.class) {
                        if (activeWss == wss) activeWss = null;
                    }
                    setStatus("You: " + text + "\nWSS send failed.");
                    return;
                }

                setListenState("Sent — listening for " + Defaults.BOARD_PEER_USERNAME + "…");
                holdScreenOn();
                // Arm the background listener: keeps listening after the
                // foreground window closes, and drives the "Listening..." UI.
                RelayService.extendWatch(MainActivity.this);

                // Listen for replies on the SAME WSS connection (2 min max).
                // Each reply extends by 90s. This avoids any handoff race.
                long windowEnd = System.currentTimeMillis() + 2 * 60 * 1000;
                final long[] sinceId = {getSinceId()};

                wss.setHandler(new BoardWssClient.MessageHandler() {
                    // In-memory dedup for this session (handles backlog+push duplicates)
                    final java.util.Set<Long> playedIds = new java.util.HashSet<Long>();
                    @Override public void onMessage(org.json.JSONObject msg) {
                        try {
                            long id = msg.optLong("id", 0);
                            String from = msg.optString("from", "");
                            String body = msg.optString("body", "");
                            // Update since (for next hello's backlog)
                            if (id > sinceId[0]) {
                                sinceId[0] = id;
                                saveSinceId(id);
                            }
                            if (!Defaults.BOARD_PEER_USERNAME.equals(from)) return;
                            // Skip if already played in this session
                            if (!playedIds.add(id)) return;
                            String spoken = body.trim();
                            if (spoken.isEmpty()) return;
                            TtsHelper.get(MainActivity.this).speak(spoken);
                        } catch (Exception e) {
                            android.util.Log.w("SlickWalk", "WSS handler: " + e.getMessage());
                        }
                    }
                    @Override public void onClose() {}
                });

                // Blocking read loop (2 min, extendable on reply)
                // Note: This is simplified; a full impl would extend windowEnd on reply.
                // For now, 2 min fixed window.
                Thread reader = new Thread(new Runnable() {
                    @Override public void run() { wss.startReading(); }
                });
                reader.start();
                while (reader.isAlive() && System.currentTimeMillis() < windowEnd) {
                    try { Thread.sleep(500); } catch (InterruptedException e) { break; }
                }
                wss.stop();
                synchronized (MainActivity.class) {
                    if (activeWss == wss) activeWss = null;
                }
                try { reader.join(2000); } catch (InterruptedException e) {}
                setListenState("Done listening.");
            }

            private long getSinceId() {
                return MainActivity.this.getSharedPreferences("walkmode_v3", MODE_PRIVATE)
                        .getLong("since_id", 0);
            }
            private void saveSinceId(long id) {
                MainActivity.this.getSharedPreferences("walkmode_v3", MODE_PRIVATE)
                        .edit().putLong("since_id", id).apply();
            }
        }).start();
    }

    private void setListenState(final String s) {
        runOnUiThread(new Runnable() {
            @Override public void run() { listenState.setText(s); }
        });
    }

    /**
     * Open a WSS connection to the board. Returns null on failure.
     * The caller is responsible for storing it in WssHolder and closing it.
     */
    private BoardWssClient openWssConnection() {
        try {
            String username = Defaults.BOARD_USERNAME;
            String password = Defaults.BOARD_PASSWORD;
            String token = BoardClient.getToken(username, password);
            if (token == null) return null;

            long sinceId = getSharedPreferences("walkmode_v3", MODE_PRIVATE)
                    .getLong("since_id", 0);

            // Split-horizon DNS (if you run one) handles LAN vs WAN: the
            // board host resolves to your LAN IP on Wi-Fi, public IP
            // elsewhere. No LAN probe needed.
            String host = Defaults.BOARD_HOST;
            int port = Defaults.BOARD_PORT;
            boolean useTls = Defaults.BOARD_USE_TLS;
            String path = Defaults.BOARD_WS_PATH;

            // No handler yet; RelayService will set it when it takes over.
            // For now, use a no-op handler.
            BoardWssClient.MessageHandler noop = new BoardWssClient.MessageHandler() {
                @Override public void onMessage(org.json.JSONObject msg) {}
                @Override public void onClose() {}
            };

            BoardWssClient client = new BoardWssClient(host, port, useTls, path,
                    token, sinceId, noop);
            client.connectAndHello();
            return client;
        } catch (Exception e) {
            android.util.Log.w("SlickWalk", "WSS open failed: " + e.getMessage());
            return null;
        }
    }



    private void setStatus(final String s) {
        runOnUiThread(new Runnable() {
            @Override public void run() { status.setText(s); }
        });
    }

    /** Start background GPS tracking. Safe to call repeatedly (no-op if running).
     *  Keeps trying for a fix so one is ready by send time. Never blocks. */
    private void startLocationUpdates() {
        try {
            if (locationManager == null) {
                locationManager = (android.location.LocationManager)
                        getSystemService(Context.LOCATION_SERVICE);
            }
            if (locationManager == null) return;
            if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                    != PackageManager.PERMISSION_GRANTED) {
                return;
            }
            if (locationListener != null) return; // already running
            locationListener = new android.location.LocationListener() {
                @Override public void onLocationChanged(android.location.Location loc) {
                    latestLocation = loc;
                }
                @Override public void onStatusChanged(String p, int s, android.os.Bundle b) {}
                @Override public void onProviderEnabled(String p) {}
                @Override public void onProviderDisabled(String p) {}
            };
            // Seed with last known fix if available.
            try {
                android.location.Location last = locationManager.getLastKnownLocation(
                        android.location.LocationManager.GPS_PROVIDER);
                if (last != null) latestLocation = last;
            } catch (Exception e) {}
            locationManager.requestLocationUpdates(
                    android.location.LocationManager.GPS_PROVIDER,
                    5000, 0, locationListener);
        } catch (Exception e) {
            android.util.Log.w("SlickWalk", "startLocation failed: " + e.getMessage());
        }
    }

    private void stopLocationUpdates() {
        try {
            if (locationManager != null && locationListener != null) {
                locationManager.removeUpdates(locationListener);
            }
        } catch (Exception e) {}
        locationListener = null;
    }

    /** Get the latest GPS fix as metadata. Returns immediately — never blocks.
     *  Returns null if no fix yet (send proceeds without location). */
    private org.json.JSONObject getLatestLocation() {
        try {
            android.location.Location loc = latestLocation;
            if (loc == null) return null;
            org.json.JSONObject obj = new org.json.JSONObject();
            obj.put("lat", loc.getLatitude());
            obj.put("lon", loc.getLongitude());
            return obj;
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * Keep the screen on while waiting for the first reply (the user just
     * spoke and is watching for the answer). Cleared on the first reply or
     * after 60 s, whichever comes first; the service keeps listening in the
     * background either way.
     */
    private void holdScreenOn() {
        final int gen = ++screenOnGen;
        runOnUiThread(new Runnable() {
            @Override public void run() {
                getWindow().addFlags(
                        WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                status.postDelayed(new Runnable() {
                    @Override public void run() {
                        if (gen == screenOnGen) {
                            getWindow().clearFlags(
                                    WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                        }
                    }
                }, 60 * 1000);
            }
        });
    }
}
