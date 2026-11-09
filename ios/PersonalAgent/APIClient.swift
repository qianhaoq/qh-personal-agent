import Foundation

struct Tokens: Codable {
    let access_token: String
    let refresh_token: String
}

struct Note: Decodable, Identifiable {
    let id: String
    let title: String?
    let text: String
    let status: String

    var summary: String {
        let title = title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return title.isEmpty ? String(text.prefix(120)) : title
    }
}

struct NotesResponse: Decodable { let items: [Note] }
struct AskResponse: Decodable { let answer: String }

struct APIError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

struct APIClient {
    static let baseURL = URL(string: "https://agent.pinhaoyun.com/")!

    func request(_ path: String, method: String = "GET", token: String? = nil,
                 body: [String: String]? = nil) async throws -> Data {
        guard let url = URL(string: path, relativeTo: Self.baseURL)?.absoluteURL else {
            throw APIError(message: "请求地址无效")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 90
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(body)
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw APIError(message: "服务端响应无效")
        }
        guard (200..<300).contains(http.statusCode) else {
            struct ErrorResponse: Decodable {
                struct Detail: Decodable { let message: String }
                let error: Detail
            }
            let message = (try? JSONDecoder().decode(ErrorResponse.self, from: data))?.error.message
            throw APIError(message: message ?? (http.statusCode == 401
                ? "登录已失效，请登出后重新登录" : "请求失败（HTTP \(http.statusCode)）"))
        }
        return data
    }
}
