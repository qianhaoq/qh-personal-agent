import SwiftUI

private enum Palette {
    static let canvas = Color(paperHex: 0xF7F3EA)
    static let surface = Color(paperHex: 0xFFFDF8)
    static let soft = Color(paperHex: 0xE7EEE6)
    static let ink = Color(paperHex: 0x20332D)
    static let secondary = Color(paperHex: 0x596B62)
    static let accent = Color(paperHex: 0x0E6858)
    static let accentSoft = Color(paperHex: 0xD5E8DE)
    static let attention = Color(paperHex: 0x984B35)
    static let danger = Color(paperHex: 0x9D4141)
    static let line = Color(paperHex: 0xD9D8CC)
}

private extension Color {
    init(paperHex: UInt32) {
        self.init(.sRGB,
                  red: Double((paperHex >> 16) & 0xFF) / 255,
                  green: Double((paperHex >> 8) & 0xFF) / 255,
                  blue: Double(paperHex & 0xFF) / 255,
                  opacity: 1)
    }
}

private struct PaperCard<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) { content }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.surface)
            .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(Palette.line, lineWidth: 1))
    }
}

private struct StatusPill: View {
    let text: String
    var color = Palette.secondary

    var body: some View {
        Text(text)
            .font(.caption.weight(.medium))
            .foregroundStyle(color)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(color.opacity(0.1))
            .clipShape(Capsule())
    }
}

private struct PrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.body.weight(.semibold))
            .foregroundStyle(isEnabled ? Color.white : Palette.secondary)
            .frame(maxWidth: .infinity, minHeight: 48)
            .background(isEnabled ? Palette.accent : Palette.line)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .opacity(configuration.isPressed ? 0.82 : 1)
    }
}

private extension ButtonStyle where Self == PrimaryButtonStyle {
    static var paperPrimary: PrimaryButtonStyle { PrimaryButtonStyle() }
}

private enum FlowEntry: Identifiable {
    case note(Note)
    case ask(AskItem)

    var id: String {
        switch self {
        case .note(let note): "note-\(note.id)"
        case .ask(let item): "ask-\(item.id.uuidString)"
        }
    }

    var date: Date {
        switch self {
        case .note(let note): note.recordedDate
        case .ask(let item): item.createdAt
        }
    }
}

struct ContentView: View {
    @StateObject private var model = AppModel()

    var body: some View {
        Group {
            if model.isAuthenticated {
                PersonalFlowView(model: model)
            } else {
                LoginView(model: model)
            }
        }
        .background(Palette.canvas.ignoresSafeArea())
        .tint(Palette.accent)
        .task(id: model.isAuthenticated) {
            if model.isAuthenticated { await model.loadNotes() }
        }
    }
}

private struct LoginView: View {
    @ObservedObject var model: AppModel
    @State private var inviteCode = ""
    @FocusState private var codeFocused: Bool

    private var canSubmit: Bool {
        model.activity == nil && !inviteCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                HStack(spacing: 10) {
                    Image(systemName: "square.and.pencil")
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(.white)
                        .frame(width: 40, height: 40)
                        .background(Palette.accent)
                        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    Text("随记")
                        .font(.title2.weight(.semibold))
                        .foregroundStyle(Palette.ink)
                }

                VStack(alignment: .leading, spacing: 12) {
                    Text("把一闪而过的事，\n留在纸上。")
                        .font(.largeTitle.weight(.semibold))
                        .foregroundStyle(Palette.ink)
                        .lineSpacing(6)
                    Text("记下此刻，也可以在需要时问回来。")
                        .font(.body)
                        .foregroundStyle(Palette.secondary)
                }
                .padding(.top, 28)

                PaperCard {
                    Text("使用邀请码登录")
                        .font(.headline)
                        .foregroundStyle(Palette.ink)
                    SecureField("输入邀请码", text: $inviteCode)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.go)
                        .focused($codeFocused)
                        .onSubmit(submit)
                        .padding(14)
                        .background(Palette.canvas)
                        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
                            .stroke(codeFocused ? Palette.accent : Palette.line, lineWidth: 1.5))
                    if let message = model.loginError {
                        Label(message, systemImage: "exclamationmark.circle")
                            .font(.footnote)
                            .foregroundStyle(Palette.danger)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Button(action: submit) {
                        HStack {
                            if model.activity == .loggingIn { ProgressView().tint(.white) }
                            Text(model.activity == .loggingIn ? "正在登录…" : "进入随记")
                            Image(systemName: "arrow.right")
                        }
                    }
                    .buttonStyle(.paperPrimary)
                    .disabled(!canSubmit)
                }

                Label("当前支持文字记事与提问；语音、提醒和闹钟尚未接入。",
                      systemImage: "info.circle")
                    .font(.footnote)
                    .foregroundStyle(Palette.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 32)
            .frame(maxWidth: 560)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Palette.canvas)
        .accessibilityIdentifier("login-screen")
    }

    private func submit() {
        guard canSubmit else { return }
        codeFocused = false
        Task {
            await model.login(inviteCode: inviteCode)
            if model.isAuthenticated { inviteCode = "" }
        }
    }
}

private struct PersonalFlowView: View {
    @ObservedObject var model: AppModel
    @State private var showSettings = false
    @FocusState private var composerFocused: Bool

    private var entries: [FlowEntry] {
        (model.notes.map(FlowEntry.note) + model.askItems.map(FlowEntry.ask))
            .sorted { $0.date > $1.date }
    }

    private var showEmpty: Bool {
        entries.isEmpty && model.pendingNote == nil && model.notesError == nil
            && model.hasLoadedNotes && model.activity != .loadingNotes
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("随记")
                        .font(.title2.weight(.semibold))
                    Text("我的个人流")
                        .font(.caption)
                        .foregroundStyle(Palette.secondary)
                }
                Spacer()
                Button { Task { await model.loadNotes() } } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.title3)
                        .frame(width: 44, height: 44)
                }
                .disabled(model.activity != nil)
                .accessibilityLabel("刷新笔记")
                Button { showSettings = true } label: {
                    Image(systemName: "gearshape")
                        .font(.title3)
                        .frame(width: 44, height: 44)
                }
                .accessibilityLabel("设置")
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 8)
            .foregroundStyle(Palette.ink)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("此刻，想留下什么？")
                            .font(.system(size: 30, weight: .semibold, design: .rounded))
                        Text("笔记和刚刚问过的答案，会按时间留在这里。")
                            .font(.subheadline)
                            .foregroundStyle(Palette.secondary)
                        if model.isDemo {
                            StatusPill(text: "示例内容 · 仅用于界面预览", color: Palette.attention)
                        }
                    }
                    .padding(.top, 12)
                    .padding(.bottom, 4)

                    if let pending = model.pendingNote {
                        PendingNoteCard(model: model, pending: pending)
                    }
                    if let message = model.notesError {
                        PaperCard {
                            Label("暂时没能读取全部笔记", systemImage: "exclamationmark.circle")
                                .font(.headline).foregroundStyle(Palette.attention)
                            Text(message).font(.footnote).foregroundStyle(Palette.secondary)
                            Button("重新读取") { Task { await model.loadNotes() } }
                                .disabled(model.activity != nil)
                        }
                    }
                    if model.notesError == nil && (!model.hasLoadedNotes || (model.activity == .loadingNotes && model.notes.isEmpty)) {
                        PaperCard {
                            HStack(spacing: 12) {
                                ProgressView()
                                Text(model.activity?.label ?? "准备读取笔记…")
                                    .foregroundStyle(Palette.secondary)
                            }
                        }
                    }

                    if !entries.isEmpty {
                        Text("最近")
                            .font(.headline)
                            .padding(.top, 4)
                        ForEach(entries) { entry in
                            switch entry {
                            case .note(let note): NoteCard(note: note, isDemo: model.isDemo)
                            case .ask(let item): AskCard(model: model, item: item)
                            }
                        }
                    }

                    if showEmpty {
                        EmptyFlowView(mode: $model.mode, focused: $composerFocused)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 28)
                .frame(maxWidth: 680)
                .frame(maxWidth: .infinity)
            }
            .scrollDismissesKeyboard(.interactively)

            ComposerView(model: model, focused: $composerFocused)
        }
        .background(Palette.canvas)
        .sheet(isPresented: $showSettings) { SettingsView(model: model) }
    }
}

private struct PendingNoteCard: View {
    @ObservedObject var model: AppModel
    let pending: PendingNote

    var body: some View {
        PaperCard {
            HStack {
                Label(pending.serverID == nil ? "原文保存" : "读取已保存的笔记",
                      systemImage: "doc.text")
                    .font(.headline)
                Spacer()
                if model.activity == .savingNote { ProgressView() }
            }
            Text(pending.text)
                .font(.subheadline)
                .foregroundStyle(Palette.secondary)
                .lineLimit(3)
            if let failure = pending.failure {
                Text(failure)
                    .font(.footnote)
                    .foregroundStyle(Palette.attention)
                    .fixedSize(horizontal: false, vertical: true)
                Button(pending.isRejected ? "取回编辑" : (pending.serverID == nil ? "重试保存" : "重新读取")) {
                    if pending.isRejected { model.takeBackPendingNote() }
                    else { Task { await model.retryPendingNote() } }
                }
                .font(.body.weight(.semibold))
                .frame(minHeight: 44)
                .disabled(model.activity != nil)
            } else {
                Text(pending.serverID == nil ? "正在保存原文；完成前不会把它当作服务器笔记。"
                                             : "服务端已接收原文，正在读取真实状态…")
                    .font(.footnote)
                    .foregroundStyle(Palette.secondary)
            }
        }
    }
}

private struct NoteCard: View {
    let note: Note
    let isDemo: Bool

    private var status: (String, Color) {
        switch note.status {
        case "uploaded": ("已保存 · 整理中", Palette.attention)
        case "organized": ("已整理", Palette.accent)
        case "failed": ("原文已保存 · 整理失败", Palette.attention)
        default: ("已保存", Palette.secondary)
        }
    }

    var body: some View {
        PaperCard {
            HStack(spacing: 8) {
                Label("笔记", systemImage: "doc.text")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Palette.secondary)
                Spacer()
                if isDemo { StatusPill(text: "示例", color: Palette.attention) }
                StatusPill(text: status.0, color: status.1)
            }
            if let title = note.displayTitle {
                Text(title).font(.title3.weight(.semibold)).foregroundStyle(Palette.ink)
            }
            Text(note.text)
                .font(.body)
                .foregroundStyle(Palette.ink)
                .lineSpacing(5)
                .lineLimit(5)
                .textSelection(.enabled)
            if !note.tags.isEmpty {
                HStack(spacing: 8) {
                    ForEach(note.tags.prefix(3), id: \.self) { Text("#\($0)").font(.caption) }
                }
                .foregroundStyle(Palette.accent)
            }
            ForEach(note.todos.prefix(2), id: \.self) { todo in
                Label(todo, systemImage: "circle")
                    .font(.footnote)
                    .foregroundStyle(Palette.secondary)
            }
            Text(note.recordedDate == .distantPast ? "时间未提供" :
                    note.recordedDate.formatted(date: .abbreviated, time: .shortened))
                .font(.caption)
                .foregroundStyle(Palette.secondary)
        }
        .accessibilityIdentifier("note-card-\(note.id)")
    }
}

private struct AskCard: View {
    @ObservedObject var model: AppModel
    let item: AskItem

    var body: some View {
        PaperCard {
            HStack {
                Label("你问", systemImage: "bubble.left")
                    .font(.caption.weight(.semibold)).foregroundStyle(Palette.secondary)
                Spacer()
                if model.isDemo { StatusPill(text: "示例", color: Palette.attention) }
            }
            Text(item.question).font(.headline).foregroundStyle(Palette.ink)
            Divider().overlay(Palette.line)
            if item.isAsking {
                HStack(spacing: 10) {
                    ProgressView()
                    Text("正在查找记忆并回答…")
                        .font(.subheadline).foregroundStyle(Palette.secondary)
                }
            } else if let answer = item.answer {
                Text(answer)
                    .font(.body)
                    .foregroundStyle(Palette.ink)
                    .lineSpacing(6)
                    .textSelection(.enabled)
                if !item.citations.isEmpty {
                    DisclosureGroup("查看 \(item.citations.count) 条来源摘录") {
                        VStack(alignment: .leading, spacing: 12) {
                            ForEach(item.citations) { citation in
                                Text(citation.text)
                                    .font(.footnote)
                                    .foregroundStyle(Palette.secondary)
                                    .lineLimit(4)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                        .padding(.top, 12)
                    }
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(Palette.accent)
                }
            } else {
                Label("这次没有拿到答案", systemImage: "exclamationmark.circle")
                    .font(.headline).foregroundStyle(Palette.attention)
                if let message = item.errorMessage {
                    Text(message).font(.footnote).foregroundStyle(Palette.secondary)
                }
                Button("重新提问") { Task { await model.retryAsk(id: item.id) } }
                    .font(.body.weight(.semibold))
                    .frame(minHeight: 44)
                    .disabled(model.activity != nil)
            }
            Text(item.createdAt.formatted(date: .omitted, time: .shortened))
                .font(.caption).foregroundStyle(Palette.secondary)
        }
    }
}

private struct EmptyFlowView: View {
    @Binding var mode: InputMode
    var focused: FocusState<Bool>.Binding

    var body: some View {
        VStack(spacing: 18) {
            EmptyPaperArt()
            Text("从一句话开始")
                .font(.title2.weight(.semibold)).foregroundStyle(Palette.ink)
            Text("这里还没有笔记或问答。\n写下此刻，或问回一段记忆。")
                .font(.body)
                .foregroundStyle(Palette.secondary)
                .multilineTextAlignment(.center)
                .lineSpacing(5)
            Button {
                mode = .note
                focused.wrappedValue = true
            } label: {
                Label("用文字记下第一条", systemImage: "square.and.pencil")
                    .padding(.horizontal, 20)
            }
            .buttonStyle(.paperPrimary)
            .fixedSize()
            Button("我想问一段记忆") {
                mode = .ask
                focused.wrappedValue = true
            }
            .font(.body.weight(.medium))
            .frame(minHeight: 44)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 24)
        .padding(.vertical, 34)
        .background(Palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 24, style: .continuous)
            .stroke(Palette.line, lineWidth: 1))
        .accessibilityIdentifier("personal-flow-empty")
    }
}

private struct EmptyPaperArt: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(Palette.soft).frame(width: 100, height: 100)
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Palette.surface).frame(width: 56, height: 68).rotationEffect(.degrees(-5))
            VStack(spacing: 9) {
                Capsule().fill(Palette.line).frame(width: 30, height: 3)
                Capsule().fill(Palette.line).frame(width: 36, height: 3)
                Capsule().fill(Palette.accent).frame(width: 22, height: 3)
            }
            Image(systemName: "plus")
                .font(.caption.weight(.bold)).foregroundStyle(.white)
                .frame(width: 28, height: 28).background(Palette.accent).clipShape(Circle())
                .offset(x: 31, y: 31)
        }
        .accessibilityHidden(true)
    }
}

private struct ComposerView: View {
    @ObservedObject var model: AppModel
    var focused: FocusState<Bool>.Binding

    private var trimmed: String { model.draft.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var sendDisabled: Bool {
        model.activity != nil || trimmed.isEmpty || trimmed.count > model.mode.maximumLength
            || (model.mode == .note && model.pendingNote != nil)
    }

    var body: some View {
        VStack(spacing: 10) {
            HStack(spacing: 4) {
                ForEach([InputMode.note, .ask], id: \.self) { mode in
                    Button { model.mode = mode } label: {
                        Text(mode.title)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(model.mode == mode ? Palette.accent : Palette.secondary)
                            .frame(maxWidth: .infinity, minHeight: 38)
                            .background(model.mode == mode ? Palette.accentSoft : Color.clear)
                            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    }
                    .accessibilityAddTraits(model.mode == mode ? .isSelected : [])
                }
            }
            .padding(4)
            .background(Palette.soft)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))

            TextField(model.mode.placeholder, text: $model.draft, axis: .vertical)
                .lineLimit(1...4)
                .focused(focused)
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .background(Palette.surface)
                .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .stroke(focused.wrappedValue ? Palette.accent : Palette.line, lineWidth: 1.5))
                .accessibilityIdentifier("composer-text")

            if let message = model.composerError {
                Text(message).font(.caption).foregroundStyle(Palette.danger)
            } else if model.draft.count > model.mode.maximumLength * 4 / 5 {
                Text("\(model.draft.count) / \(model.mode.maximumLength) 字")
                    .font(.caption)
                    .foregroundStyle(model.draft.count > model.mode.maximumLength ? Palette.danger : Palette.secondary)
            }

            HStack(spacing: 12) {
                Button {} label: {
                    Label("语音", systemImage: "mic")
                        .frame(minWidth: 72, minHeight: 44)
                }
                .disabled(true)
                .foregroundStyle(Palette.secondary)
                .accessibilityIdentifier("voice-disabled")
                .accessibilityHint("语音输入尚未接入，请使用文字")

                Button { Task { await model.submit() } } label: {
                    HStack {
                        Spacer()
                        if model.activity == .savingNote || model.activity == .asking {
                            ProgressView().tint(.white)
                        }
                        Text(model.mode.title)
                        Image(systemName: model.mode == .note ? "arrow.up" : "sparkles")
                        Spacer()
                    }
                }
                .buttonStyle(.paperPrimary)
                .disabled(sendDisabled)
                .accessibilityIdentifier("composer-submit")
            }

            Text(model.activity.map { "\($0.label) 可继续写，结束后请手动发送。" }
                 ?? "语音输入尚未接入，请使用文字。")
                .font(.caption)
                .foregroundStyle(Palette.secondary)
                .frame(maxWidth: .infinity, alignment: .center)
        }
        .padding(.horizontal, 16)
        .padding(.top, 12)
        .padding(.bottom, 10)
        .background(Palette.canvas)
        .overlay(alignment: .top) { Rectangle().fill(Palette.line).frame(height: 1) }
    }
}

private struct SettingsView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var confirmLogout = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    PaperCard {
                        Text("当前能力").font(.headline)
                        CapabilityRow(icon: "square.and.pencil", title: "文字记事", available: true)
                        CapabilityRow(icon: "bubble.left.and.text", title: "文字提问", available: true)
                        CapabilityRow(icon: "mic", title: "语音输入", available: false)
                        CapabilityRow(icon: "bell", title: "提醒与闹钟", available: false)
                    }
                    PaperCard {
                        Text("数据与恢复").font(.headline)
                        Text("笔记保存在服务端。问答和未完成的创建恢复项只保留在当前进程；未发送草稿不持久保存。")
                            .font(.footnote).foregroundStyle(Palette.secondary).lineSpacing(4)
                        Text("当前不自动刷新登录凭证，过期后需要重新登录。")
                            .font(.footnote).foregroundStyle(Palette.secondary).lineSpacing(4)
                    }
                    Button("退出登录", role: .destructive) { confirmLogout = true }
                        .font(.body.weight(.semibold))
                        .frame(maxWidth: .infinity, minHeight: 48)
                        .background(Palette.surface)
                        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                        .disabled(model.activity != nil)
                }
                .padding(20)
            }
            .background(Palette.canvas)
            .foregroundStyle(Palette.ink)
            .navigationTitle("设置")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
            .confirmationDialog("退出当前设备？", isPresented: $confirmLogout, titleVisibility: .visible) {
                Button("退出登录", role: .destructive) {
                    Task { await model.logout(); dismiss() }
                }
            } message: {
                Text("本机将清除登录凭证、草稿与当前问答；服务器笔记不会删除。")
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }
}

private struct CapabilityRow: View {
    let icon: String
    let title: String
    let available: Bool

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon).frame(width: 24)
            Text(title)
            Spacer()
            Text(available ? "可用" : "未接入")
                .font(.caption).foregroundStyle(available ? Palette.accent : Palette.secondary)
        }
        .foregroundStyle(available ? Palette.ink : Palette.secondary)
        .frame(minHeight: 36)
    }
}

#if DEBUG
#Preview("登录") { LoginView(model: AppModel.previewLogin()) }

#Preview("空个人流") { PersonalFlowView(model: AppModel.previewEmpty()) }

#Preview("示例内容") { PersonalFlowView(model: AppModel.previewContent()) }
#endif
