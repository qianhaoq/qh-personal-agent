// LocalStore 自测（作者自测，非独立评审）。不属于 App target。
// 在 Mac（或装有 Swift 的 Linux）仓库根目录运行：
//   swiftc ios/PersonalAgent/LocalStore.swift ios/Tests/LocalStoreSelfTest/main.swift -o /tmp/local-store-selftest && /tmp/local-store-selftest
import Foundation

var failures = 0
func check(_ condition: @autoclosure () -> Bool, _ name: String) {
    if condition() { print("ok   \(name)") } else { failures += 1; print("FAIL \(name)") }
}
func expectThrows<E: Error & Equatable>(_ expected: E, _ name: String, _ body: () throws -> Void) {
    do { try body(); check(false, name) } catch let error as E { check(error == expected, name) } catch { check(false, name) }
}

let root = FileManager.default.temporaryDirectory.appendingPathComponent("local-store-selftest-\(UUID().uuidString)")
try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
func fileURL(_ name: String) -> URL { root.appendingPathComponent(name).appendingPathComponent("local-store.json") }
let now = Date(timeIntervalSinceReferenceDate: 700_000_000)

// 1. 首次使用：无文件 → 空文档、可写、不创建文件
let firstURL = fileURL("first")
let first = LocalStore(fileURL: firstURL)
check(first.problem == nil && first.document == LocalDocument(), "missing file loads empty and writable")
check(!FileManager.default.fileExists(atPath: firstURL.path), "loading does not create a file")

// 2. 写入 → 新实例读回一致（任务、记忆、完成状态、草稿）
var doc = first.document
let message = try doc.add(.message, text: "  周六带相机  ", now: now)
check(message.text == "周六带相机", "text is trimmed")
try doc.save(messageID: message.id, as: .task, now: now)
try doc.save(messageID: message.id, as: .memory, now: now)
let task = doc.derived(from: message.id, kind: .task)!
try doc.setDone(id: task.id, true, now: now)
doc.draft = "未发送的草稿"
try first.commit(doc)
let reread = LocalStore(fileURL: firstURL)
check(reread.problem == nil && reread.document == doc, "round trip preserves items, done state and draft")

// 3. 写失败：抛错、内存与磁盘都保持旧值
let before = try Data(contentsOf: firstURL)
reread.failWritesForTesting = true
var changed = reread.document
try changed.remove(id: task.id)
expectThrows(LocalStoreProblem.writeFailed, "failed write throws writeFailed") { try reread.commit(changed) }
check(reread.document == doc, "failed write leaves in-memory document unchanged")
check((try? Data(contentsOf: firstURL)) == before, "failed write leaves file unchanged")

// 4. 损坏文件：只读、拒绝写、原字节不动
let corruptURL = fileURL("corrupt")
try FileManager.default.createDirectory(at: corruptURL.deletingLastPathComponent(), withIntermediateDirectories: true)
let garbage = Data("{not json".utf8)
try garbage.write(to: corruptURL)
let corrupt = LocalStore(fileURL: corruptURL)
check(corrupt.problem == .corrupt && corrupt.document.items.isEmpty, "corrupt file enters read-only")
expectThrows(LocalStoreProblem.corrupt, "corrupt store refuses writes") { try corrupt.commit(doc) }
check((try? Data(contentsOf: corruptURL)) == garbage, "corrupt file bytes are never overwritten")

// 5. 未来版本：只读、拒绝写、原字节不动
let futureURL = fileURL("future")
try FileManager.default.createDirectory(at: futureURL.deletingLastPathComponent(), withIntermediateDirectories: true)
let futureBytes = Data(#"{"version":2,"items":[],"draft":"","newField":true}"#.utf8)
try futureBytes.write(to: futureURL)
let future = LocalStore(fileURL: futureURL)
check(future.problem == .futureVersion(2), "future version enters read-only")
expectThrows(LocalStoreProblem.futureVersion(2), "future store refuses writes") { try future.commit(doc) }
check((try? Data(contentsOf: futureURL)) == futureBytes, "future-version bytes are never overwritten")

// 6. 无法读取（路径是目录）：只读而非当作空数据覆盖
let dirURL = fileURL("unreadable")
try FileManager.default.createDirectory(at: dirURL, withIntermediateDirectories: true)
check(LocalStore(fileURL: dirURL).problem == .unreadable, "unreadable existing path enters read-only")

// 7. 校验与操作语义
var rules = LocalDocument()
expectThrows(LocalEditError.empty, "whitespace-only text rejected") { try rules.add(.task, text: " \n ", now: now) }
expectThrows(LocalEditError.tooLong, "over-limit text rejected") {
    try rules.add(.task, text: String(repeating: "字", count: LocalDocument.maxTextLength + 1), now: now)
}
let limit = try rules.add(.memory, text: String(repeating: "字", count: LocalDocument.maxTextLength), now: now)
expectThrows(LocalEditError.invalidKind, "memories cannot be completed") { try rules.setDone(id: limit.id, true, now: now) }
expectThrows(LocalEditError.notFound, "editing a missing item fails") { try rules.edit(id: UUID(), text: "x", now: now) }
var twice = doc
expectThrows(LocalEditError.alreadySaved, "saving the same message twice as task fails") {
    try twice.save(messageID: message.id, as: .task, now: now)
}
try twice.remove(id: message.id)
check(twice.derived(from: message.id, kind: .memory) != nil, "deleting a message keeps derived memory")

// 8. 逐字查找
check(LocalSearch.matches("Buy MILK today", query: "milk"), "search is case-insensitive")
check(LocalSearch.matches("周六带相机", query: " 相机 "), "search trims query")
check(!LocalSearch.matches("周六带相机", query: "   "), "blank query matches nothing")
check(!LocalSearch.matches("周六带相机", query: "照相"), "search is literal, not semantic")

// 10. P1-01 回归：父目录不可遍历时绝不能当作「首次使用」，恢复访问后也不得在显式重读前覆盖旧库。
// 须以非 root 用户运行；若权限拒绝无法复现（root / ACL），计为失败并打印 UNSUPPORTED，不静默通过。
func lockedParentRegression() throws {
    let lockedDir = root.appendingPathComponent("locked-parent")
    let lockedURL = lockedDir.appendingPathComponent("local-store.json")
    let seed = LocalStore(fileURL: lockedURL)
    var seedDoc = seed.document
    try seedDoc.add(.memory, text: "旧记忆", now: now)
    seedDoc.draft = "旧草稿"
    try seed.commit(seedDoc)
    let originalBytes = try Data(contentsOf: lockedURL)

    try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: lockedDir.path)
    var restored = false
    func restore() {
        guard !restored else { return }
        restored = true
        try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: lockedDir.path)
    }
    defer { restore() }  // 断言失败或抛错时也恢复权限，保证临时目录可清理

    guard (try? Data(contentsOf: lockedURL)) == nil else {
        check(false, "UNSUPPORTED: permission denial not reproducible here (running as root or ACL override); rerun as a normal user")
        return
    }
    let blocked = LocalStore(fileURL: lockedURL)
    check(blocked.problem == .unreadable, "untraversable parent enters read-only, not first launch")
    check(blocked.document.items.isEmpty, "read-only store exposes no fabricated data")

    restore()  // 访问恢复，但没有显式重读
    var attempt = blocked.document
    attempt.draft = "新草稿"
    expectThrows(LocalStoreProblem.unreadable, "draft write before explicit reread is refused") {
        try blocked.commit(attempt)
    }
    check((try? Data(contentsOf: lockedURL)) == originalBytes, "original bytes unchanged after refused write")

    blocked.reload()
    check(blocked.problem == nil, "explicit reread after access restored clears read-only")
    check(blocked.document.items.map(\.text) == ["旧记忆"] && blocked.document.draft == "旧草稿",
          "explicit reread recovers the old items and draft")
    var next = blocked.document
    next.draft = "恢复后草稿"
    try blocked.commit(next)
    let reopened = LocalStore(fileURL: lockedURL)
    check(reopened.document.items.map(\.text) == ["旧记忆"] && reopened.document.draft == "恢复后草稿",
          "writes after reread keep old items")
}
do { try lockedParentRegression() } catch { check(false, "locked-parent regression threw \(type(of: error))") }

try? FileManager.default.removeItem(at: root)  // 只删本次自测创建的临时目录
print(failures == 0 ? "ALL PASSED" : "\(failures) FAILED")
exit(failures == 0 ? 0 : 1)
