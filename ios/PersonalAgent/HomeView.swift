import SwiftUI

private enum HomeSheet: Identifiable, Equatable {
    case library, cloud
    var id: Self { self }
}

/// App 根：本机对话为首页，无需邀请码；云端账户是独立的可选 Sheet，两者不共享数据。
struct RootView: View {
    @StateObject private var local = LocalModel()
    @StateObject private var cloud = AppModel()
    @State private var sheet: HomeSheet?
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        NavigationStack {
            ConversationView(model: local, openLibrary: { sheet = .library })
                .navigationTitle("随记")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        Button { sheet = .cloud } label: { Label("云端账户（可选）", systemImage: "person.crop.circle") }
                            .accessibilityIdentifier("open-cloud")
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { sheet = .library } label: { Label("任务与记忆", systemImage: "tray.full") }
                            .accessibilityIdentifier("open-library")
                    }
                }
        }
        .tint(.blue)
        .sheet(item: $sheet) { sheet in
            switch sheet {
            case .library: LibraryView(model: local)
            case .cloud: CloudAccountView(model: cloud)
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase != .active { local.saveDraftNow() }
        }
    }
}

private extension View {
    func panel(_ color: Color, radius: CGFloat = 18) -> some View {
        padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(color, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
    }
}

struct ConversationView: View {
    @ObservedObject var model: LocalModel
    let openLibrary: () -> Void
    @FocusState private var composerFocused: Bool
    @State private var pendingDelete: LocalItem?

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 20) {
                    boundary
                    if let problem = model.problem {
                        problemCard(problem)
                    } else if model.messages.isEmpty {
                        welcome
                    }
                    ForEach(model.messages) { message in
                        MessageRow(model: model, message: message, openLibrary: openLibrary) { pendingDelete = message }
                            .id(message.id)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
                .frame(maxWidth: 720)
                .frame(maxWidth: .infinity)
            }
            .defaultScrollAnchor(.bottom)
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: model.messages.last?.id) { _, id in
                guard let id else { return }
                withAnimation { proxy.scrollTo(id, anchor: .bottom) }
            }
        }
        .background(Color(.systemBackground))
        .safeAreaInset(edge: .bottom, spacing: 0) { ComposerBar(model: model, focused: $composerFocused) }
        .confirmationDialog("删除这条消息？", isPresented: Binding(
            get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }
        ), titleVisibility: .visible, presenting: pendingDelete) { item in
            Button("删除", role: .destructive) { _ = model.remove(item) }
        } message: { _ in
            Text("只删除对话里的这条消息，已存的任务或记忆会保留。仅影响本机。")
        }
    }

    private var boundary: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "iphone").font(.title3).foregroundStyle(.tint).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text("本机对话 · 离线可用 · 未同步").font(.subheadline.weight(.semibold))
                Text("内容只保存在这台设备，不会上传，也没有 AI 自动回复。云端账户是另外的可选功能，连接后也不会上传这里的内容。")
                    .font(.footnote).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .panel(Color(.secondarySystemBackground))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("local-boundary")
    }

    private var welcome: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("想到什么，就写下来").font(.title2.weight(.semibold))
            Text("发出的话会留在这条对话里。任何一条都可以存为任务或记忆，之后随时编辑、完成或忘记。")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Button("开始输入") { composerFocused = true }
                .buttonStyle(.borderedProminent).controlSize(.large)
        }
        .padding(.vertical, 24)
        .accessibilityIdentifier("local-empty")
    }

    private func problemCard(_ problem: LocalStoreProblem) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("本机数据只读", systemImage: "exclamationmark.triangle.fill").font(.headline).foregroundStyle(.orange)
            Text(LocalModel.message(for: problem)).font(.subheadline).fixedSize(horizontal: false, vertical: true)
            if problem == .unreadable {
                Button("重新读取") { model.reload() }.buttonStyle(.bordered)
            }
        }
        .panel(Color.orange.opacity(0.12))
        .accessibilityIdentifier("local-problem")
    }
}

private struct MessageRow: View {
    @ObservedObject var model: LocalModel
    let message: LocalItem
    let openLibrary: () -> Void
    let requestDelete: () -> Void

    var body: some View {
        VStack(alignment: .trailing, spacing: 8) {
            Text(message.text)
                .textSelection(.enabled)
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .background(Color.blue.opacity(0.14), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            HStack(spacing: 8) {
                ForEach([LocalItemKind.task, .memory]) { kind in
                    if model.document.derived(from: message.id, kind: kind) != nil {
                        Button(action: openLibrary) { Label("已存为\(kind.title)", systemImage: "checkmark") }
                            .buttonStyle(.borderless).foregroundStyle(.secondary)
                    } else {
                        Button { _ = model.save(message, as: kind) } label: {
                            Label("存为\(kind.title)", systemImage: kind == .task ? "checklist" : "bookmark")
                        }
                        .buttonStyle(.bordered)
                    }
                }
                Menu {
                    Button(role: .destructive, action: requestDelete) { Label("删除这条消息", systemImage: "trash") }
                } label: {
                    Image(systemName: "ellipsis").frame(width: 44, height: 32)
                }
                .accessibilityLabel("更多操作")
            }
            .font(.footnote)
            .controlSize(.small)
            Text(message.createdAt.formatted(date: .abbreviated, time: .shortened) + " · 仅本机")
                .font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 32)
    }
}

private struct ComposerBar: View {
    @ObservedObject var model: LocalModel
    var focused: FocusState<Bool>.Binding

    private var trimmed: String { model.draft.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var canSend: Bool {
        !model.isReadOnly && !trimmed.isEmpty && trimmed.count <= LocalDocument.maxTextLength
    }
    private var status: (text: String, warn: Bool) {
        let count = model.draft.count, limit = LocalDocument.maxTextLength
        if model.isReadOnly { return ("本机存储只读：新内容和草稿不会保存", true) }
        if model.draftUnsaved { return ("草稿未能保存到本机", true) }
        if count > limit * 4 / 5 { return ("\(count) / \(limit) 字 · 仅存本机", count > limit) }
        return ("仅保存在本机 · 未同步", false)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let error = model.actionError {
                HStack(alignment: .top) {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                    Spacer()
                    Button("知道了") { model.dismissError() }
                }
                .font(.footnote)
                .accessibilityIdentifier("local-action-error")
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField("写下想留下的事…", text: $model.draft, axis: .vertical)
                    .lineLimit(1...6)
                    .focused(focused)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                    .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                    .accessibilityIdentifier("local-composer")
                Button { _ = model.send() } label: {
                    Image(systemName: "arrow.up.circle.fill").font(.system(size: 36)).frame(minWidth: 44, minHeight: 44)
                }
                .disabled(!canSend)
                .accessibilityLabel("发送到本机对话")
                .accessibilityIdentifier("local-send")
            }
            Text(status.text)
                .font(.caption)
                .foregroundStyle(status.warn ? Color.orange : Color.secondary)
                .accessibilityIdentifier("local-status")
        }
        .padding(.horizontal, 16)
        .padding(.top, 10)
        .padding(.bottom, 8)
        .background(.bar)
    }
}

#if DEBUG
#Preview("本机对话") { RootView() }
#endif
