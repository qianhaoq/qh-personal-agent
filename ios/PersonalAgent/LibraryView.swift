import SwiftUI

private enum EditTarget: Identifiable {
    case new(LocalItemKind)
    case edit(LocalItem)

    var id: String {
        switch self {
        case .new(let kind): "new-\(kind.rawValue)"
        case .edit(let item): item.id.uuidString
        }
    }
}

/// 本机任务与记忆：可新建、编辑、完成、删除/忘记；查找为逐字匹配。
struct LibraryView: View {
    @ObservedObject var model: LocalModel
    @Environment(\.dismiss) private var dismiss
    @State private var kind = LocalItemKind.task
    @State private var query = ""
    @State private var editing: EditTarget?
    @State private var pendingRemoval: LocalItem?

    private var needle: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var removalTitle: String { pendingRemoval?.kind == .memory ? "忘记这条记忆？" : "删除这条任务？" }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Label("全部只保存在本机，未同步到云端。查找是逐字匹配，不是 AI 理解。", systemImage: "iphone")
                        .foregroundStyle(.secondary)
                    if let problem = model.problem {
                        Text(LocalModel.message(for: problem)).foregroundStyle(.orange)
                    }
                    if let error = model.actionError {
                        Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.red)
                            .accessibilityIdentifier("library-action-error")
                    }
                }
                .font(.footnote)
                if needle.isEmpty {
                    Picker("类别", selection: $kind) {
                        Text("任务").tag(LocalItemKind.task)
                        Text("记忆").tag(LocalItemKind.memory)
                    }
                    .pickerStyle(.segmented)
                    .listRowBackground(Color.clear)
                    section(kind, items: model.items(kind),
                            emptyText: "还没有\(kind.title)。在对话里点「存为\(kind.title)」，或点左上角 + 新建。")
                } else {
                    ForEach(LocalItemKind.allCases) { kind in
                        let matches = model.items(kind).filter { LocalSearch.matches($0.text, query: needle) }
                        if !matches.isEmpty { section(kind, items: matches, emptyText: "") }
                    }
                    if !model.document.items.contains(where: { LocalSearch.matches($0.text, query: needle) }) {
                        Text("本机没有包含「\(needle)」的内容。").foregroundStyle(.secondary)
                    }
                }
            }
            .searchable(text: $query, prompt: "逐字查找本机内容")
            .navigationTitle("任务与记忆")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { editing = .new(kind) } label: { Label("新建\(kind.title)", systemImage: "plus") }
                        .disabled(model.isReadOnly)
                }
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
            .sheet(item: $editing) { ItemEditor(model: model, target: $0) }
            .confirmationDialog(removalTitle, isPresented: Binding(
                get: { pendingRemoval != nil }, set: { if !$0 { pendingRemoval = nil } }
            ), titleVisibility: .visible, presenting: pendingRemoval) { item in
                Button(item.kind == .memory ? "忘记" : "删除", role: .destructive) { _ = model.remove(item) }
            } message: { _ in
                Text("会从本机永久移除，无法撤销。对话里的原消息保留。")
            }
        }
    }

    private func section(_ kind: LocalItemKind, items: [LocalItem], emptyText: String) -> some View {
        Section(kind.title) {
            if items.isEmpty {
                Text(emptyText).font(.subheadline).foregroundStyle(.secondary)
            }
            ForEach(Array(items.reversed())) { row($0) }
        }
    }

    private func row(_ item: LocalItem) -> some View {
        HStack(alignment: .top, spacing: 12) {
            if item.kind == .task {
                Button { _ = model.toggleDone(item) } label: {
                    Image(systemName: item.isDone ? "checkmark.circle.fill" : "circle")
                        .font(.title3).frame(minWidth: 32, minHeight: 32)
                }
                .buttonStyle(.plain)
                .foregroundStyle(item.isDone ? Color.blue : Color.secondary)
                .accessibilityLabel(item.isDone ? "标记为未完成" : "标记为完成")
            } else {
                Image(systemName: item.kind == .memory ? "bookmark" : "text.bubble")
                    .foregroundStyle(Color.secondary).frame(minWidth: 32, minHeight: 32).accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(item.text).strikethrough(item.isDone)
                    .foregroundStyle(item.isDone ? Color.secondary : Color.primary)
                Text("本机 · 未同步 · " + item.updatedAt.formatted(date: .abbreviated, time: .shortened))
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
        .swipeActions(edge: .trailing) { if item.kind != .message { actions(for: item) } }
        .contextMenu { if item.kind != .message { actions(for: item) } }
    }

    @ViewBuilder
    private func actions(for item: LocalItem) -> some View {
        Button { pendingRemoval = item } label: {
            Label(item.kind == .memory ? "忘记" : "删除", systemImage: "trash")
        }
        .tint(.red)
        Button { editing = .edit(item) } label: { Label("编辑", systemImage: "pencil") }
            .tint(.blue)
    }
}

private struct ItemEditor: View {
    @ObservedObject var model: LocalModel
    let target: EditTarget
    @Environment(\.dismiss) private var dismiss
    @State private var text: String
    @State private var error: String? = nil
    private let original: String

    init(model: LocalModel, target: EditTarget) {
        _model = ObservedObject(wrappedValue: model)
        self.target = target
        let initial: String
        if case .edit(let item) = target { initial = item.text } else { initial = "" }
        original = initial
        _text = State(initialValue: initial)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("内容", text: $text, axis: .vertical)
                        .lineLimit(3...12)
                        .accessibilityIdentifier("item-editor-text")
                } footer: {
                    Text("仅保存在本机，未同步。最多 \(LocalDocument.maxTextLength) 字。")
                }
                if let error {
                    Section { Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.red) }
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存", action: save)
                        .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .interactiveDismissDisabled(text != original)
    }

    private var title: String {
        switch target {
        case .new(let kind): "新建\(kind.title)"
        case .edit(let item): "编辑\(item.kind.title)"
        }
    }

    /// 写盘成功才关闭；失败时保留编辑内容并就地说明。
    private func save() {
        let ok: Bool
        switch target {
        case .new(let kind): ok = model.add(kind, text: text)
        case .edit(let item): ok = model.edit(item.id, text: text)
        }
        if ok { dismiss() } else { error = model.actionError ?? "未能保存到本机。" }
    }
}
