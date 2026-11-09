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
    let recorded_at: String
    let tags: [String]
    let todos: [String]

    var displayTitle: String? {
        guard let value = title?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else {
            return nil
        }
        return value
    }

    var recordedDate: Date {
        Date(iso8601: recorded_at) ?? .distantPast
    }
}

struct Citation: Decodable, Identifiable {
    let memory_id: String
    let note_id: String?
    let text: String

    var id: String { memory_id }
}

struct NotesResponse: Decodable { let items: [Note] }
struct AskResponse: Decodable { let answer: String; let citations: [Citation] }
struct CreateNoteResponse: Decodable { let note_id: String }

struct APIError: LocalizedError {
    let message: String
    let statusCode: Int?

    var errorDescription: String? { message }
    var isUnauthorized: Bool { statusCode == 401 }
    var isDefiniteRejection: Bool {
        guard let statusCode else { return false }
        return (400..<500).contains(statusCode) && statusCode != 408 && statusCode != 429
    }

    init(message: String, statusCode: Int? = nil) {
        self.message = message
        self.statusCode = statusCode
    }
}

struct APIClient {
    private static let baseURL = URL(string: "https://agent.pinhaoyun.com/")!

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
            do {
                request.httpBody = try JSONEncoder().encode(body)
            } catch {
                throw APIError(message: "无法整理这次输入，请修改后重试")
            }
        }

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw APIError(message: "服务端响应无效，请稍后重试")
            }
            guard (200..<300).contains(http.statusCode) else {
                throw Self.error(statusCode: http.statusCode, data: data)
            }
            return data
        } catch let error as APIError {
            throw error
        } catch {
            throw APIError(message: "网络连接失败，请检查网络后重试")
        }
    }

    private static func error(statusCode: Int, data: Data) -> APIError {
        struct ErrorResponse: Decodable {
            struct Detail: Decodable { let message: String }
            let error: Detail
        }
        let serverMessage = try? JSONDecoder().decode(ErrorResponse.self, from: data).error.message
        let message: String
        switch statusCode {
        case 401: message = "登录已失效，请重新登录"
        case 408: message = "请求超时，请稍后重试"
        case 422: message = serverMessage ?? "输入内容未被接受，请检查后重试"
        case 429: message = "请求较频繁，请稍后重试"
        case 400..<500: message = serverMessage ?? "这次操作未被接受，请检查后重试"
        default: message = "服务暂时不可用，请稍后重试"
        }
        return APIError(message: message, statusCode: statusCode)
    }
}

extension Date {
    init?(iso8601 value: String) {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { self = date; return }
        formatter.formatOptions = [.withInternetDateTime]
        guard let date = formatter.date(from: value) else { return nil }
        self = date
    }

    var iso8601String: String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: self)
    }
}
