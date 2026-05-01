import Foundation

@MainActor
public final class AgentCoordinator {
    public let id: UUID
    public let name: String
    public let kind: AgentKind

    private let workingDirectory: URL
    private let store: SQLiteStore
    private let service: AgentService
    private var sessionID: String?
    private var fullHistory: [TaggedMessage] = []
    private var lastPromptedIndex = 0
    private var currentTask: Task<Void, Never>?

    public var onResponseStarted: ((ChatMessageRecord, UUID) -> Void)?
    public var onTextChanged: ((UUID, UUID, String, String) -> Void)?
    public var onStreamComplete: ((UUID, UUID, String, Bool, Bool, Int) -> Void)?
    public var onStatusChanged: ((UUID, AgentStatus) -> Void)?

    public init(
        config: AgentConfig,
        workingDirectory: URL,
        store: SQLiteStore,
        sessionID: String?,
        service: AgentService
    ) {
        self.id = config.id
        self.name = config.name
        self.kind = config.kind
        self.workingDirectory = workingDirectory
        self.store = store
        self.sessionID = sessionID
        self.service = service
    }

    public var hasSession: Bool {
        sessionID != nil
    }

    public func loadHistory(_ history: [TaggedMessage], resumedSession: Bool) {
        fullHistory = history
        lastPromptedIndex = resumedSession ? history.count : 0
    }

    public func injectContext(_ tagged: TaggedMessage) {
        fullHistory.append(tagged)
    }

    public func send(_ tagged: TaggedMessage, hops: Int = 0) {
        currentTask?.cancel()

        let response = ChatMessageRecord(
            sender: .agent(id),
            text: "",
            isStreaming: true
        )
        onResponseStarted?(response, id)
        onStatusChanged?(id, .processing)

        let systemPrompt = sessionID == nil ? identityPreamble() : nil
        let prompt = buildPrompt(tagged)
        fullHistory.append(tagged)
        lastPromptedIndex = fullHistory.count

        currentTask = Task { [weak self] in
            guard let self else { return }
            var body = ""
            var thinking = ""
            var isError = false
            var cancelled = false

            for await event in service.send(
                prompt: prompt,
                sessionID: sessionID,
                workingDirectory: workingDirectory,
                systemPrompt: systemPrompt
            ) {
                if Task.isCancelled {
                    cancelled = true
                    break
                }

                switch event.kind {
                case .sessionStarted:
                    if let newSession = event.sessionID {
                        sessionID = newSession
                        try? await store.saveSession(agentName: name, sessionID: newSession)
                    }
                case .textDelta:
                    body += event.text ?? ""
                    onTextChanged?(response.id, id, body, thinking)
                case .textComplete:
                    if body.isEmpty {
                        body = event.text ?? ""
                    }
                    onTextChanged?(response.id, id, body, thinking)
                case .toolUseStarted:
                    let prefix = thinking.isEmpty || thinking.hasSuffix("\n") ? "" : "\n"
                    thinking += "\(prefix)> Using \(event.toolName ?? "tool")...\n"
                    onTextChanged?(response.id, id, body, thinking)
                case .thinking:
                    thinking += event.text ?? ""
                    onTextChanged?(response.id, id, body, thinking)
                case .warning:
                    break
                case .error:
                    isError = true
                    let message = event.error ?? "Unknown error"
                    body = body.isEmpty ? message : "\(body)\n\n[Error: \(message)]"
                    onTextChanged?(response.id, id, body, thinking)
                case .usage:
                    break
                case .done:
                    break
                }
            }

            if Task.isCancelled {
                cancelled = true
            }

            if !cancelled && !body.isEmpty {
                fullHistory.append(TaggedMessage(senderLabel: name, text: body))
            }

            onStatusChanged?(id, .idle)
            onStreamComplete?(response.id, id, body, isError, cancelled, hops)
        }
    }

    public func cancel() {
        currentTask?.cancel()
        service.cancel()
        onStatusChanged?(id, .idle)
    }

    public func shutdown() {
        cancel()
        service.shutdown()
    }

    private func buildPrompt(_ current: TaggedMessage) -> String {
        var parts: [String] = []
        let missed = fullHistory.suffix(from: lastPromptedIndex)
        if !missed.isEmpty {
            parts.append("[Messages since your last response:]")
            parts += missed.map(\.formatted)
            parts.append("")
            parts.append("[New message:]")
        }
        parts.append(current.formatted)
        return parts.joined(separator: "\n")
    }

    private func identityPreamble() -> String {
        """
        You are "\(name)" in a multi-agent group chat called Penta.
        Working directory: \(workingDirectory.path)
        Other participants: other agents, User.
        Messages tagged [Group - <name>] are from the group chat.
        Use @name to address other participants.
        """
    }
}
