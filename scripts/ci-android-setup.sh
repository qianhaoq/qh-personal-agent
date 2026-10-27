#!/usr/bin/env bash
# Sourced by ci-test.sh before Gradle; setup errors must stop the build.

setup_error() {
  echo "Android CI setup: $*" >&2
  exit 1
}

is_jdk17() {
  local version
  [ -x "$1/bin/java" ] && [ -x "$1/bin/javac" ] || return 1
  version=$("$1/bin/java" -version 2>&1) || return 1
  [[ "$version" =~ version\ \"17[.\"] ]]
}

install_jdk17() (
  local staging
  mkdir -p "$HOME/.local/jdk" || setup_error "cannot create JDK directory"
  staging=$(mktemp -d "$HOME/.local/jdk/.download-XXXXXX") || setup_error "cannot create JDK staging directory"
  trap 'rm -rf "$staging"' EXIT
  curl -fL --retry 3 'https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse' \
    -o "$staging/jdk.tar.gz" || setup_error "JDK 17 download failed"
  mkdir "$staging/jdk"
  tar -xzf "$staging/jdk.tar.gz" -C "$staging/jdk" --strip-components=1 || setup_error "JDK 17 extraction failed"
  is_jdk17 "$staging/jdk" || setup_error "downloaded JDK is not a working JDK 17"
  # Keep the validated installation and point current at it for subsequent runs.
  local installed
  installed=$(mktemp -d "$HOME/.local/jdk/jdk-17-XXXXXX") || setup_error "cannot create JDK destination"
  mv "$staging/jdk" "$installed/home" || setup_error "cannot install JDK 17"
  ln -sfn "$installed/home" "$HOME/.local/jdk/current" || setup_error "cannot update JDK current symlink"
)

setup_jdk17() {
  local candidate path_java
  local candidates=("${JAVA_HOME:-}" "$HOME/.local/jdk/current"
    "$HOME"/.local/jdk/jdk-17* /usr/lib/jvm/temurin-17-jdk* /usr/lib/jvm/java-17-openjdk*)
  path_java=$(command -v java || true)
  if [ -n "$path_java" ]; then
    path_java=$(readlink -f "$path_java" || true)
    [ -z "$path_java" ] || candidates+=("$(dirname "$(dirname "$path_java")")")
  fi
  for candidate in "${candidates[@]}"; do
    if [ -n "$candidate" ] && is_jdk17 "$candidate"; then
      JAVA_HOME=$(cd "$candidate" && pwd -P)
      export JAVA_HOME
      export PATH="$JAVA_HOME/bin:$PATH"
      return
    fi
  done
  install_jdk17
  JAVA_HOME=$(cd "$HOME/.local/jdk/current" && pwd -P)
  export JAVA_HOME
  export PATH="$JAVA_HOME/bin:$PATH"
}

has_android_sdk() {
  [ -f "$1/platforms/android-34/android.jar" ] &&
    [ -x "$1/build-tools/34.0.0/aapt2" ]
}

install_android_sdk() (
  local sdk=$1 staging manager license_status
  mkdir -p "$sdk" || setup_error "cannot create Android SDK directory"
  manager="$sdk/cmdline-tools/latest/bin/sdkmanager"
  if [ ! -x "$manager" ]; then
    staging=$(mktemp -d "$sdk/.download-XXXXXX") || setup_error "cannot create SDK staging directory"
    trap 'rm -rf "$staging"' EXIT
    curl -fL --retry 3 'https://dl.google.com/android/repository/commandlinetools-linux-13114758_latest.zip' \
      -o "$staging/tools.zip" || setup_error "Android command line tools download failed"
    unzip -q "$staging/tools.zip" -d "$staging" || setup_error "Android command line tools extraction failed"
    mkdir -p "$sdk/cmdline-tools"
    [ ! -e "$sdk/cmdline-tools/latest" ] || setup_error "incomplete cmdline-tools/latest installation; remove it and retry"
    mv "$staging/cmdline-tools" "$sdk/cmdline-tools/latest" || setup_error "cannot install Android command line tools"
  fi
  # yes normally exits with SIGPIPE once sdkmanager has all its answers.
  # Inspect sdkmanager's status independently so pipefail does not reject success.
  license_status=0
  yes | "$manager" --sdk_root="$sdk" --licenses || license_status=${PIPESTATUS[1]}
  [ "$license_status" -eq 0 ] || setup_error "Android SDK license acceptance failed ($license_status)"
  "$manager" --sdk_root="$sdk" 'platform-tools' 'platforms;android-34' 'build-tools;34.0.0' ||
    setup_error "Android SDK package installation failed"
  has_android_sdk "$sdk" || setup_error "Android SDK installation is incomplete"
)

setup_android_build() {
  local candidate sdk='' escaped
  setup_jdk17
  for candidate in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" "$HOME/.local/android-sdk"; do
    if [ -n "$candidate" ] && has_android_sdk "$candidate"; then
      sdk=$candidate
      break
    fi
  done
  if [ -z "$sdk" ]; then
    sdk="$HOME/.local/android-sdk"
    install_android_sdk "$sdk"
  fi
  sdk=$(cd "$sdk" && pwd -P)
  export ANDROID_HOME="$sdk" ANDROID_SDK_ROOT="$sdk"
  # java.util.Properties escaping, including spaces in an absolute SDK path.
  escaped=${sdk//\\/\\\\}
  escaped=${escaped//:/\\:}
  escaped=${escaped//=/\\=}
  escaped=${escaped// /\\ }
  escaped=${escaped//#/\\#}
  escaped=${escaped//!/\\!}
  escaped=${escaped//$'\t'/\\t}
  escaped=${escaped//$'\n'/\\n}
  escaped=${escaped//$'\r'/\\r}
  printf 'sdk.dir=%s\n' "$escaped" > android/local.properties || setup_error "cannot write android/local.properties"
}
