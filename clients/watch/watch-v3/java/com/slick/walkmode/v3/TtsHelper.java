package com.slick.walkmode.v3;

import android.content.Context;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.speech.tts.Voice;
import android.util.Log;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedQueue;

/** Singleton TextToSpeech wrapper; queues speech until the engine is ready. */
public class TtsHelper {
    private static final String TAG = "SlickWalk";
    private static final String PREFS = "walkmode_v3";
    private static final String KEY_VOICE = "tts_voice_override";
    /** Default voice (voice 4 of 7 from the on-device tour). */
    private static final String DEFAULT_VOICE_NAME = "en-us-x-iom-local";
    private static TtsHelper instance;
    private final Context appCtx;
    private TextToSpeech tts;
    private volatile boolean ready = false;
    private final ConcurrentLinkedQueue<String> pending = new ConcurrentLinkedQueue<>();
    // Voice-tour state ("test voices" command).
    private volatile boolean pendingTour = false;
    private volatile List<Voice> tourVoices = null;
    private volatile int tourIndex = 0;
    /** One-shot callbacks run when a specific utterance finishes (or errors). */
    private final Map<String, Runnable> doneCallbacks = new ConcurrentHashMap<>();

    public static synchronized TtsHelper get(Context ctx) {
        if (instance == null) {
            instance = new TtsHelper(ctx.getApplicationContext());
        }
        return instance;
    }

    private TtsHelper(Context ctx) {
        appCtx = ctx.getApplicationContext();
        tts = new TextToSpeech(ctx, status -> {
            if (status == TextToSpeech.SUCCESS) {
                tts.setLanguage(Locale.US);
                selectInitialVoice();
                ready = true;
                tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                    @Override public void onStart(String id) {}
                    @Override public void onDone(String id) { onUtteranceDone(id); }
                    @Override public void onError(String id) { onUtteranceDone(id); }
                });
                String s;
                while ((s = pending.poll()) != null) {
                    speakNow(s);
                }
                if (pendingTour) {
                    pendingTour = false;
                    startVoiceTour();
                }
            }
        });
    }

    public void speak(String text) {
        speak(text, null);
    }

    /**
     * Speak, running onDone (on a binder thread) when this utterance
     * finishes or errors. If the engine isn't ready the text is queued and
     * the callback is dropped (replies only happen once the app is up).
     */
    public void speak(String text, Runnable onDone) {
        if (text == null || text.trim().isEmpty()) return;
        if (!ready || tts == null) {
            pending.offer(text);
            return;
        }
        String id = UUID.randomUUID().toString();
        if (onDone != null) doneCallbacks.put(id, onDone);
        tts.speak(text, TextToSpeech.QUEUE_ADD, null, id);
    }

    /** Routes utterance completion: voice-tour chaining or one-shot callbacks. */
    private void onUtteranceDone(String id) {
        if (id == null) return;
        if (id.startsWith("tour-")) {
            speakNextTourVoice();
            return;
        }
        Runnable r = doneCallbacks.remove(id);
        if (r != null) {
            try {
                r.run();
            } catch (Exception e) {
                Log.i(TAG, "doneCallback: " + e);
            }
        }
    }

    private void speakNow(String text) {
        speak(text, null);
    }

    public void stop() {
        pending.clear();
        pendingTour = false;
        tourVoices = null;
        if (tts != null) tts.stop();
        // A stopped utterance still counts as done (e.g. "quiet" during a
        // reply resumes the music the reply had paused).
        for (Runnable r : doneCallbacks.values()) {
            try {
                r.run();
            } catch (Exception e) {
                Log.i(TAG, "stop doneCallback: " + e);
            }
        }
        doneCallbacks.clear();
    }

    /** Lowercase alphanumeric only: "en us x t p f local" matches "en-us-x-tpf-local". */
    private static String normalizeVoiceName(String s) {
        return s.toLowerCase(Locale.US).replaceAll("[^a-z0-9]", "");
    }

    /**
     * Set the TTS voice by name, forgiving of speech-recognition mangling of
     * the code (spaces/hyphens ignored, case-insensitive). The choice is
     * persisted and survives restarts. Returns the matched voice's real name,
     * or null if nothing matched.
     */
    public String setVoiceByName(String rawName) {
        if (rawName == null || rawName.trim().isEmpty() || tts == null) return null;
        String trimmed = rawName.trim();
        if (!ready) {
            // Engine still warming up: stash the request; init applies it via
            // applyVoiceOverride(), and the queued confirmation is then spoken
            // in the new voice.
            appCtx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                    .edit().putString(KEY_VOICE, trimmed).apply();
            Log.i(TAG, "setVoiceByName: engine not ready, stashed " + trimmed);
            return trimmed;
        }
        String want = normalizeVoiceName(trimmed);
        Voice match = null;
        try {
            Set<Voice> voices = tts.getVoices();
            if (voices != null) {
                for (Voice v : voices) {
                    if (normalizeVoiceName(v.getName()).equals(want)) {
                        match = v;
                        break;
                    }
                }
            }
        } catch (Exception e) {
            Log.i(TAG, "setVoiceByName: exception " + e);
        }
        if (match == null) {
            Log.i(TAG, "setVoiceByName: no match for " + trimmed);
            return null;
        }
        appCtx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().putString(KEY_VOICE, match.getName()).apply();
        tts.setVoice(match);
        Log.i(TAG, "setVoiceByName: selected " + match.getName());
        return match.getName();
    }

    /**
     * "test voices": speak one sample in each installed en-US on-device
     * voice, in name order. Each sample is numbered and speaks its own
     * voice code, so the listener can report back "I liked number 3".
     * Chained via utterance callbacks so each sample is fully synthesized
     * in its own voice before the next voice is selected. Saying "quiet"
     * mid-tour stops it (TtsHelper.stop drops the queued utterances).
     */
    public void tourVoices() {
        if (tts == null) return;
        if (!ready) {
            pendingTour = true;
            return;
        }
        startVoiceTour();
    }

    private void startVoiceTour() {
        List<Voice> found = new ArrayList<>();
        try {
            Set<Voice> voices = tts.getVoices();
            if (voices != null) {
                for (Voice v : voices) {
                    Locale loc = v.getLocale();
                    String n = v.getName().toLowerCase(Locale.US);
                    if (loc != null
                            && "en".equals(loc.getLanguage())
                            && "US".equals(loc.getCountry())
                            && n.endsWith("-local")
                            && !v.isNetworkConnectionRequired()) {
                        found.add(v);
                    }
                }
            }
        } catch (Exception e) {
            Log.i(TAG, "startVoiceTour: exception " + e);
        }
        Collections.sort(found, (a, b) -> a.getName().compareTo(b.getName()));
        Log.i(TAG, "startVoiceTour: " + found.size() + " voices");
        tourVoices = found;
        tourIndex = 0;
        speakNextTourVoice();
    }

    private void speakNextTourVoice() {
        List<Voice> list = tourVoices;
        if (list == null || tts == null) return;
        if (tourIndex >= list.size()) {
            tourVoices = null;
            tts.speak("That's all of them. Tell me which number you liked.",
                    TextToSpeech.QUEUE_ADD, null, "tour-done");
            return;
        }
        Voice v = list.get(tourIndex);
        tts.setVoice(v);
        String text = "Voice " + (tourIndex + 1) + " of " + list.size()
                + ": " + spellCode(v.getName())
                + ". Hey, this is what I sound like.";
        tts.speak(text, TextToSpeech.QUEUE_ADD, null, "tour-" + tourIndex);
        tourIndex++;
    }

    /** "en-us-x-tpf-local" -> "T P F" so the code is speakable. */
    private static String spellCode(String name) {
        String[] parts = name.toLowerCase(Locale.US).split("-");
        for (int i = 0; i < parts.length - 1; i++) {
            if (parts[i].equals("x")) {
                StringBuilder sb = new StringBuilder();
                for (char c : parts[i + 1].toCharArray()) {
                    if (sb.length() > 0) sb.append(' ');
                    sb.append(Character.toUpperCase(c));
                }
                return sb.toString();
            }
        }
        return name;
    }
    /**
     * Initial voice: the persisted "voice <name>" choice wins; otherwise
     * the tour pick; otherwise the (currently match-nothing) heuristic.
     */
    private void selectInitialVoice() {
        String saved = appCtx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(KEY_VOICE, null);
        if (saved != null && applyVoiceByName(saved)) return;
        if (applyVoiceByName(DEFAULT_VOICE_NAME)) return;
        selectMaleVoice();
    }

    /** Re-apply a voice choice when the engine (re)initializes. Returns true on match. */
    private boolean applyVoiceByName(String name) {
        if (name == null || tts == null) return false;
        try {
            Set<Voice> voices = tts.getVoices();
            if (voices == null) return false;
            String want = normalizeVoiceName(name);
            for (Voice v : voices) {
                if (normalizeVoiceName(v.getName()).equals(want)) {
                    tts.setVoice(v);
                    Log.i(TAG, "applyVoiceByName: " + v.getName());
                    return true;
                }
            }
            Log.i(TAG, "applyVoiceByName: no match for " + name);
        } catch (Exception e) {
            Log.i(TAG, "applyVoiceByName: exception " + e);
        }
        return false;
    }

    /** Re-apply the persisted voice choice when the engine (re)initializes. */
    private void applyVoiceOverride() {
        String name = appCtx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(KEY_VOICE, null);
        applyVoiceByName(name);
    }

    /**
     * Pick an English male voice when the engine has one (API 34 exposes no
     * gender field, so match on the voice name; "female" contains "male",
     * hence the exclusion). Prefers on-device voices to avoid network
     * latency on LTE. Falls back to the default voice if none match.
     */
    private void selectMaleVoice() {
        Log.i(TAG, "selectMaleVoice: called");
        try {
            Set<Voice> voices = tts.getVoices();
            Log.i(TAG, "selectMaleVoice: voices="
                    + (voices == null ? "null" : String.valueOf(voices.size())));
            if (voices == null) return;
            for (Voice v : voices) {
                Locale loc = v.getLocale();
                Log.i(TAG, "tts voice name=" + v.getName()
                        + " locale=" + (loc == null ? "null" : loc.toLanguageTag())
                        + " network=" + v.isNetworkConnectionRequired()
                        + " quality=" + v.getQuality()
                        + " features=" + v.getFeatures());
            }
            Voice best = null;
            for (Voice v : voices) {
                Locale loc = v.getLocale();
                if (loc == null || !"en".equals(loc.getLanguage())) continue;
                String n = v.getName().toLowerCase(Locale.US);
                if (!n.contains("male") || n.contains("female")) continue;
                if (best == null
                        || (!v.isNetworkConnectionRequired()
                            && best.isNetworkConnectionRequired())) {
                    best = v;
                }
            }
            if (best != null) tts.setVoice(best);
            Log.i(TAG, "selectMaleVoice: selected="
                    + (best == null ? "<default>" : best.getName()));
        } catch (Exception e) {
            Log.i(TAG, "selectMaleVoice: exception " + e);
        }
    }
}
