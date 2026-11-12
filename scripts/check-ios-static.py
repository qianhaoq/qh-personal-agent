#!/usr/bin/env python3
"""iOS 客户端静态安全检查（文本级，不是 Swift 编译/类型检查）。

用法：python3 scripts/check-ios-static.py [--root 仓库根目录]
检查本机/云端数据隔离、权限声明、工程成员、DEBUG 边界、持久化写法等不变量。
文本启发式只能拦明确违例，Swift 正确性仍须 Mac 上 xcodebuild 验证。
"""
from __future__ import annotations

import argparse
import plistlib
import re
import sys
from pathlib import Path

LOCAL_FILES = ("LocalStore.swift", "LocalModel.swift", "HomeView.swift", "LibraryView.swift", "SpeechDictation.swift")
CLOUD_FILES = ("ContentView.swift", "AppModel.swift", "APIClient.swift")
NETWORK_TOKENS = ("URLSession", "URLRequest", "APIClient", "http://", "https://")
LOCAL_TYPES = ("LocalModel", "LocalStore", "LocalDocument", "LocalItem")
LOG_CALLS = re.compile(r"\b(print|NSLog|os_log|debugPrint|dump)\s*\(|\bLogger\s*\(")
SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{10,}"),
    re.compile(r'"invite_code"\s*:\s*"[^"]+"'),
    re.compile(r'(access|refresh)_token"\s*:\s*"[^"]+"'),
)
USAGE_KEYS = ("NSMicrophoneUsageDescription", "NSSpeechRecognitionUsageDescription")


def strip_comments(text: str) -> str:
    return "\n".join(line.split("//", 1)[0] if "//" in line and "://" not in line else line
                     for line in text.splitlines())


def debug_only_violations(text: str, pattern: re.Pattern[str]) -> list[int]:
    """Return 1-based line numbers where pattern appears outside an `#if DEBUG` block."""
    stack: list[bool] = []
    bad = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#if"):
            stack.append(stripped == "#if DEBUG")
        elif stripped.startswith("#else") and stack:
            stack[-1] = False
        elif stripped.startswith("#endif") and stack:
            stack.pop()
        elif pattern.search(line) and not any(stack):
            bad.append(number)
    return bad


def check(root: Path) -> list[str]:
    errors: list[str] = []
    app = root / "ios" / "PersonalAgent"
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(app.glob("*.swift"))}
    code = {name: strip_comments(text) for name, text in sources.items()}

    # 工程成员：每个 Swift 文件都在 PBXFileReference 且进入 Sources 构建阶段；引用的文件都存在。
    pbx = (root / "ios" / "PersonalAgent.xcodeproj" / "project.pbxproj").read_text(encoding="utf-8")
    refs = dict((path, ref) for ref, path in re.findall(
        r"(\w{24}) = \{ isa = PBXFileReference; lastKnownFileType = sourcecode\.swift; path = ([\w.]+);", pbx))
    builds = dict(re.findall(r"(\w{24}) = \{ isa = PBXBuildFile; fileRef = (\w{24}); \};", pbx))
    phase = re.search(r"isa = PBXSourcesBuildPhase;[^}]*files = \(([^)]*)\)", pbx)
    in_phase = {builds.get(b.strip()) for b in (phase.group(1).split(",") if phase else []) if b.strip()}
    for name in sources:
        if name not in refs:
            errors.append(f"pbxproj: {name} has no PBXFileReference")
        elif refs[name] not in in_phase:
            errors.append(f"pbxproj: {name} is not in the Sources build phase")
    for name in refs:
        if name not in sources:
            errors.append(f"pbxproj: references missing file {name}")

    if sum(len(re.findall(r"^\s*@main\b", t, re.M)) for t in code.values()) != 1:
        errors.append("swift: expected exactly one @main")

    # Info.plist：可解析；无 ATS 放宽；麦克风/语音用途说明与实际使用一一对应。
    with open(app / "Info.plist", "rb") as handle:
        plist = plistlib.load(handle)
    if "NSAppTransportSecurity" in plist:
        errors.append("Info.plist: NSAppTransportSecurity must not be relaxed")
    uses_speech = any(re.search(r"^\s*import\s+(Speech|AVFoundation)\b", t, re.M) for t in code.values())
    for key in USAGE_KEYS:
        value = plist.get(key)
        if uses_speech and not (isinstance(value, str) and value.strip()):
            errors.append(f"Info.plist: {key} required when Speech/AVFoundation is used")
        if not uses_speech and key in plist:
            errors.append(f"Info.plist: {key} declared but no code uses it")

    for name, text in code.items():
        if "preferredColorScheme(.light)" in text or "paperHex" in text:
            errors.append(f"{name}: forced light mode / paper palette must not return")
        if re.search(r"\.disabled\(\s*true\s*\)", text):
            errors.append(f"{name}: permanently disabled control (fake capability button)")
        if LOG_CALLS.search(text):
            errors.append(f"{name}: logging call may leak user content or tokens")
        if any(p.search(sources[name]) for p in SECRET_PATTERNS):
            errors.append(f"{name}: looks like an embedded credential")
        for line in debug_only_violations(sources[name], re.compile(r"static func preview|#Preview|-PAFail")):
            errors.append(f"{name}:{line}: preview/sample/fault-injection code outside #if DEBUG")

    # 本机侧：不得触网、不得持有云端会话；HomeView 只允许创建 AppModel 并交给 CloudAccountView。
    for name in LOCAL_FILES:
        text = code.get(name)
        if text is None:
            continue
        for token in NETWORK_TOKENS:
            if token in text:
                errors.append(f"{name}: local code must not use {token}")
        if "UserDefaults" in text:
            errors.append(f"{name}: local data must use the versioned file store, not UserDefaults")
        if name != "HomeView.swift" and "AppModel" in text:
            errors.append(f"{name}: local code must not reference the cloud AppModel")
    home = code.get("HomeView.swift", "")
    for member in re.findall(r"\bcloud\.(\w+)", home):
        errors.append(f"HomeView.swift: must not call cloud.{member} (local content never flows to cloud)")

    # 云端侧：不得读取本机数据（连接云端不得上传本机内容）。
    for name in CLOUD_FILES:
        for token in LOCAL_TYPES:
            if token in code.get(name, ""):
                errors.append(f"{name}: cloud code must not reference {token}")

    store = code.get("LocalStore.swift", "")
    if store:
        imports = set(re.findall(r"^\s*import\s+(\w+)", store, re.M))
        if imports != {"Foundation"}:
            errors.append("LocalStore.swift: must import only Foundation (testable outside Xcode)")
        if ".atomic" not in store:
            errors.append("LocalStore.swift: writes must be atomic")
        if "completeFileProtection" not in store:
            errors.append("LocalStore.swift: iOS writes must request file protection")
    for name in ("LocalStore.swift", "LocalModel.swift"):
        if "removeItem" in code.get(name, ""):
            errors.append(f"{name}: must never delete the data file")

    speech = code.get("SpeechDictation.swift")
    if speech is not None:
        if "requiresOnDeviceRecognition = true" not in speech:
            errors.append("SpeechDictation.swift: must require on-device recognition")
        if re.search(r"requiresOnDeviceRecognition\s*=\s*false", speech):
            errors.append("SpeechDictation.swift: server recognition fallback is forbidden")
        if re.search(r"\bsend\s*\(", speech):
            errors.append("SpeechDictation.swift: dictation must never send")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    errors = check(parser.parse_args().root)
    for error in errors:
        print(f"ERROR {error}")
    print(f"check-ios-static: {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
