import Foundation
import Combine

/// 本机对话状态。只读写本机文件，不持有、不调用任何网络或云端会话。
@MainActor
final class LocalModel: ObservableObject {
    @Published private(set) var document = LocalDocument()
    @Published private(set) var problem: LocalStoreProblem?
    @Published private(set) var actionError: String?
    @Published private(set) var draftUnsaved = false
    @Published var draft = "" {
        didSet { if draft != oldValue { scheduleDraftSave() } }
    }

    private let store: LocalStore?
    private var draftTask: Task<Void, Never>?

    init() {
        if let url = try? LocalStore.defaultFileURL() {
            let store = LocalStore(fileURL: url)
            #if DEBUG
            store.failWritesForTesting = ProcessInfo.processInfo.arguments.contains("-PAFailLocalWrites")
            #endif
            self.store = store
        } else {
            store = nil
        }
        syncFromStore()
        draft = store?.document.draft ?? ""
    }

    var isReadOnly: Bool { problem != nil }
    var messages: [LocalItem] { document.items.filter { $0.kind == .message } }
    func items(_ kind: LocalItemKind) -> [LocalItem] { document.items.filter { $0.kind == kind } }

    func send() -> Bool {
        let ok = apply { document in
            try document.add(.message, text: draft, now: Date())
            document.draft = ""
        }
        if ok { draft = "" }
        return ok
    }

    func save(_ message: LocalItem, as kind: LocalItemKind) -> Bool {
        apply { try $0.save(messageID: message.id, as: kind, now: Date()) }
    }

    func add(_ kind: LocalItemKind, text: String) -> Bool {
        apply { try $0.add(kind, text: text, now: Date()) }
    }

    func edit(_ id: UUID, text: String) -> Bool {
        apply { try $0.edit(id: id, text: text, now: Date()) }
    }

    func toggleDone(_ item: LocalItem) -> Bool {
        apply { try $0.setDone(id: item.id, !item.isDone, now: Date()) }
    }

    func remove(_ item: LocalItem) -> Bool {
        apply { try $0.remove(id: item.id) }
    }

    func dismissError() { actionError = nil }

    /// 只读状态下由用户触发重读（例如设备解锁后）。重读成功前任何写入都被 LocalStore 拒绝；
    /// 成功后既不丢磁盘上的旧草稿，也不丢只读期间输入的新草稿（两者不同则合并，旧在前）。
    func reload() {
        store?.reload()
        syncFromStore()
        guard problem == nil else { return }
        let stored = document.draft
        if draft.isEmpty {
            draft = stored
        } else if !stored.isEmpty && !draft.hasPrefix(stored) {
            draft = stored + "\n" + draft
        }
    }

    /// 离开前台时立即落盘草稿；失败会在输入栏下方提示。
    func saveDraftNow() {
        draftTask?.cancel()
        draftTask = nil
        guard let store else { return }
        guard store.problem == nil else { draftUnsaved = !draft.isEmpty; return }
        guard store.document.draft != draft else { draftUnsaved = false; return }
        var candidate = store.document
        candidate.draft = draft
        do {
            try store.commit(candidate)
            document = store.document
            draftUnsaved = false
        } catch {
            draftUnsaved = true
        }
    }

    /// 每次修改：在副本上变更 → 写盘成功才更新界面；失败则界面保持写盘前的真实状态。
    private func apply(_ change: (inout LocalDocument) throws -> Void) -> Bool {
        guard let store else {
            actionError = Self.message(for: LocalStoreProblem.unavailable)
            return false
        }
        var candidate = store.document
        candidate.draft = draft
        do {
            try change(&candidate)
            try store.commit(candidate)
        } catch let error as LocalEditError {
            actionError = Self.message(for: error)
            return false
        } catch let error as LocalStoreProblem {
            actionError = error == .writeFailed ? Self.message(for: error) : "本机存储处于只读状态，本次操作没有保存。"
            return false
        } catch {
            actionError = "本机操作失败，没有保存。"
            return false
        }
        actionError = nil
        draftUnsaved = false
        syncFromStore()
        return true
    }

    private func scheduleDraftSave() {
        draftTask?.cancel()
        draftTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 600_000_000)
            guard !Task.isCancelled else { return }
            self?.saveDraftNow()
        }
    }

    private func syncFromStore() {
        guard let store else {
            document = LocalDocument()
            problem = .unavailable
            return
        }
        document = store.document
        problem = store.problem
    }

    nonisolated static func message(for error: LocalEditError) -> String {
        switch error {
        case .empty: "内容不能为空。"
        case .tooLong: "最多 \(LocalDocument.maxTextLength) 字，请精简后再保存。"
        case .notFound: "这条内容已不存在，没有保存。"
        case .alreadySaved: "已经存过了。"
        case .invalidKind: "这类内容不支持此操作。"
        }
    }

    nonisolated static func message(for problem: LocalStoreProblem) -> String {
        switch problem {
        case .unavailable:
            "无法定位本机存储位置，内容不会被保存。"
        case .unreadable:
            "本机数据暂时无法读取（设备可能尚未解锁）。为避免覆盖，已暂停保存。请稍后点「重新读取」。"
        case .corrupt:
            "本机数据文件无法解析。为避免覆盖，App 已停止保存，也不会删除该文件。请勿卸载 App，并联系开发者协助恢复。"
        case .futureVersion(let version):
            "本机数据来自更新版本的 App（格式 v\(version)）。为避免覆盖，已停止保存。请升级 App 后再打开。"
        case .writeFailed:
            "未能写入本机，本次操作没有保存。"
        }
    }
}
