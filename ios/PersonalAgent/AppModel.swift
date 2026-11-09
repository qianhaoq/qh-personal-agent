import Foundation
import Combine

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var tokens: Tokens?
    @Published private(set) var notes: [Note] = []
    @Published private(set) var answer = ""
    @Published private(set) var busy = false
    @Published private(set) var errorMessage: String?

    private let api = APIClient()
    private let defaults = UserDefaults.standard
    private let tokenKey = "verification.tokens"
    private let deviceKey = "verification.deviceID"

    init() {
        if let data = defaults.data(forKey: tokenKey) {
            tokens = try? JSONDecoder().decode(Tokens.self, from: data)
        }
    }

    private var deviceID: String {
        if let value = defaults.string(forKey: deviceKey) { return value }
        let value = UUID().uuidString
        defaults.set(value, forKey: deviceKey)
        return value
    }

    func login(inviteCode: String) async {
        guard !busy else { return }
        busy = true
        errorMessage = nil
        defer { busy = false }
        do {
            let data = try await api.request("v1/auth/login", method: "POST", body: [
                "invite_code": inviteCode.trimmingCharacters(in: .whitespacesAndNewlines),
                "device_id": deviceID, "device_name": "iOS", "platform": "ios"
            ])
            let value = try JSONDecoder().decode(Tokens.self, from: data)
            defaults.set(try JSONEncoder().encode(value), forKey: tokenKey)
            tokens = value
        } catch { errorMessage = error.localizedDescription }
    }

    func loadNotes() async {
        guard !busy, let token = tokens?.access_token else { return }
        busy = true
        errorMessage = nil
        defer { busy = false }
        do {
            let data = try await api.request("v1/notes?limit=50", token: token)
            notes = try JSONDecoder().decode(NotesResponse.self, from: data).items
        } catch { errorMessage = error.localizedDescription }
    }

    func ask(text: String) async {
        guard !busy, let token = tokens?.access_token else { return }
        busy = true
        errorMessage = nil
        answer = ""
        defer { busy = false }
        do {
            let data = try await api.request("v1/ask", method: "POST", token: token, body: [
                "text": text.trimmingCharacters(in: .whitespacesAndNewlines),
                "client_time": ISO8601DateFormatter().string(from: Date()),
                "timezone": "Asia/Shanghai"
            ])
            answer = try JSONDecoder().decode(AskResponse.self, from: data).answer
        } catch { errorMessage = error.localizedDescription }
    }

    func logout() async {
        guard !busy else { return }
        busy = true
        errorMessage = nil
        defer {
            defaults.removeObject(forKey: tokenKey)
            tokens = nil
            notes = []
            answer = ""
            busy = false
        }
        if let token = tokens?.access_token {
            do {
                _ = try await api.request("v1/auth/logout", method: "POST", token: token)
            } catch {
                errorMessage = "本机已登出；服务端登出未成功：\(error.localizedDescription)"
            }
        }
    }
}
