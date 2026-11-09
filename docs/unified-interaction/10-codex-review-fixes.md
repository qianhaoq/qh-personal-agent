# Codex implementation fixes after actual Kimi review 09

Status: concrete source fixes and regression tests are saved; **post-fix full Gradle/host-test execution and independent re-review are pending**. No implementation P0/P1-clearance or release approval is claimed.

Repository/worktree/branch are unchanged. HEAD remains `3600c3ab4a1c499f099e39be6b8084b3351dd7b7`; no commits, pushes, PRs, external messages, deployments, settings changes or other worktrees touched. Actual independent design/code reviews remain intact. Read the complete 09 and independently compared its paths with current source. Review 09 SHA-256: `f37e617899d002d309ee8acaa9d4b2606d000b0566cbedf9f8a577264b0beab0`.

## Actual build failure: validate before capturing default owner

Coordinator logs `/workspace/qh-unified-evidence/implementation/full-gradle-after-kapt-fix.log` and `full-gradle-all-diagnostics.log` show real Room generation and Android build work. The latter reaches app debug/release assembly, app units and lint under `--continue`, but the overall build fails: `56 tests completed, 2 failed`, specifically `RepositoryTest.noteDetailRejectsBlankIdWithoutNetwork` and `searchValidatesBeforeSendingRequests`. Their correct preflight assertions exposed a product regression: Kotlin evaluates `owner = captureOwner()` before entering method bodies, so invalid input on an unauthenticated repository threw SessionExpired instead of input rejection.

Fixed default-owner evaluation through `validatedOwner { ... }`: note/search validation precedes metadata owner capture. Applied the same ordering to create/patch/ask/sync argument checks to fix the class of regression, retaining implementation-side validation for explicitly supplied owners. Successful validation still captures immutable owner before suspension, and credential reads remain guarded inside the session mutex. **Neither of the two existing failing tests nor their assertions was changed.** RepositoryTest changes relative to baseline are only the already-required TokenStore test implementation methods. Added a no-socket regression asserting invalid logged-out inputs reject without credential read or HTTP.

## P1 dispositions

| Finding | Independent assessment and actual source fix | Real regression tests added/updated |
| --- | --- | --- |
| P1-1 model logout order | Confirmed. Repository now exposes synchronous `beginLogout`, an immutable metadata-only `LogoutSession` receipt, conditional `withClosingSession`, and mutex-protected `finishLogout`. SecureTokens atomically persists a new epoch plus closed=true in one preference commit. `AgentModel.logout` calls begin synchronously before launching any coroutine or cleaning Room/audio/alarms, then invalidates process alarm callbacks. Room and alarm cleanup remain conditional on that exact closing receipt; cleanup failures cannot prevent remaining cleanup, and network logout is bounded at eight seconds. Late cleanup/network finalization cannot touch a new same-user login. | `LogoutRoomModelTest`: actual model call before queued cleanup, durable synthetic session re-instantiation, zero credential reads while waiting; actual Room reopen; cleanup fault; same-user relogin before stale cleanup; actual Room write/close serialization and rejection of late writes. Existing RequestOwner race assertions retained. |
| P1-2 scheduled badge | Confirmed. Shared `reminderStatus("scheduled")` is now **服务端已确认**. `ReminderScheduleStatus` renders the server label and a separate local result. Only actual submitted-exact/inexact enum labels claim local submission. Production scan has no remaining 已排程 mapping. | Corrected the legacy scheduled-label expectation in `ReminderStatusTest`: its old assertion was wrong because server confirmation is not an AlarmManager receipt. Other status assertions retained. `ComposeReviewTest.allLocalResultsRenderServerConfirmationSeparately` renders every local result, including failed/expired/unknown, and rejects false submission labels. |
| P1-3 404/tombstone/409 projection | Confirmed. Reconcile always applies durable recovery state even on exceptions; deleted A removes note projection, matching recoveredNote/detail, related reminders and local alarms under existing fences, preserving B. Sync tombstones update recovery independently of alarm-consume generation, then apply the same projection deletion. Update requires live A. PATCH409 fetches and displays authoritative A/current rev and explicitly asks for a second decision; it never automatically PATCHes B against the new rev. | `RecoveryWireReviewTest`: real Retrofit/MockWebServer distinct-ID 404, sync tombstones, 409/new rev and explicit second PATCH. `RecoveryModelRoomTest`: model/real Room GET404 and sync paths remove stale affordance, 409 retains B and refreshes A. `RealRecoveryModelTest`: RealRepository + model + real Room, distinct IDs, 409 then 404 with API seam, no implicit second PATCH or create. |
| P1-4 live A + B save-as-new | Confirmed missing UI capability. RecoveryCard has an explicit **将草稿另存为新记事，保留原记事** action only for authoritative live A. `saveCopy` captures a fresh operation; `prepare(replaceLive=true)` atomically replaces only an already-live recovery with B's complete new snapshot and new client identity, before POST. A's server projection stays. Unknown/deleted/rejected recoveries cannot be silently replaced. Busy gating prevents double click; response loss preserves B's recovery and any next draft C. | `RecoveryModelRoomTest.saveCopyUsesNewDurableIdentityAndSurvivesLostResponseDoubleClickAndRestart` inspects the real Room snapshot while POST is suspended, checks both identities, double-click count, lost response, restart recovery and preserved C/A. `RecoveryWireReviewTest.realSaveCopyResponseLossRestartUsesNewIdentityAndKeepsBothServerObjects` verifies actual wire requests and authoritative reconciliation without duplicate POST. |
| P1-5 interactive bounds | Partially disagreed with the inference: absence of an explicit modifier does **not** establish Material3 default touch failure. Preserve Material3 semantics/enforcement; add explicit `touchTarget()` layout minimum 48×48dp to our buttons, tabs and chips, including dialogs, notification explanation, search and navigation. Top/note-action/status-filter rows wrap. Composer is inside the scrollable flow so a large fixed dock cannot consume the viewport. | Actual host `ComposeReviewTest`, not helper-only tests: default Material3 touch bounds; app-owned layout **and touch** bounds; reminder/ASR controls; recovery and deletion dialogs; date/time confirmation; navigation, search and filters at 320dp/fontScale 2.0. Tests are saved but not run here. Consequently actual runtime sizing verification remains pending, not a declared accessibility pass. |

## P2 dispositions

1. **Voice metadata — fixed in source.** Unified acceptVoice persists `voice_asr` for record mode before preparing/sending; editing manually returns source to app. Model/Room regression starts through `beginVoice`/`acceptVoice` and checks the actual submitted NoteCreate.source.
2. **Rejected/deleted recovery copy — fixed in source.** RecoveryCard has distinct sending/unknown/live/rejected/deleted text and state-appropriate actions. Deleted/rejected do not offer reconcile/retry/update as though merely unknown. B is retained. Host tests cover stale update/copy affordances disappearing on deletion; core/model tests cover durable states.
3. **First-only notification explanation — fixed in source.** A durable synchronized device-wide claim is committed before showing the explanation; dismissal or denial does not trigger another automatic request on confirm/snooze/local retry. Already-granted permission does not request again. Status-page manual settings remain. Explanation itself is a real Compose dialog with explicit targets. `NotificationPromptTest` checks persistence across instances. Actual system permission denial/channel behavior remains device QA.
4. **Replay helper disconnected from UI — fixed in source.** Production AnswerCard calls `answerReplayText` with actual busy/ASR/TTS state, uses its result for both enablement and playback, labels speaking state, and keeps stop separate. Unified composer exports actual recognizer UI state. Host test changes those inputs and exercises the real replay control; old chunk/replay regressions retained. No automatic TTS added.
5. **Attention/source/search affordances — fixed in source.** First real suggested reminder with a source has an attention action. No-citation answer offers search; search empty copy suggests shorter keywords and null source explicitly says unavailable. No fake counts, network routing or new server scope.
6. **Consume successful ask version — fixed in source.** Capture submitted version; on success consume only if persisted version/mode/text still match, increment version and clear unknown. Newer B remains, including mode changes. `RecoveryModelRoomTest` covers unchanged successful question consumption and B typed while ask is suspended. Failed/unknown ask still retains the draft and never automatically retries.
7. **Developer details/density — fixed in source.** Remove user-visible note rev and search score/technical ID noise, retain rev in mutation payloads, limit note body previews to four lines and show long-answer expansion only on actual overflow. Root surface uses canvas/background. Source/detail access remains. Pixel/color calibration is not claimed without rendering.
8. **Large font/keyboard/focus — partially fixed, specific remainder deferred.** Add wrapping action/filter rows, explicit target sizing, scrollable composer, error live-region semantics and real large-font host scenarios. Retain mode selected semantics, Ctrl/Cmd+Enter and native Dialog keyboard/focus behavior. **P2: defer — full physical keyboard/focus-return traversal, active-ASR Escape behavior outside the focused field, TalkBack announcements, contrast measurement, reduced-motion behavior and realistic phone viewport/fontScale matrix require further host/device evidence; no such pass is claimed.** Escape handling currently belongs to the text-input key handler and must not be advertised as a global server cancel. Native dialog focus is not assumed broken and no custom focus trap was added without evidence.

## Test implementation and execution boundary

New tests use real production Room database/DAO code through `RoomComposerStore`, which now supports a test database name and explicit close. No Room stub, fake generated DAO or fake Android compilation is used for these regressions. Test session credentials are synthetic and confined to isolated Robolectric preferences. Model tests use a controlled Main dispatcher; the RealRepository seam test is explicitly distinguished from true Retrofit/MockWebServer tests.

Added free test dependencies: Robolectric 4.13, AndroidX test core 1.6.1, coroutines-test 1.8.1 and Compose UI test via the existing BOM; enable Android resources in JVM tests. Existing tests/assertions are retained except the documented incorrect scheduled label. Host UI tests use a manually launched ComponentActivity plus createEmptyComposeRule, so they do not require an emulator or a release-manifest test activity. Qualifiers are 390dp host width/1600dp height, with the actual content constrained to 320dp and explicit fontScale 1 or 2; these are layout/bounds tests, **not** a complete realistic handset viewport matrix. No screenshots or PNG renderer output was produced or inspected. Browser rendering was neither needed nor attempted.

Actual final-snapshot commands executed here:

- `python3 scripts/check-invariants.py` → `check-invariants: 0 error(s), 0 warning(s)`.
- `git diff --check` → clean.
- `python3 .verification/refresh-review-stack.py` → patch series applies to a fresh baseline snapshot and exactly reproduces all current changed/new Android files; no Git metadata or worktree code was modified by patch verification.
- Reviewed the diff of RepositoryTest: both correct failing test bodies/assertions remain untouched. Reviewed scheduled-label source/test changes.

**New/changed Gradle, Room/model, Compose host and wire tests have not been executed on this final snapshot.** Earlier auxiliary test counts are historical and do not validate these changes. The user explicitly directs coordinator execution because this sandbox denies sockets; no repeated denied Gradle or auxiliary fake compilation was run during continuation. A cached Material3 binary was not visible for separate local disassembly; the actual default-minimum-touch test is provided for coordinator execution instead of inventing a verified result.

Coordinator command (installed environment, outside restrictive launcher):

```sh
JAVA_HOME=/home/box/.local/jdk/current \
ANDROID_HOME=/home/box/.local/android-sdk \
GRADLE_USER_HOME=/home/box/.gradle \
/home/box/.gradle/wrapper/dists/gradle-8.7-bin/bhs2wmbdwecv87pi65oeuq5iu/gradle-8.7/bin/gradle \
-p android :data:test :app:testDebugUnitTest :app:testReleaseUnitTest \
:app:assembleDebug :app:assembleRelease :app:build :core-security:build --continue
```

To isolate host regressions for diagnostics, use `:app:testDebugUnitTest --tests 'dev.qh.agent.*Room*Test' --tests 'dev.qh.agent.RealRecoveryModelTest' --tests 'dev.qh.agent.ComposeReviewTest' --tests 'dev.qh.agent.NotificationPromptTest'`; this targeted run does not replace the full command. No assertion weakening, skips, lint suppression or gate bypass is authorized.

## Refreshed actual stack boundaries

Canonical `.verification/stack/manifest.json` contains current file lists, predecessor bases and patch hashes. Use only its three named candidates; older patch filenames left in the directory are superseded historical artifacts, not extra stack members.

| Candidate patch | Production added + removed lines | Base |
| --- | ---: | --- |
| 01-session-and-alarm-foundations.patch | 441 | baseline 3600c3ab4a1c499f099e39be6b8084b3351dd7b7 |
| 02-durable-recovery-and-ui-foundations.patch | 561 | committed candidate 01 head, to be made by coordinator |
| 03-complete-flow-integration.patch | 799 | committed candidate 02 head, to be made by coordinator |

The second candidate is a **real independently scoped foundation**: reusable callback-driven recovery/answer/reminder/input UI, shared accessible controls, Room/recovery/projection types and actual dialog extraction. It atomically moves baseline MainActivity's confirmation dialogs to the shared implementation and redirects existing calls, preserving the old working flow until candidate 03 switches to the full new model/UI. This means MainActivity deliberately changes in candidates 02 and 03; do not regenerate each patch against HEAD independently. No generated application code or omitted final UI is used to evade the line budget. Tests/docs are excluded, Gradle test configuration conservatively included.

Mechanical patch application is verified; independent Gradle builds/tests for each candidate are still pending. There are **no new commit ranges or SHAs**, because Git metadata remains read-only and commits are forbidden for this run. Coordinator must create the ordered commits, run actual checks, obtain Kimi k3.1-pro re-review, fix any real remaining P0/P1, then decide publication. No overall P1=0, green gate, rendered UI or hardware QA claim is made here.

## 2026-11-08 — actual wrapper failure follow-up (tests only)

Evidence read: `/workspace/qh-unified-evidence/implementation/final-full-gradle-wrapper.log` and `android/data/build/test-results/test/TEST-dev.qh.data.RecoveryWireReviewTest.xml`. This run genuinely ended **BUILD FAILED in 3m 28s, 200 actionable tasks: 200 executed**. Production Kotlin/Room generation, APK assembly and lint completed, but data tests reported 60 tests / one failure, and both app unit-test compilations failed. This supersedes any inference of an overall pass. The corrected tests below have not yet been compiled/run by Gradle.

Only four test source files changed in this follow-up; production Kotlin was frozen for independent Kimi re-review:

- `ComposeReviewTest.kt`: compute DpRect width/height from right-left/bottom-top. The original property access resolved to incompatible GestureScope extensions with the actual Compose API. Retains both visual >=48dp and actual semantics touch-bounds assertions, including large-font host tests.
- `LogoutRoomModelTest.kt`: bind `requireNotNull(state.recovery)` to a local before asserting immutable request and unknown state. Kotlin cannot smart-cast a public property from the data module; no Room/epoch assertion removed.
- `ReviewModelFixture.kt`: use `ResponseBody.create(null, "{}")`, supported by the resolved OkHttp API, for the same synthetic HTTP error body. No status or product behavior changed.
- `RecoveryWireReviewTest.kt`: replace the failed test's untyped FIFO/connection-loss setup with a method/path dispatcher and idempotent client-ID-keyed server objects. POST commits B, then sends a 202 body with a declared length exceeding transmitted bytes and closes the connection; this models response loss after acceptance without waiting for an empty FIFO queue. GET list always returns NotePage; GET server-ID returns Note. Unsupported requests get 404.

Wire-failure diagnosis: the XML proves `RealRepository.notes` received a decoded NotePage with null items (NPE at addAll), after a 60.092-second failing test. The old test queued DISCONNECT_AFTER_REQUEST on a reused connection, then only queued list/note responses after the IOException. It incorrectly assumed exactly one physical POST and a drained FIFO; an OkHttp retransmission/pending request can consume the subsequently queued list response, leaving a Note-shaped response for GET list. The XML contains no request trace, so the exact physical retry sequence cannot be claimed as observed. This malformed list was a fixture routing defect, not evidence that the actual server violates its list contract. The replacement dispatcher's recorded methods/paths and immutable POST bodies now make this class of mismatch visible.

Retained/strengthened assertions: new operation and client identities for explicit B copy; second prepare blocked; complete immutable request in every physical POST and durable unknown snapshot; server A retained and B stored exactly once by client ID, both distinct from server IDs; restart preserves the same B request/operation; authoritative list+GET resolves B without any additional POST; newer C text AND version survive; recovered state is live with B's actual server ID; final authoritative map contains exactly A and B. Physical transport attempts are counted separately from logical server objects. No existing RepositoryTest assertions changed, and no test was skipped or threshold weakened. No production defect requiring source modification was identified in this investigation.

Actual local checks after these edits:

- `python3 scripts/check-invariants.py`: `check-invariants: 0 error(s), 0 warning(s)`.
- `git diff --check`: exit 0, no output.
- `sha256sum -c /tmp/qh-production-before.sha256`: all production Kotlin files match their start-of-run hashes.
- `python3 .verification/refresh-review-stack.py`: all three ordered patches apply cleanly to a fresh baseline snapshot and reproduce every current Android file. Production budgets remain 441 / 561 / 799. Updated candidate 02 SHA256: `29d6c935664c921f93c061f021da61b864509c73c4c93a35bc5e2875fa8009d9`; candidate 03: `3c8b4944cee76823e58e10729816957f35dd46a75bda65793f647903b71527a0`. Candidate 01 unchanged. These are patch hashes, not commits or proof of independent Gradle passes.

Coordinator must rerun the real wrapper outside this sandbox:

```sh
JAVA_HOME=/home/box/.local/jdk/current \
ANDROID_HOME=/home/box/.local/android-sdk \
GRADLE_USER_HOME=/home/box/.gradle \
android/gradlew -p android :data:test :app:testDebugUnitTest :app:testReleaseUnitTest \
:app:assembleDebug :app:assembleRelease :app:build :core-security:build --continue
```

Remaining blocker: real compilation/execution of these corrected host/wire tests and the complete wrapper pass are pending coordinator execution. No Gradle, auxiliary compilation, screenshot rendering, emulator/device/TalkBack QA, commit or external action was performed in this follow-up. Independent review files were not edited.

## 2026-11-08 — Compose host lifecycle/recomposer correction

Read actual `final-full-gradle-after-test-fixes.log` and the full debug ComposeReviewTest XML. Data suite now passes and app host sources compile. Debug app reports 110 tests / five failures; the other 105 (including actual Room/model tests and two Compose tests) pass. All five failures occur at the initial content idle wait, including a bare Material3 TextButton, with suppressed `UnExecutedRunnablesException: Main looper has queued unexecuted runnables`. This evidence isolates host initialization/synchronization from a demonstrated product recomposition loop. Release was still running the previous compiled snapshot when this follow-up started; no release result is inferred.

Verified the cached Compose 1.6.8 AAR bytecode with installed JDK `javap` (inspection only, not compilation): AndroidComposeUiTestImpl.setContent invokes runOnUiThread, supplies the environment's own Recomposer to activity.setContent, registers a disposal hook, and then waits normally for idle. The previous harness instead used createEmptyComposeRule plus a direct activity.setContent with default composition context, and manually initialized a visible activity under PAUSED main-looper mode without draining posted startup/window traversal work. That bypassed the rule's explicit content/recomposer ownership and left queued host work. XML alone does not identify each queued Runnable; the exact runtime success of the correction still requires the real targeted run.

Exact source scope: **ComposeReviewTest.kt harness only**. Replaced the empty rule with the real AndroidComposeTestRule, supplied a real ComponentActivity via an ExternalResource/Robolectric ActivityController, moved lifecycle ownership into that rule, drained the paused Android main looper after activity startup and destruction, and call compose.setContent so the rule owns recomposer/clock/disposal. The controller avoids a debug-only manifest Activity dependency in release host tests. Existing compose.waitForIdle remains. No timeout changes, idling-resource bypass, autoAdvance disabling, manual layout fabrication, stub UI or assertion changes. All seven test bodies, >=48dp visual/touch checks, font-scale cases, real dialogs and real AgentApp/model paths remain intact.

Actual checks: invariant checker 0 errors/0 warnings; git diff --check clean; all production Kotlin hashes match start-of-run; refreshed three patches apply in order and exactly reproduce current Android files. Budgets remain 441/561/799. Candidate 03 patch SHA256 now `2b22918017aea094aac12955dffe98dc02496357c568f39f78f5cf7956cc3599`; 01/02 unchanged. Refresh script now explicitly compares against approved baseline 3600c3ab4a1c499f099e39be6b8084b3351dd7b7, because coordinator has already committed candidate 01 as observed HEAD daaa3e52f847fb73198ff00ab298741aef16f40f; using moving HEAD previously omitted its files and raised Missing source .gitignore. No Git mutation was made here.

Coordinator targeted command, outside socket-restricted sandbox:

```sh
JAVA_HOME=/home/box/.local/jdk/current \
ANDROID_HOME=/home/box/.local/android-sdk \
GRADLE_USER_HOME=/home/box/.gradle \
android/gradlew -p android :app:testDebugUnitTest :app:testReleaseUnitTest \
--tests 'dev.qh.agent.ComposeReviewTest' --continue
```

Then rerun the full wrapper command above. This harness correction is **not yet executed**; real build remains red until proven otherwise. No clearance/device/TalkBack claim. Kimi 09/11 untouched. Optional P2s (scrolling composer, local retry suitability, removal copy, physical QA) unchanged and deferred as explicitly directed.

## 2026-11-08 — consistent 320dp host window for dialog tests

Read actual `compose-harness-smoke.log`: targeted bare Material3 test BUILD SUCCESSFUL in 22s. Read ongoing `final-full-gradle-verified.log`: confirmDialog still reports initial AppNotIdleException on the previous compiled harness; no final suite pass inferred. Independently read public first-hand issue https://github.com/robolectric/robolectric/issues/8460, including the original Dialog+TextField qualifier reproduction and the September 2026 width matrix: w320dp passes, w340dp and wider examples loop; changing rule type does not resolve that separate issue. This is supporting environment evidence, not proof of our runtime cause or an instruction to bypass idling.

The old harness specified a 390dp host but constrained its root Box to 320dp. That was inconsistent with its explicitly intended 320dp/fontScale2 checks: separate platform dialog windows resolve sizing from the host, not the underlying Box's width. There was no assertion of 390dp content or actual host-window size. It therefore did not establish a coherent 390dp rendering matrix. The remaining dialog failure is consistent with the published qualified-window mismatch; production dialogs are untouched.

Changed only ComposeReviewTest harness: w390dp -> w320dp, retaining h1600dp/mdpi/sdk34/PAUSED mode, the real activity/Compose runner, existing clock/idle policies and all seven test bodies. Added three non-optional invariants on every content setup: configured screenWidthDp == 320, measured decorView.width / resource density == 320dp (0.01dp numeric measurement tolerance), and actual tagged Compose root width == 320dp. All prior >=48dp visual/touch assertions, fontScale2, dialog/date actions and real model/navigation paths are unchanged. No test deletion, skip, assertion reduction, timeout extension, idling bypass or production sizing workaround.

Coverage is now explicitly a **320dp host/content matrix**, including fontScale2 where previously specified. Genuine 390dp platform dialogs/device rendering, keyboard/IME, TalkBack and physical hardware remain **UNVERIFIED** and need a separate device/emulator matrix or a proven fixed Robolectric version; do not label these tests 390dp coverage. If the unchanged confirmDialog still fails, it remains a real build blocker for investigation; this edit has not been executed here.

Actual local verification: check-invariants reports 0 errors/0 warnings; git diff --check clean; production Kotlin hashes unchanged; automated textual comparison confirms every test body and existing assertion helper unchanged. Three refreshed patches apply sequentially and reproduce actual Android files; budgets 441/561/799. Candidate03 SHA256: `5e9551176f542fec155565599650d389dbd3b0e0d53a2aaf148087d2700ae6a3`; candidates01/02 unchanged. Independent 09/11 reports untouched.

Coordinator targeted real command, then full suite as already recorded:

```sh
JAVA_HOME=/home/box/.local/jdk/current \
ANDROID_HOME=/home/box/.local/android-sdk \
GRADLE_USER_HOME=/home/box/.gradle \
android/gradlew -p android :app:testDebugUnitTest \
--tests 'dev.qh.agent.ComposeReviewTest.confirmDialogUsesActual48dpActionsAtLargeFont'
```

No sandbox Gradle, external write, commit or production change performed. Existing optional P2 dispositions remain deferred.

## 2026-11-08 — real date/time dialog action-space defect

Evidence: actual `compose-confirm-dialog-window-smoke.log` completed in 23s with one failing assertion; XML reports `Actual height is 0.0.dp, expected at least 48.0.dp` for 使用所选本地时间 after opening the picker. The 320dp window corrected the idle freeze sufficiently to expose a real layout failure; this run was not a pass. The obsolete390dp full run was cancelled (coordinator reports exit143), not a successful check.

Independently inspected the **installed Material3 1.2.1 AAR** using JDK javap, not a substituted UI: DatePickerDialog$1 applies the DatePickerModalTokens container-height maximum; DatePickerDialog$1$1 builds a Column that invokes supplied content before adding its unweighted action container. Our body was an unweighted Column with heightIn(max600dp), containing the date picker AND time input/zone. It could consume the parent's whole capped height, leaving the later action row zero height. The semantics assertion already synchronizes layout, and this measurement/order defect requires product sizing correction, not another idle wait.

Only production change: **ReminderDialogs.kt**, in candidate02. The picker body now has ColumnScope.weight(1f, fill=false) before its existing height cap and vertical scrolling. Compose measures unweighted confirm/dismiss actions first and allocates remaining constrained height to the scrollable body. The primary confirmation dialog's text body is also vertically scrollable so long copy/title fields cannot force its actions off a short large-font screen. No platform date/time controls, callbacks, future-time validation, IDs, scheduling or server contracts changed.

Regression: original confirmDialogUsesActual48dpActionsAtLargeFont retains the same layout/action checks through a shared scenario, expanded to assert displayed >=48dp confirm/dismiss controls, exercise picker cancellation (no selection/confirmation side effect), reopen, actually scroll the date/time body to its zone label, confirm the chosen future instant, and then invoke the real outer confirmation callback exactly once. Added the same actual scenario at w320dp-h640dp-mdpi/fontScale2, alongside existing h1600dp coverage. Picker footer is never scrolled into view to manufacture action reachability: it must remain displayed before/after body scrolling. Existing 48dp thresholds/assertions retained, no skips/timeouts/idling bypasses. Both scenarios still require coordinator execution.

Actual local evidence: check-invariants 0 errors/0 warnings; git diff --check clean; start/end production hashes differ **only ReminderDialogs.kt** as authorized. All three refreshed patches apply from explicit3600 baseline and reproduce actual Android files. Budgets: candidate01=441 (unchanged), candidate02=563 (was561), candidate03=799 (unchanged). Candidate02 patch SHA256 `99a4aee71f9302e6d146cf8371b4af24c938cf1c92a1fb61604104827aa4b67b`; candidate03 test-expanded patch SHA256 `525eb22e7f961cd8d5756f9562d486abe8e4560b894b12e889d75d5c40e1970b`. Source fix belongs entirely to02;03 changes only its tests in this follow-up. Observed local HEAD309a0be7c3dd4ba42d812d48c0e8a7e5d991fdce is coordinator's candidate02, not a commit made here. No history changes here; coordinator may handle its unpushed candidate safely.

Coordinator fast command (same genuine target plus short-phone regression), then real full wrapper:

```sh
JAVA_HOME=/home/box/.local/jdk/current \
ANDROID_HOME=/home/box/.local/android-sdk \
GRADLE_USER_HOME=/home/box/.gradle \
android/gradlew -p android :app:testDebugUnitTest \
--tests 'dev.qh.agent.ComposeReviewTest.confirmDialogUsesActual48dpActionsAtLargeFont' \
--tests 'dev.qh.agent.ComposeReviewTest.confirmDialogActionsRemainReachableOnShortPhoneAtLargeFont'
```

No Gradle execution in this sandbox, pass claim, real screenshot/device/390dp QA, or external action. Independent09/11 unchanged; other optionalP2 work untouched. The new product fix requires real tests and independent review; previous source review cannot be presented as clearance of this new diff.
