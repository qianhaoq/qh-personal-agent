"""Unit tests for scripts/check-ios-static.py (author self-tests).

Run: python3 -m unittest scripts/test_check_ios_static.py
Each test copies ios/ into a temp dir, injects one violation and asserts the
checker reports it; the untouched copy must pass. This proves the checks fire,
not that the Swift compiles.
"""
from __future__ import annotations

import importlib.util
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("check_ios_static", ROOT / "scripts" / "check-ios-static.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class CheckIOSStaticTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "ios", self.tmp / "ios")
        self.app = self.tmp / "ios" / "PersonalAgent"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def edit(self, name: str, old: str, new: str) -> None:
        path = self.app / name
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, f"fixture anchor missing in {name}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def assertFlags(self, fragment: str) -> None:
        errors = checker.check(self.tmp)
        self.assertTrue(any(fragment in e for e in errors), f"expected {fragment!r} in {errors}")

    def test_repository_passes(self) -> None:
        self.assertEqual(checker.check(self.tmp), [])

    def test_unregistered_swift_file(self) -> None:
        (self.app / "Extra.swift").write_text("import Foundation\n", encoding="utf-8")
        self.assertFlags("Extra.swift has no PBXFileReference")

    def test_local_code_using_network(self) -> None:
        self.edit("LocalModel.swift", "import Combine", "import Combine\nlet leak = URLSession.shared")
        self.assertFlags("local code must not use URLSession")

    def test_home_forwarding_local_content_to_cloud(self) -> None:
        self.edit("HomeView.swift", "local.saveDraftNow()", "local.saveDraftNow(); cloud.draft = local.draft")
        self.assertFlags("must not call cloud.draft")

    def test_cloud_code_reading_local_store(self) -> None:
        self.edit("AppModel.swift", "private let api = APIClient()", "private let api = APIClient()\n    let l = LocalModel()")
        self.assertFlags("cloud code must not reference LocalModel")

    def test_forced_light_mode(self) -> None:
        self.edit("PersonalAgentApp.swift", "RootView()", "RootView().preferredColorScheme(.light)")
        self.assertFlags("forced light mode")

    def test_permanently_disabled_button(self) -> None:
        self.edit("HomeView.swift", ".disabled(!canSend)", ".disabled(true)")
        self.assertFlags("permanently disabled control")

    def test_logging(self) -> None:
        self.edit("LocalModel.swift", "func dismissError() {", "func dismissError() { print(draft);")
        self.assertFlags("logging call")

    def test_preview_outside_debug(self) -> None:
        path = self.app / "HomeView.swift"
        path.write_text(path.read_text(encoding="utf-8") + '\n#Preview("leak") { RootView() }\n', encoding="utf-8")
        self.assertFlags("outside #if DEBUG")

    def test_store_must_not_delete_file(self) -> None:
        self.edit("LocalStore.swift", "private func enterReadOnly(_ value: LocalStoreProblem) {",
                  "private func enterReadOnly(_ value: LocalStoreProblem) {\n        try? FileManager.default.removeItem(at: fileURL)")
        self.assertFlags("must never delete the data file")

    def test_store_requires_atomic_write(self) -> None:
        self.edit("LocalStore.swift", "[.atomic, .completeFileProtectionUntilFirstUserAuthentication]",
                  "[.completeFileProtectionUntilFirstUserAuthentication]")
        self.edit("LocalStore.swift", "[.atomic]", "[]")
        self.assertFlags("writes must be atomic")

    def test_ats_relaxation(self) -> None:
        path = self.app / "Info.plist"
        data = plistlib.loads(path.read_bytes())
        data["NSAppTransportSecurity"] = {"NSAllowsArbitraryLoads": True}
        path.write_bytes(plistlib.dumps(data))
        self.assertFlags("NSAppTransportSecurity")

    def test_speech_without_usage_strings(self) -> None:
        path = self.app / "Info.plist"
        data = plistlib.loads(path.read_bytes())
        for key in checker.USAGE_KEYS:
            data.pop(key, None)
        path.write_bytes(plistlib.dumps(data))
        self.edit("LocalModel.swift", "import Combine", "import Combine\nimport Speech")
        self.assertFlags("NSMicrophoneUsageDescription required")

    def test_embedded_credential(self) -> None:
        self.edit("APIClient.swift", 'forHTTPHeaderField: "Accept")',
                  'forHTTPHeaderField: "Accept")\n        _ = "Bearer abcdefghijklmnop"')
        self.assertFlags("embedded credential")


if __name__ == "__main__":
    unittest.main()
