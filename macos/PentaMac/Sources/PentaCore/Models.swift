import Foundation

public enum AgentKind: String, CaseIterable, Identifiable, Sendable {
    case claude
    case codex

    public var id: String { rawValue }

    public var defaultName: String { rawValue }

    public var displayName: String {
        rawValue.prefix(1).uppercased() + rawValue.dropFirst()
    }

    public var executableEnvironmentKey: String {
        "PENTA_\(rawValue.uppercased())_PATH"
    }

    public func findExecutable(environment: [String: String] = ProcessInfo.processInfo.environment) -> String? {
        if let override = environment[executableEnvironmentKey],
           FileManager.default.isExecutableFile(atPath: override) {
            return override
        }

        let fallbackPath = "/opt/homebrew/bin:/usr/local/bin:\(NSHomeDirectory())/.local/bin:/usr/bin:/bin"
        let pathValue = environment["PATH"].map { "\(fallbackPath):\($0)" } ?? fallbackPath
        for directory in pathValue.split(separator: ":") {
            let candidate = URL(fileURLWithPath: String(directory))
                .appendingPathComponent(rawValue)
                .path
            if FileManager.default.isExecutableFile(atPath: candidate) {
                return candidate
            }
        }
        return nil
    }
}

public enum AgentStatus: String, Sendable {
    case idle
    case processing
    case waitingForUser
    case disconnected

    public var isBusy: Bool {
        self == .processing || self == .waitingForUser
    }
}

public struct AgentConfig: Identifiable, Equatable, Sendable {
    public let id: UUID
    public var name: String
    public let kind: AgentKind
    public var model: String?
    public var status: AgentStatus

    public init(
        id: UUID = UUID(),
        name: String,
        kind: AgentKind,
        model: String? = nil,
        status: AgentStatus = .idle
    ) {
        self.id = id
        self.name = name
        self.kind = kind
        self.model = model
        self.status = status
    }
}

public enum MessageSender: Equatable, Sendable {
    case user
    case agent(UUID)
    case external(String)

    public var isUser: Bool {
        if case .user = self { return true }
        return false
    }

    public var agentID: UUID? {
        if case let .agent(id) = self { return id }
        return nil
    }

    public var externalName: String? {
        if case let .external(name) = self { return name }
        return nil
    }
}

public struct ChatMessageRecord: Identifiable, Equatable, Sendable {
    public let id: UUID
    public var sender: MessageSender
    public var text: String
    public var timestamp: Date
    public var isStreaming: Bool
    public var isError: Bool
    public var isCancelled: Bool
    public var thinkingText: String

    public init(
        id: UUID = UUID(),
        sender: MessageSender,
        text: String,
        timestamp: Date = Date(),
        isStreaming: Bool = false,
        isError: Bool = false,
        isCancelled: Bool = false,
        thinkingText: String = ""
    ) {
        self.id = id
        self.sender = sender
        self.text = text
        self.timestamp = timestamp
        self.isStreaming = isStreaming
        self.isError = isError
        self.isCancelled = isCancelled
        self.thinkingText = thinkingText
    }
}

public struct TaggedMessage: Equatable, Sendable {
    public var senderLabel: String
    public var text: String

    public init(senderLabel: String, text: String) {
        self.senderLabel = senderLabel
        self.text = text
    }

    public var formatted: String {
        "[Group - \(senderLabel)]: \(text)"
    }
}

public struct ConversationInfo: Identifiable, Hashable, Sendable {
    public let id: Int64
    public var title: String
    public var createdAt: Date
    public var updatedAt: Date

    public init(id: Int64, title: String, createdAt: Date, updatedAt: Date) {
        self.id = id
        self.title = title
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }
}

public struct StoredMessageRow: Equatable, Sendable {
    public let id: Int64
    public let sender: String
    public let text: String
    public let timestamp: Date

    public init(id: Int64, sender: String, text: String, timestamp: Date) {
        self.id = id
        self.sender = sender
        self.text = text
        self.timestamp = timestamp
    }
}

public let reservedSenderNames: Set<String> = ["user", "shell", "system"]
public let externalSenderSuffix = " (external)"

public func sanitizeExternalName(_ name: String, agentNames: Set<String>) -> String {
    if name.hasSuffix(externalSenderSuffix) {
        return name
    }
    if reservedSenderNames.union(agentNames).contains(name.lowercased()) {
        return "\(name)\(externalSenderSuffix)"
    }
    return name
}
