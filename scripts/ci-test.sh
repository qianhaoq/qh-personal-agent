#!/usr/bin/env bash
# CI Build & Test for the monorepo skeleton. Each part runs only when it exists.
set -euo pipefail
ran=0

if find server -name '*.py' -not -path '*/.venv/*' | grep -q .; then
  echo "== server: lint + tests"
  python -m pip install -q ruff pytest
  if [ -f server/requirements.txt ]; then python -m pip install -q -r server/requirements.txt; fi
  if [ -f server/pyproject.toml ]; then python -m pip install -q -e server; fi
  ruff check server
  if find server -name 'test_*.py' -o -name '*_test.py' | grep -q .; then
    (cd server && python -m pytest -q)
  else
    echo "server: no tests yet"
  fi
  ran=1
else
  echo "== server: no Python code yet — skipped"
fi

if [ -f android/settings.gradle.kts ] || [ -f android/settings.gradle ]; then
  echo "== android: gradle build"
  if [ ! -x android/gradlew ]; then
    echo "android/ has a Gradle project but no executable gradlew" >&2
    exit 1
  fi
  (cd android && ./gradlew --no-daemon assembleDebug testDebugUnitTest)
  ran=1
else
  echo "== android: no Gradle project yet — skipped"
fi

[ "$ran" = 1 ] || echo "skeleton: nothing to build yet (OK)"
