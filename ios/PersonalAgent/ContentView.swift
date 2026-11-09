import SwiftUI

struct ContentView: View {
    @StateObject private var model = AppModel()
    @State private var inviteCode = ""
    @State private var question = ""

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("随记 iOS 验证壳 · d5d2860").font(.headline)
                    Text(APIClient.baseURL.absoluteString)
                        .font(.caption).textSelection(.enabled)
                }
                if let message = model.errorMessage {
                    Section { Text(message).foregroundStyle(.red).textSelection(.enabled) }
                }
                if model.tokens == nil {
                    Section("登录") {
                        SecureField("邀请码", text: $inviteCode)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        Button("登录") {
                            Task {
                                await model.login(inviteCode: inviteCode)
                                if model.tokens != nil { inviteCode = "" }
                            }
                        }
                        .disabled(model.busy || inviteCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                } else {
                    Section("提问") {
                        TextField("输入问题（最多 2000 字）", text: $question, axis: .vertical)
                            .lineLimit(2...6)
                        Button("提问") { Task { await model.ask(text: question) } }
                            .disabled(model.busy || question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || question.count > 2000)
                        if !model.answer.isEmpty {
                            Text(model.answer).textSelection(.enabled)
                        }
                    }
                    Section {
                        Button("刷新笔记") { Task { await model.loadNotes() } }
                            .disabled(model.busy)
                        if model.notes.isEmpty && !model.busy { Text("暂无笔记").foregroundStyle(.secondary) }
                        ForEach(model.notes) { note in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(note.summary).lineLimit(3)
                                Text(note.status).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    } header: { Text("笔记（最多 50 条）") }
                    Section {
                        Button("登出", role: .destructive) {
                            Task {
                                await model.logout()
                                question = ""
                            }
                        }.disabled(model.busy)
                    }
                }
                if model.busy { ProgressView("请求中…") }
            }
            .navigationTitle("随记")
            .task(id: model.tokens?.access_token) {
                if model.tokens != nil { await model.loadNotes() }
            }
        }
    }
}
