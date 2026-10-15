#!/bin/bash
# Manual Wear OS APK build for Slick Walk v3 (board WSS transport):
# aapt2 + javac + d8 + zipalign + apksigner, no Gradle.
#
# Before building, run ./install.sh (it writes your local config to
# ~/.config/message-board/watch.env). This script then generates the
# gitignored Defaults.java from Defaults.java.template + that config, so
# the board host/credentials are baked into the APK without ever being
# committed. If no config exists but a Defaults.java is already present
# (e.g. hand-written), it is reused with a warning.
#
# Env overrides:
#   SLICK_WALK_CONFIG   config file (default ~/.config/message-board/watch.env;
#                       kept name for backward compat with older checkouts)
#   WALK_PROJ           watch-v3 source dir (default <clients/watch>/watch-v3)
#   WALK_ANDROID_SDK / ANDROID_SDK_ROOT / ANDROID_HOME
#                       Android SDK location
#   WALK_JAVA_HOME      JDK 17 location
#   WALK_KEYSTORE       keystore (default: KEYSTORE_PATH from config, else
#                       <repo>/watch-v3/debug.keystore)
#   WALK_APK            output APK (default <repo>/SlickWalk-v3.apk)
set -e
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="${WALK_PROJ:-$REPO_DIR/watch-v3}"

# --- local config (board host/credentials) -------------------------------
CONFIG_FILE="${SLICK_WALK_CONFIG:-$HOME/.config/message-board/watch.env}"
TEMPLATE="$PROJ/java/com/slick/walkmode/v3/Defaults.java.template"
GENERATED="$PROJ/java/com/slick/walkmode/v3/Defaults.java"
if [ -f "$CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CONFIG_FILE"
    if [ -f "$TEMPLATE" ]; then
        echo "== generate Defaults.java from $CONFIG_FILE =="
        case "$BOARD_PORT" in ''|*[!0-9]*) echo "build: BOARD_PORT must be a number: $BOARD_PORT" >&2; exit 1 ;; esac
        case "${BOARD_USE_TLS,,}" in true|false) ;; *) echo "build: BOARD_USE_TLS must be true/false: $BOARD_USE_TLS" >&2; exit 1 ;; esac
        export BOARD_HOST BOARD_PORT BOARD_USE_TLS BOARD_WS_PATH
        export BOARD_USERNAME BOARD_PASSWORD BOARD_PEER_USERNAME
        python3 - "$TEMPLATE" "$GENERATED" "$CONFIG_FILE" <<'PYEOF'
import os, sys
template_path, out_path, config_path = sys.argv[1], sys.argv[2], sys.argv[3]
required = ["BOARD_HOST", "BOARD_PORT", "BOARD_USE_TLS", "BOARD_WS_PATH",
            "BOARD_USERNAME", "BOARD_PASSWORD", "BOARD_PEER_USERNAME"]
values = {}
missing = []
for key in required:
    val = os.environ.get(key, "")
    if val == "":
        missing.append(key)
    values[key] = val
if missing:
    sys.exit("build: missing keys in %s: %s (re-run install.sh)"
             % (config_path, ", ".join(missing)))
with open(template_path, encoding="utf-8") as f:
    text = f.read()
for key, val in values.items():
    if key in ("BOARD_PORT",):
        replacement = val  # int literal, no quoting
    elif key in ("BOARD_USE_TLS",):
        replacement = val.lower() if val.lower() in ("true", "false") else val
    else:
        # Java string literal escaping
        replacement = (val.replace("\\", "\\\\").replace('"', '\\"')
                          .replace("\n", "\\n").replace("\r", "\\r")
                          .replace("\t", "\\t"))
    text = text.replace("@%s@" % key, replacement)
leftover = [t for t in ("@BOARD_HOST@", "@BOARD_PORT@", "@BOARD_USE_TLS@",
                        "@BOARD_WS_PATH@", "@BOARD_USERNAME@",
                        "@BOARD_PASSWORD@", "@BOARD_PEER_USERNAME@") if t in text]
if leftover:
    sys.exit("build: unsubstituted placeholders in Defaults.java.template: %s" % leftover)
with open(out_path, "w", encoding="utf-8") as f:
    f.write(text)
print("build: wrote", out_path, "(gitignored — never commit it)")
PYEOF
    fi
elif [ ! -f "$GENERATED" ]; then
    echo "build: no config at $CONFIG_FILE and no $GENERATED" >&2
    echo "build: run ./install.sh first (it collects the board host/credentials)" >&2
    exit 1
else
    echo "build: WARNING: no config at $CONFIG_FILE; reusing existing $GENERATED" >&2
fi

# --- SDK / JDK ------------------------------------------------------------
if [ -n "${WALK_ANDROID_SDK:-}" ]; then
  SDK="$WALK_ANDROID_SDK"
elif [ -n "${ANDROID_SDK_ROOT:-}" ]; then
  SDK="$ANDROID_SDK_ROOT"
elif [ -n "${ANDROID_HOME:-}" ]; then
  SDK="$ANDROID_HOME"
else
  SDK="$HOME/Android/Sdk"
fi
BT="$SDK/build-tools/34.0.0"
PLATFORM="$SDK/platforms/android-34/android.jar"
OUT="$PROJ/out"
APK="${WALK_APK:-$REPO_DIR/SlickWalk-v3.apk}"

if [ ! -x "$BT/aapt2" ] || [ ! -f "$PLATFORM" ]; then
  echo "build: Android SDK not found at $SDK" >&2
  echo "build: install it (Android Studio, or cmdline-tools) and set" >&2
  echo "build: ANDROID_SDK_ROOT, or WALK_ANDROID_SDK for a custom path." >&2
  echo "build: Need build-tools 34.0.0 and platforms/android-34 (android.jar)." >&2
  exit 1
fi

# Signing key lives OUTSIDE out/: the build wipes $OUT every run, and a
# keystore inside it would be regenerated each time, breaking updates.
# install.sh generates it at KEYSTORE_PATH (default ~/.config/message-board/
# debug.keystore); fall back to the checkout copy for hand-built setups.
KEYSTORE="${WALK_KEYSTORE:-${KEYSTORE_PATH:-$PROJ/debug.keystore}}"
STORE_PASS="${KEYSTORE_STORE_PASS:-android}"
KEY_PASS="${KEYSTORE_KEY_PASS:-android}"

if [ -n "${WALK_JAVA_HOME:-}" ]; then
  export JAVA_HOME="$WALK_JAVA_HOME"
else
  export JAVA_HOME="$(ls -d "$HOME"/workspace/tooling/jdk/jdk-* 2>/dev/null | head -1)"
fi
if [ -z "$JAVA_HOME" ] || [ ! -x "$JAVA_HOME/bin/javac" ]; then
  JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
fi
JAVAC="$JAVA_HOME/bin/javac"
KEYTOOL="$JAVA_HOME/bin/keytool"
export PATH="$JAVA_HOME/bin:$PATH"

# One-time migration: the key used to live inside $OUT (regenerated every
# build, breaking updates). Move it beside out/ if it's still there.
if [ ! -f "$KEYSTORE" ] && [ -f "$OUT/debug.keystore" ]; then
  mv "$OUT/debug.keystore" "$KEYSTORE"
fi

rm -rf "$OUT"
mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex"

if [ ! -f "$KEYSTORE" ]; then
  "$KEYTOOL" -genkeypair -keystore "$KEYSTORE" -alias androiddebugkey \
    -storepass "$STORE_PASS" -keypass "$KEY_PASS" -keyalg RSA -keysize 2048 -validity 10950 \
    -dname "CN=Android Debug,O=Android,C=US" 2>/dev/null
fi

echo "== aapt2 compile =="
"$BT/aapt2" compile --dir "$PROJ/res" -o "$OUT/compiled_res.zip"

echo "== aapt2 link =="
"$BT/aapt2" link -o "$OUT/app.unsigned.apk" -I "$PLATFORM" \
  --manifest "$PROJ/AndroidManifest.xml" \
  --min-sdk-version 30 --target-sdk-version 34 \
  --java "$OUT/gen" "$OUT/compiled_res.zip"

echo "== javac =="
find "$PROJ/java" "$OUT/gen" -name "*.java" > "$OUT/sources.txt"
"$JAVAC" -encoding UTF-8 --release 17 -cp "$PLATFORM" -d "$OUT/classes" @"$OUT/sources.txt"

echo "== d8 =="
"$BT/d8" --lib "$PLATFORM" --min-api 30 --output "$OUT/dex" \
  $(find "$OUT/classes" -name "*.class")

echo "== package dex =="
python3 - "$OUT/app.unsigned.apk" "$OUT/dex/classes.dex" "$OUT/app.withdex.apk" <<'EOF'
import sys, zipfile
src, dexf, dst = sys.argv[1], sys.argv[2], sys.argv[3]
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        zout.writestr(item, zin.read(item.filename))
    zout.write(dexf, 'classes.dex')
EOF

echo "== zipalign + sign =="
"$BT/zipalign" -f 4 "$OUT/app.withdex.apk" "$OUT/app.aligned.apk"
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass "pass:$STORE_PASS" --key-pass "pass:$KEY_PASS" \
  --out "$APK" "$OUT/app.aligned.apk"
"$BT/apksigner" verify --print-certs "$APK" | head -4
ls -la "$APK"
echo "WATCH_V3_BUILD_DONE"
