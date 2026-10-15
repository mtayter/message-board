package com.slick.walkmode.v3;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Token auth for the message board API.
 * Only handles login (POST /board/login) to obtain a Bearer token.
 * Message send/receive is WSS-only via BoardWssClient — no HTTP messaging.
 *
 * Every method blocks on the network. NEVER call from the main thread.
 */
public class BoardClient {

    /** Board URL for login. Host/port come from the generated Defaults. */
    private static String base() { return Defaults.BOARD_BASE_URL; }

    /** Cached Bearer token. Null = not yet logged in. */
    private static volatile String sToken = null;

    /**
     * Login to the board and cache the token.
     * Called automatically by getToken if needed.
     */
    private static synchronized boolean ensureToken(String username, String password) {
        if (sToken != null) return true;
        HttpURLConnection c = null;
        try {
            URL url = new URL(base() + "/login");
            c = (HttpURLConnection) url.openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(15000);
            c.setReadTimeout(15000);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            String body = "{\"username\":\"" + jsonEscape(username)
                    + "\",\"password\":\"" + jsonEscape(password) + "\"}";
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            OutputStream os = c.getOutputStream();
            os.write(bytes);
            os.flush();
            os.close();
            if (c.getResponseCode() != 200) return false;
            String resp = readAll(c.getInputStream());
            JSONObject root = new JSONObject(resp);
            sToken = root.optString("token", null);
            return sToken != null && !sToken.isEmpty();
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** Clear the cached token (e.g., on auth failure). */
    public static synchronized void resetToken() { sToken = null; }

    /** Get the cached token, logging in if needed. Returns null on failure. */
    public static synchronized String getToken(String username, String password) {
        if (sToken != null) return sToken;
        if (ensureToken(username, password)) return sToken;
        return null;
    }

    private static String readAll(InputStream in) throws Exception {
        BufferedReader br = new BufferedReader(
                new InputStreamReader(in, StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder();
        char[] buf = new char[4096];
        int n;
        while ((n = br.read(buf)) != -1) {
            sb.append(buf, 0, n);
        }
        return sb.toString();
    }

    private static String jsonEscape(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 16);
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            switch (ch) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (ch < 0x20) {
                        sb.append(String.format("\\u%04x", (int) ch));
                    } else {
                        sb.append(ch);
                    }
            }
        }
        return sb.toString();
    }
}
