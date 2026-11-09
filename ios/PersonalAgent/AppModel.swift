import Foundation
import Combine

enum InputMode: String {
    case note, ask

    var title: String { self == .note ? "记下" : "提问" }
    var placeholder: String { self == .note ? "写一件想留下的事…" : "问问你的笔记…" }
    var maximumLength: Int { self == .note ? 5000 : 2000 }
}

enum Activity {
    case loggingIn, loadingNotes, savingNote, asking, loggingOut

    var label: String {
        switch self {
        case .loggingIn: "正在登录…"
        case .loadingNotes: "正在读取笔记…"
        case .savingNote: "正在保存原文…"
        case .asking: "正在查找记忆并回答…"
        case .loggingOut: "正在退出…"
        }
    }
}

struct PendingNote: Identifiable {
    let id: String
    let text: String
    var serverID: String? = nil
    var failure: String? = nil
    var isRejected = false
}

struct AskItem: Identifiable {
    let id = UUID()
    let question: String
    let createdAt = Date()
    var answer: String? = nil
    var citations: [Citation] = []
    var isAsking = true
    var errorMessage: String? = nil
}

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var isAuthenticated = false
    @Published private(set) var notes: [Note] = []
    @Published private(set) var askItems: [AskItem] = []
    @Published private(set) var pendingNote: PendingNote?
    @Published private(set) var activity: Activity?
    @Published private(set) var hasLoadedNotes = false
    @Published private(set) var loginError: String?
    @Published private(set) var notesError: String?
    @Published private(set) var composerError: String?
    @Published private(set) var isDemo = false
    @Published var mode = InputMode.note { didSet { composerError = nil } }
    @Published var draft = "" { didSet { composerError = nil } }

    private let api = APIClient()
    private let defaults = UserDefaults.standard
    private let tokenKey = "verification.tokens"
    private let deviceKey = "verification.deviceID"
    private var tokens: Tokens?
    private var sessionGeneration = 0

    init() {
        if let data = defaults.data(forKey: tokenKey) {
            tokens = try? JSONDecoder().decode(Tokens.self, from: data)
            isAuthenticated = tokens != nil
        }
    }

    private init(previewAuthenticated: Bool) {
        isDemo = true
        isAuthenticated = previewAuthenticated
        hasLoadedNotes = previewAuthenticated
    }

    private var deviceID: String {
        if let value = defaults.string(forKey: deviceKey) { return value }
        let value = UUID().uuidString
        defaults.set(value, forKey: deviceKey)
        return value
    }

    func login(inviteCode: String) async {
        let code = inviteCode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard activity == nil, !code.isEmpty else { return }
        activity = .loggingIn
        loginError = nil
        defer { activity = nil }
        do {
            let data = try await api.request("v1/auth/login", method: "POST", body: [
                "invite_code": code, "device_id": deviceID,
                "device_name": "随记 iOS", "platform": "ios"
            ])
            let value = try JSONDecoder().decode(Tokens.self, from: data)
            defaults.set(try JSONEncoder().encode(value), forKey: tokenKey)
            sessionGeneration += 1
            tokens = value
            hasLoadedNotes = false
            isAuthenticated = true
        } catch let error as APIError {
            loginError = error.isUnauthorized ? "邀请码无效或已过期，请重新输入" : error.message
        } catch {
            loginError = "登录信息无法读取，请稍后重试"
        }
    }

    func loadNotes() async {
        guard activity == nil, let token = tokens?.access_token else { return }
        let generation = sessionGeneration
        activity = .loadingNotes
        notesError = nil
        defer { if generation == sessionGeneration { activity = nil } }
        do {
            let data = try await api.request("v1/notes?limit=50", token: token)
            let items = try JSONDecoder().decode(NotesResponse.self, from: data).items
            guard generation == sessionGeneration else { return }
            notes = items.sorted { $0.recordedDate > $1.recordedDate }
            hasLoadedNotes = true
        } catch {
            guard generation == sessionGeneration, !expireIfNeeded(error) else { return }
            notesError = presentable(error)
        }
    }

    func submit() async {
        guard activity == nil else { return }
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { composerError = "先写一点内容"; return }
        guard text.count <= mode.maximumLength else {
            composerError = "\(mode.title)最多 \(mode.maximumLength) 字，请精简后再发送"
            return
        }

        switch mode {
        case .note:
            guard pendingNote == nil else {
                composerError = "请先处理上方未完成的记事"
                return
            }
            let pending = PendingNote(id: UUID().uuidString, text: text)
            draft = ""
            await saveNote(pending)
        case .ask:
            let item = AskItem(question: text)
            askItems.insert(item, at: 0)
            draft = ""
            await performAsk(id: item.id, question: text)
        }
    }

    func retryPendingNote() async {
        guard activity == nil, let pending = pendingNote, !pending.isRejected else { return }
        await saveNote(pending)
    }

    func takeBackPendingNote() {
        guard activity == nil, let pending = pendingNote,
              pending.serverID == nil, pending.isRejected else { return }
        mode = .note
        draft = pending.text
        pendingNote = nil
    }

    private func saveNote(_ pending: PendingNote) async {
        guard let token = tokens?.access_token else { return }
        let generation = sessionGeneration
        activity = .savingNote
        pendingNote = pending
        pendingNote?.failure = nil
        pendingNote?.isRejected = false
        defer { if generation == sessionGeneration { activity = nil } }

        do {
            if pendingNote?.serverID == nil {
                let body = [
                    "client_note_id": pending.id, "text": pending.text,
                    "recorded_at": Date().iso8601String,
                    "timezone": TimeZone.current.identifier, "source": "app"
                ]
                let data = try await api.request("v1/notes", method: "POST", token: token, body: body)
                let created = try JSONDecoder().decode(CreateNoteResponse.self, from: data)
                guard generation == sessionGeneration else { return }
                pendingNote?.serverID = created.note_id
            }
            guard generation == sessionGeneration, let id = pendingNote?.serverID else { return }
            let data = try await api.request("v1/notes/\(id)", token: token)
            let note = try JSONDecoder().decode(Note.self, from: data)
            guard generation == sessionGeneration else { return }
            notes.removeAll { $0.id == note.id }
            notes.insert(note, at: 0)
            notes.sort { $0.recordedDate > $1.recordedDate }
            pendingNote = nil
        } catch {
            guard generation == sessionGeneration, !expireIfNeeded(error) else { return }
            if pendingNote?.serverID != nil {
                pendingNote?.failure = "服务端已接收原文，但还没读取到笔记。重试只会重新读取，不会再次创建。"
            } else if let apiError = error as? APIError, apiError.isDefiniteRejection {
                pendingNote?.failure = apiError.message
                pendingNote?.isRejected = true
            } else {
                pendingNote?.failure = "\(presentable(error))。保存结果未确认，服务器可能已接收；重试会沿用同一条创建标识。"
            }
        }
    }

    func retryAsk(id: UUID) async {
        guard activity == nil, let item = askItems.first(where: { $0.id == id }) else { return }
        await performAsk(id: id, question: item.question)
    }

    private func performAsk(id: UUID, question: String) async {
        guard let token = tokens?.access_token else { return }
        let generation = sessionGeneration
        activity = .asking
        updateAsk(id: id) { $0.isAsking = true; $0.errorMessage = nil }
        defer { if generation == sessionGeneration { activity = nil } }
        do {
            let data = try await api.request("v1/ask", method: "POST", token: token, body: [
                "text": question, "client_time": Date().iso8601String,
                "timezone": TimeZone.current.identifier
            ])
            let response = try JSONDecoder().decode(AskResponse.self, from: data)
            guard generation == sessionGeneration else { return }
            updateAsk(id: id) {
                $0.answer = response.answer
                $0.citations = response.citations
                $0.isAsking = false
            }
        } catch {
            guard generation == sessionGeneration, !expireIfNeeded(error) else { return }
            let message = presentable(error)
            updateAsk(id: id) { $0.isAsking = false; $0.errorMessage = message }
        }
    }

    private func updateAsk(id: UUID, _ update: (inout AskItem) -> Void) {
        guard let index = askItems.firstIndex(where: { $0.id == id }) else { return }
        update(&askItems[index])
    }

    func logout() async {
        guard activity == nil else { return }
        activity = .loggingOut
        var notice: String?
        if let token = tokens?.access_token {
            do {
                _ = try await api.request("v1/auth/logout", method: "POST", token: token)
            } catch {
                notice = "本机已退出；服务端登出未成功，请检查网络连接"
            }
        }
        clearSession()
        loginError = notice
        activity = nil
    }

    private func expireIfNeeded(_ error: Error) -> Bool {
        guard let apiError = error as? APIError, apiError.isUnauthorized else { return false }
        clearSession()
        loginError = "登录已失效，请重新登录"
        return true
    }

    private func clearSession() {
        sessionGeneration += 1
        defaults.removeObject(forKey: tokenKey)
        tokens = nil
        isAuthenticated = false
        notes = []
        askItems = []
        pendingNote = nil
        draft = ""
        mode = .note
        hasLoadedNotes = false
        notesError = nil
        composerError = nil
        activity = nil
    }

    private func presentable(_ error: Error) -> String {
        (error as? APIError)?.message ?? "服务返回内容无法读取，请稍后重试"
    }
}

#if DEBUG
extension AppModel {
    static func previewLogin() -> AppModel { AppModel(previewAuthenticated: false) }
    static func previewEmpty() -> AppModel { AppModel(previewAuthenticated: true) }

    static func previewContent() -> AppModel {
        let model = AppModel(previewAuthenticated: true)
        model.notes = [Note(
            id: "sample-note", title: "周末去植物园", text: "周六上午去植物园，记得带上相机和水瓶。",
            status: "organized", recorded_at: "2026-11-09T09:30:00+08:00",
            tags: ["周末"], todos: [NoteTodo(text: "带上相机和水瓶")]
        )]
        model.askItems = [AskItem(
            question: "周末出门要准备什么？",
            answer: "你记下要带相机和水瓶。",
            citations: [Citation(memory_id: "sample-memory", note_id: "sample-note", text: "带上相机和水瓶")],
            isAsking: false
        )]
        return model
    }
}
#endif
