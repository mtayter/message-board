package com.slick.walkmode.v3;

import android.content.Context;
import android.media.AudioManager;
import android.os.SystemClock;
import android.view.KeyEvent;

/**
 * Handles voice commands locally on the watch, with zero round trip.
 *
 * Music commands match on the FIRST WORD of the transcript (so "pause the
 * music" and "pause" both work). Quiet commands require an EXACT match (so
 * "quiet down the trail is loud" is not swallowed). The voice command takes
 * the rest of the utterance as a voice name ("voice en-us-x-tpf-local");
 * matching ignores case, spaces, and hyphens, since speech recognition
 * mangles the codes. "test voices" (exact match) tours all installed
 * en-US on-device voices, speaking one numbered sample in each.
 *
 * Returns a short spoken confirmation, or null if the text should be sent
 * to the server instead.
 */
public class LocalCommands {

    /**
     * Intro spoken when the voice tour starts. MainActivity watches for this
     * exact string and kicks off TtsHelper.tourVoices() after speaking it,
     * so the intro is heard before the first sample.
     */
    public static final String VOICE_TOUR_INTRO =
            "Trying all seven voices. Listen to each one.";

    /**
     * @return confirmation text to speak, or null to send the text to the peer agent.
     */
    public static String handle(String text, Context ctx) {
        if (text == null) return null;
        String t = text.trim().toLowerCase();
        if (t.isEmpty()) return null;

        // Quiet commands: exact match only.
        if (t.equals("stop talking") || t.equals("shut up") || t.equals("quiet")) {
            TtsHelper.get(ctx).stop();
            return "Okay, quiet.";
        }

        // Voice tour: exact match only.
        if (t.equals("test voices")) {
            return VOICE_TOUR_INTRO;
        }

        // Music commands: first word match.
        String first = t.split("\\s+")[0];
        if (first.equals("voice")) {
            String arg = t.substring("voice".length()).trim();
            if (arg.isEmpty()) {
                return "Tell me which voice to try, like: voice en-us-x-tpf-local.";
            }
            String matched = TtsHelper.get(ctx).setVoiceByName(arg);
            if (matched != null) {
                // Spoken by MainActivity in the NEW voice: the confirmation
                // doubles as the sample.
                return "Voice changed. This is how I sound now.";
            }
            return "I don't have a voice called " + arg + ".";
        }
        int key = -1;
        String confirm = null;
        if (first.equals("pause")) {
            key = KeyEvent.KEYCODE_MEDIA_PAUSE;
            confirm = "Paused.";
        } else if (first.equals("play") || first.equals("resume")) {
            key = KeyEvent.KEYCODE_MEDIA_PLAY;
            confirm = "Playing.";
        } else if (first.equals("next")) {
            key = KeyEvent.KEYCODE_MEDIA_NEXT;
            confirm = "Next track.";
        } else if (first.equals("previous") || first.equals("back")) {
            key = KeyEvent.KEYCODE_MEDIA_PREVIOUS;
            confirm = "Previous track.";
        }
        if (key >= 0) {
            dispatchMediaKey(ctx, key);
            return confirm;
        }
        return null; // not a local command -> send to server
    }

    private static void dispatchMediaKey(Context ctx, int key) {
        AudioManager am = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
        if (am == null) return;
        long now = SystemClock.uptimeMillis();
        am.dispatchMediaKeyEvent(new KeyEvent(now, now, KeyEvent.ACTION_DOWN, key, 0));
        am.dispatchMediaKeyEvent(new KeyEvent(now, now, KeyEvent.ACTION_UP, key, 0));
    }
}
