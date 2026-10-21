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
                // Single shared WSS connection, owned by RelayService:
                // arm the service first — it opens the connection and
                // publishes it in WssHolder — then send through that same
                // connection. MainActivity never opens its own connection
                // and never speaks replies; the service's handler is the
                // only speaker. (Previously this code opened a second
                // connection here with its own speak handler, so every
                // reply was spoken twice.)
                RelayService.extendWatch(MainActivity.this);
                BoardWssClient wss = waitForSharedWss();
                if (wss == null) {
                    setStatus("You: " + text + "\nCouldn't reach the server via WSS.");
                    return;
                }

                // Send via the shared WSS connection — prefix marks it as
                // from the watch so the agent knows to send a quick "on it"
                // for longer responses. Location is the latest async GPS
                // fix (may be null if no fix yet). RecordedAt is when the
                // user lifted their finger (for latency).
                org.json.JSONObject location = getLatestLocation();
                if (!wss.sendMessage(Defaults.BOARD_PEER_USERNAME, "(from watch) " + text, null,
                        location, recordingEndedAt)) {
                    setStatus("You: " + text + "\nWSS send failed.");
                    return;
                }

                setListenState("Sent — listening for " + Defaults.BOARD_PEER_USERNAME + "…");
                holdScreenOn();
                // The service keeps listening on the shared connection
                // (2-min window, +90s per reply) and speaks any reply;
                // ACTION_STATE / ACTION_REPLY broadcasts drive the UI.
            }
        }).start();
    }

    /**
     * Wait for RelayService to publish the shared WSS connection in
     * WssHolder (it appears once the hello handshake completes). Bounded:
     * if the service can't connect, the send fails instead of hanging.
     */
    private BoardWssClient waitForSharedWss() {
        long deadline = System.currentTimeMillis() + 15 * 1000;
        while (System.currentTimeMillis() < deadline) {
            BoardWssClient c = WssHolder.get();
            if (c != null) return c;
            try { Thread.sleep(100); } catch (InterruptedException e) { return null; }
        }
        return WssHolder.get();
    }

    private void setListenState(final String s) {
        runOnUiThread(new Runnable() {
            @Override public void run() { listenState.setText(s); }
        });
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
