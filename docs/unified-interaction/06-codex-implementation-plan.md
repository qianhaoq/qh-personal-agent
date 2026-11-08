# Codex implementation plan

Baseline: `3600c3ab4a1c499f099e39be6b8084b3351dd7b7`, existing branch/worktree only. Inputs: actual Kimi 01/03/prototype and independent 02/04; prototype's four P2 classes are limitations, not app behavior. No server changes, publishing or external messages.

Acceptance paths: explicit record/text or ASR → durable immutable A → create → authoritative GET → organized note → suggested reminder → explicit confirm → independent local scheduling result; ask → current-process answer → source detail → manual replay/stop. Preserve edit/delete/search, all reminder states/actions, notification confirmation and boot behavior. Busy permits draft B editing, never implicit queueing. Unknown A remains separate from B across restart; reconcile by client ID, never synthesize server content.

Stack candidates (each based on previous candidate, independently buildable, not one final aggregate PR):
1. Session authorization: data RequestOwner/SessionDescriptor, metadata-only TokenStore contract, credential-before-use guards including refresh/logout, SecureTokens implementation and race tests. Estimate 250–450 production changed lines.
2. Durable composer/create recovery: app private Room draft/recovery transactions, complete immutable snapshots, authoritative reconciliation, model integration and distinct-ID/recovery tests. Estimate 500–750 production changed lines.
3. Honest alarm submission: typed scheduling, true exact/inexact/failed/expired results, mutation/scheduler/refresh boundaries and local retry, permission separation and fault tests. Estimate 250–450 production changed lines.
4. Unified personal flow and audio/projection guards: Kimi palette/hierarchy and shared explicit-mode composer; retain detail/search/reminder operations, source navigation; immutable voice attempt, audio arbitration, projection sequencing and UI tests. Estimate 600–800 production changed lines. If integration exceeds 800, split internal guards from UI switch, retaining old working UI until complete.

Exact ranges will be recorded after commits; coordinator must publish each safe range as a stacked PR with its predecessor as base. Commits alone do not authorize a giant aggregate PR. Full cohesive release requires all stacks and cross-review, not merely first stack completion.

Inspection: current RealRepository reads tokens immediately inside mutex without owner; model's mutable pending changes identity on edit and fabricates server note; scheduler returns Unit with silent no-op/unchecked fallback; confirm wraps POST/scheduler/refresh together. Existing refreshMutex and alarm consume fences must stay. Server creates distinct `nte` IDs and returns existing deleted identity on idempotent replay; GET rejects deleted, Note serialization includes client_note_id.

Verification: existing Android JVM/app regressions plus new controllable session races, distinct-ID MockWebServer recovery, scheduler fault injection/audio guards; full assembleDebug/build/test and invariant checker. Environment discovery currently finds no java on PATH, no /usr/lib/jvm. Do not read local.properties, env, keys or actual storage for discovery. Missing tooling is a recorded verification blocker, never a pass. No emulator/device evidence currently available.

## Actual stack adjustment and blockers

Implementation yielded three disjoint-file candidates: safety/session/alarm foundations (445 production changed lines), durable recovery/projection/audio capture types (178), and final model/UI integration (739). The status composable is an unused foundation until the third candidate; final user-visible switching stays together. Each is below ~800; aggregate is ~1362 and must not become one PR. Exact file manifests and patches are `.verification/stack/manifest.json` and adjacent patches. Git metadata is mounted read-only, so no commits/ranges could be created; report 07 records the exact failure and required coordinator bases. Candidates are not independently Gradle-verified because sockets are prohibited and Room dependencies unavailable; do not infer safe publication from line budgets or patch boundaries.
