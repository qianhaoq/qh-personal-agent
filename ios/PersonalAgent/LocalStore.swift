import Foundation

// 本机对话、任务与记忆的持久层：只依赖 Foundation，不含网络、不含 UI。
// 可在 Xcode 之外用 swiftc 编译自测（见 ios/Tests/LocalStoreSelfTest/main.swift）。

enum LocalItemKind: String, Codable, CaseIterable, Identifiable {
    case message, task, memory

    var id: String { rawValue }
    var title: String {
        switch self {
        case .message: "对话"
        case .task: "任务"
        case .memory: "记忆"
        }
    }
}

struct LocalItem: Codable, Identifiable, Equatable {
    let id: UUID
    let kind: LocalItemKind
    var text: String
    var isDone: Bool
    let createdAt: Date
    var updatedAt: Date
    /// 由对话消息转存而来时，指向来源消息。
    var sourceID: UUID?
}

enum LocalEditError: Error, Equatable {
    case empty, tooLong, notFound, alreadySaved, invalidKind
}

/// 读写问题。除 writeFailed 外，出现即进入只读：绝不覆盖现有文件。
enum LocalStoreProblem: Error, Equatable {
    case unavailable, unreadable, corrupt, futureVersion(Int), writeFailed
}

struct LocalDocument: Codable, Equatable {
    static let currentVersion = 1
    static let maxTextLength = 5000

    var version = LocalDocument.currentVersion
    var items: [LocalItem] = []
    var draft = ""

    static func validated(_ raw: String) throws -> String {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { throw LocalEditError.empty }
        guard text.count <= maxTextLength else { throw LocalEditError.tooLong }
        return text
    }

    func derived(from messageID: UUID, kind: LocalItemKind) -> LocalItem? {
        items.first { $0.sourceID == messageID && $0.kind == kind }
    }

    @discardableResult
    mutating func add(_ kind: LocalItemKind, text raw: String, source: UUID? = nil, now: Date) throws -> LocalItem {
        let item = LocalItem(id: UUID(), kind: kind, text: try Self.validated(raw), isDone: false,
                             createdAt: now, updatedAt: now, sourceID: source)
        items.append(item)
        return item
    }

    mutating func save(messageID: UUID, as kind: LocalItemKind, now: Date) throws {
        guard kind != .message else { throw LocalEditError.invalidKind }
        guard let message = items.first(where: { $0.id == messageID && $0.kind == .message }) else {
            throw LocalEditError.notFound
        }
        guard derived(from: messageID, kind: kind) == nil else { throw LocalEditError.alreadySaved }
        try add(kind, text: message.text, source: messageID, now: now)
    }

    mutating func edit(id: UUID, text raw: String, now: Date) throws {
        guard let index = items.firstIndex(where: { $0.id == id }) else { throw LocalEditError.notFound }
        items[index].text = try Self.validated(raw)
        items[index].updatedAt = now
    }

    mutating func setDone(id: UUID, _ done: Bool, now: Date) throws {
        guard let index = items.firstIndex(where: { $0.id == id }) else { throw LocalEditError.notFound }
        guard items[index].kind == .task else { throw LocalEditError.invalidKind }
        items[index].isDone = done
        items[index].updatedAt = now
    }

    mutating func remove(id: UUID) throws {
        guard let index = items.firstIndex(where: { $0.id == id }) else { throw LocalEditError.notFound }
        items.remove(at: index)
    }
}

/// 本机逐字查找：包含即匹配（忽略大小写与全/半角），不做语义理解。
enum LocalSearch {
    static func matches(_ text: String, query: String) -> Bool {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return false }
        return text.range(of: needle, options: [.caseInsensitive, .widthInsensitive]) != nil
    }
}

/// 单文件 JSON 存储。非线程安全：只由 @MainActor 的 LocalModel 调用。
/// 写入先编码到临时文件再原子替换；失败时内存副本保持旧值（调用方据此回滚）。
final class LocalStore {
    let fileURL: URL
    private(set) var document = LocalDocument()
    private(set) var problem: LocalStoreProblem?
    var failWritesForTesting = false

    #if os(iOS)
    private static let writeOptions: Data.WritingOptions = [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
    #else
    private static let writeOptions: Data.WritingOptions = [.atomic]
    #endif

    init(fileURL: URL) {
        self.fileURL = fileURL
        reload()
    }

    static func defaultFileURL() throws -> URL {
        try FileManager.default
            .url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("LocalAssistant", isDirectory: true)
            .appendingPathComponent("local-store.json", isDirectory: false)
    }

    /// 只有读取错误本身明确是「文件不存在」才视为首次使用（不创建文件）；
    /// 权限、文件保护、父目录不可遍历及其他 IO 错误一律只读，必须等显式 reload 成功后才能再写。
    /// 不用第二次 fileExists 查询分类：父目录不可访问时它也返回 false，会把旧库误当成空库（P1-01）。
    func reload() {
        let data: Data
        do {
            data = try Data(contentsOf: fileURL)
        } catch {
            if Self.isMissingFile(error) {
                document = LocalDocument()
                problem = nil
            } else {
                enterReadOnly(.unreadable)
            }
            return
        }
        struct Header: Decodable { let version: Int }
        guard let header = try? JSONDecoder().decode(Header.self, from: data) else {
            return enterReadOnly(.corrupt)
        }
        guard header.version <= LocalDocument.currentVersion else {
            return enterReadOnly(.futureVersion(header.version))
        }
        guard header.version >= 1, let decoded = try? JSONDecoder().decode(LocalDocument.self, from: data) else {
            return enterReadOnly(.corrupt)
        }
        document = decoded
        problem = nil
    }

    /// 成功写盘后才替换内存副本；只读状态下拒绝任何写入。
    func commit(_ candidate: LocalDocument) throws {
        if let problem { throw problem }
        guard !failWritesForTesting else { throw LocalStoreProblem.writeFailed }
        do {
            var value = candidate
            value.version = LocalDocument.currentVersion
            let data = try JSONEncoder().encode(value)
            try FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            try data.write(to: fileURL, options: Self.writeOptions)
            document = value
        } catch {
            throw LocalStoreProblem.writeFailed
        }
    }

    /// 仅 Cocoa 的 fileReadNoSuchFile（底层 ENOENT）算「确实不存在」；父目录无搜索权限时
    /// 系统返回 EACCES（fileReadNoPermission），不会落入这里。
    private static func isMissingFile(_ error: Error) -> Bool {
        (error as? CocoaError)?.code == .fileReadNoSuchFile
    }

    private func enterReadOnly(_ value: LocalStoreProblem) {
        document = LocalDocument()
        problem = value
    }
}
