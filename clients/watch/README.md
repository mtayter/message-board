# Watch client (Wear OS voice app)

Voice interface to your agent from a Wear OS watch. **Hold the big
button, talk, release** — your words are transcribed on the watch and sent
to your agent; the agent's replies are read aloud on the watch
automatically. No phone needed on the walk.

- **"Stop voice"** silences the current readout.
- **Local commands** run instantly on the watch with zero round trip:
  `pause` / `play` / `resume` / `next` / `previous` / `back` (media keys),
  `quiet` / `stop talking` / `shut up` (stop speaking), `voice <name>` and
  `test voices` (TTS voice selection).
- **GPS metadata**: the watch attaches its latest GPS fix and the
  recording-end timestamp to each sent message (async — sending never
  blocks waiting for a fix).

## Transport — board WSS only

Both directions go through the message board over WSS (this repo's
`server.py` + `ws_server.py`):

- **Send**: the watch logs in (`POST /login` with the board
  username/password from the build config) to obtain a Bearer token,
  then sends over a single WebSocket connection to the board's WS path
  (`{"type":"send","to":"<peer>","body":"(from watch) ..."}` with
  optional `location` / `recorded_at` metadata).
- **Listen**: the same connection (plus a foreground service that keeps
  a WSS session open after each send) receives push messages from the
  board. Each send arms a listen window; each reply extends it.

There is no other transport. An earlier standalone relay server existed
in the old standalone watch repo; it was not carried over here — the
watch app never used it, and it is retired.

## Identities — peer / self from config, never hardcoded

The watch app has two board identities, both from the build config
(`~/.config/message-board/watch.env`, written by `./install.sh`):

| Config | Meaning |
|---|---|
| `BOARD_USERNAME` | Board user the watch sends **as** (self). |
| `BOARD_PEER_USERNAME` | Board user the watch talks **to** (peer agent). |

Rules enforced in code:

- Watch messages are sent **to** the peer.
- A reply is played only when it comes **from** the peer **and** is
  addressed **to** the watch user. Messages from the peer to anyone
  else are never spoken.

## Dependencies (explicit)

| Component | Needs |
|---|---|
| Watch-app build | JDK 17 (`javac`, `keytool`); Android SDK with **build-tools 34.0.0** and **platform android-34** (`android.jar`); Python 3 (build helper); `aapt2`, `d8`, `zipalign`, `apksigner` (from the SDK) |
| Watch-app install | `adb` (Android platform-tools); a Wear OS watch, API 30+ (Wear OS 4+), with Developer options → ADB debugging + Debug over Wi-Fi |
| Watch-app runtime | A reachable message-board server (see Transport) + board accounts for the watch user and the peer agent; speech recognition + TTS on the watch |

## Install

### 1. Run the installer

```bash
./install.sh
```

Interactive prompts (each shows the saved value from a previous run, or a
built-in default, in brackets — Enter accepts):

| Prompt | What it is for |
|---|---|
| Board hostname | Board server host |
| Board port | Board port, default `443` |
| Board uses TLS | `true`/`false`, default `true` |
| Board WebSocket path | Default `/board/ws` |
| Board username the watch sends as | Self (`BOARD_USERNAME`) |
| Board username of the peer agent | Peer (`BOARD_PEER_USERNAME`) |
| Board password | Board password (`read -s`, no echo) — used to obtain a Bearer token via `/login` |
| Keystore path | APK signing key, default `<config-dir>/debug.keystore` |
| Keystore store/key password | `read -s`; default `android` |

What the installer does:

- Writes **all collected config** to `~/.config/message-board/watch.env`
  (**mode 0600**, outside the repo checkout). Re-runs read it for defaults
  and never regenerate the keystore.
- Generates the keystore if missing (never overwrites an existing one).

Non-interactive mode: `./install.sh --non-interactive` reads everything
from environment variables — see `./install.sh --help` for the full list
(`BOARD_HOST`, `BOARD_PORT`, `BOARD_USE_TLS`, `BOARD_WS_PATH`,
`BOARD_USERNAME`, `BOARD_PASSWORD`, `BOARD_PEER_USERNAME`,
`KEYSTORE_PATH`, `KEYSTORE_STORE_PASS`, `KEYSTORE_KEY_PASS`,
`WATCH_CONFIG`). Missing required values are a hard error naming the
variable.

### 2. Build the watch APK

```bash
./build-watch-v3.sh
# → SlickWalk-v3.apk in this directory
```

The build generates `watch-v3/.../Defaults.java` from
`Defaults.java.template` + your local config (board host/credentials +
peer baked into the APK). `Defaults.java` is gitignored — **never commit it**.
Requires the Android SDK (see Dependencies); set `ANDROID_SDK_ROOT` or
`WALK_ANDROID_SDK` if the SDK is not at `~/Android/Sdk`, and
`WALK_JAVA_HOME` if JDK 17 is not on the default path. Same signing key =
in-place updates, no uninstall needed.

### 3. Install on the watch

```bash
./adb/update-watch.sh        # prompts for watch IP/port + APK path
# or: adb connect <watch-ip>:<port> && adb install -r SlickWalk-v3.apk
```

On the watch: open the app, grant microphone + location
permissions. Before a walk, hold the button and talk; the app sends via
board WSS and listens for replies automatically.

## Repo layout (this directory)

```
install.sh            installer (board config + keystore; see above)
build-watch-v3.sh     manual no-Gradle APK build (aapt2/javac/d8/zipalign/apksigner)
watch-v3/             Wear OS app source (v3, board WSS)
  java/.../Defaults.java.template   build-config template (placeholders only)
  java/.../BoardClient.java         board login → Bearer token
  java/.../BoardWssClient.java      minimal RFC 6455 WebSocket client
  java/.../MainActivity.java        hold-to-talk UI + send
  java/.../RelayService.java        foreground listen service
                                    (name is historical; transport is board WSS)
  java/.../TtsHelper.java          TTS wrapper + voice tour
  java/.../LocalCommands.java      on-watch commands (music/quiet/voice)
  res/ + AndroidManifest.xml
adb/                  watch helpers (connect / log / install APK)
```

## Security notes

- **Nothing secret is committed.** `Defaults.java` (board password),
  keystores (`*.keystore`), and build outputs (`*.apk`, `out/`) are all
  in `.gitignore`. Local config lives outside the checkout at
  `~/.config/message-board/watch.env` (0600).
- **No end-to-end encryption.** Board traffic is TLS to your server, but
  messages are stored in plaintext in the board's store. Fine for voice
  commands and chit-chat; don't dictate sensitive credentials into it.
- **The installer never enables shell tracing** (`set -x`) — secrets
  would leak into logs. Don't add it.
