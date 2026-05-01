import PentaCore
import SwiftUI

@main
struct PentaMacApp: App {
    @StateObject private var model = PentaViewModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .task {
                    await model.start()
                }
        }
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Chat") {
                    Task { await model.createConversation() }
                }
                .keyboardShortcut("n", modifiers: [.command])
            }
        }
    }
}

@MainActor
final class PentaViewModel: ObservableObject {
    private enum RouteMode {
        case allIfNoMentions
        case mentionedOnly
    }

    private let maxRoutingHops = 3

    @Published var directory: URL
    @Published var conversations: [ConversationInfo] = []
    @Published var currentConversationTitle = "Default"
    @Published var agents: [AgentConfig] = []
    @Published var selectedAgentIDs = Set<UUID>()
    @Published var messages: [ChatMessageRecord] = []
    @Published var draft = ""
    @Published var errorMessage: String?

    private let store: SQLiteStore
    private var coordinators: [UUID: AgentCoordinator] = [:]
    private var externalPollTask: Task<Void, Never>?

    init(directory: URL = PentaViewModel.launchDirectory()) {
        let resolved = directory.resolvingSymlinksInPath().standardizedFileURL
        self.directory = resolved
        self.store = SQLiteStore(directory: resolved)
    }

    func start() async {
        do {
            try await store.connect()
            try await configureAgents()
            try await refreshConversations()
            try await loadMessages()
            startExternalPolling()
        } catch {
            errorMessage = String(describing: error)
        }
    }

    func sendDraft() async {
        let text = routedDraftText()
        guard !text.isEmpty else { return }
        draft = ""
        do {
            _ = try await store.appendMessage(sender: "User", text: text)
            messages.append(ChatMessageRecord(sender: .user, text: text))
            let tagged = TaggedMessage(senderLabel: "User", text: text)
            let mentioned = extractMentions(from: text, agents: agents)
            route(tagged, excluding: nil, mentioned: mentioned, mode: .allIfNoMentions)
            try await refreshConversations()
        } catch {
            errorMessage = String(describing: error)
        }
    }

    func toggleAgent(_ id: UUID) {
        if selectedAgentIDs.contains(id) {
            selectedAgentIDs.remove(id)
        } else {
            selectedAgentIDs.insert(id)
        }
    }

    func cancelAgent(_ id: UUID) {
        coordinators[id]?.cancel()
    }

    func cancelAll() {
        coordinators.values.forEach { $0.cancel() }
    }

    func createConversation() async {
        do {
            cancelAll()
            let title = "Chat \(Date().formatted(date: .numeric, time: .standard))"
            let id = try await store.createConversation(title: title)
            try await store.setConversation(id)
            try await configureAgents(keepIDs: true)
            try await refreshConversations()
            try await loadMessages()
        } catch {
            errorMessage = String(describing: error)
        }
    }

    func switchConversation(_ id: Int64) async {
        do {
            cancelAll()
            try await store.setConversation(id)
            try await configureAgents(keepIDs: true)
            try await refreshConversations()
            try await loadMessages()
        } catch {
            errorMessage = String(describing: error)
        }
    }

    private func refreshConversations() async throws {
        conversations = try await store.listConversations()
        let currentID = await store.conversationID
        currentConversationTitle = conversations.first(where: { $0.id == currentID })?.title ?? "Default"
    }

    private func loadMessages() async throws {
        let rows = try await store.getMessages()
        messages = rows.map { row in
            ChatMessageRecord(
                sender: sender(for: row.sender),
                text: row.text,
                timestamp: row.timestamp
            )
        }

        let history = rows.map { TaggedMessage(senderLabel: $0.sender, text: $0.text) }
        for coordinator in coordinators.values {
            coordinator.loadHistory(history, resumedSession: coordinator.hasSession)
        }
    }

    private func sender(for storedSender: String) -> MessageSender {
        if storedSender == "User" {
            return .user
        }
        if let agent = agents.first(where: { $0.name.lowercased() == storedSender.lowercased() }) {
            return .agent(agent.id)
        }
        return .external(storedSender)
    }

    private func configureAgents(keepIDs: Bool = false) async throws {
        coordinators.values.forEach { $0.shutdown() }
        coordinators.removeAll()

        let existingByKind = Dictionary(uniqueKeysWithValues: agents.map { ($0.kind, $0.id) })
        agents = AgentKind.allCases.map { kind in
            let executable = kind.findExecutable()
            return AgentConfig(
                id: keepIDs ? existingByKind[kind] ?? UUID() : UUID(),
                name: kind.defaultName,
                kind: kind,
                status: executable == nil ? .disconnected : .idle
            )
        }

        for agent in agents {
            let sessionID = try await store.loadSession(agentName: agent.name)
            let executable = agent.kind.findExecutable()
            let service: AgentService = switch agent.kind {
            case .claude:
                ClaudeService(executable: executable, model: agent.model)
            case .codex:
                CodexService(executable: executable, model: agent.model)
            }
            let coordinator = AgentCoordinator(
                config: agent,
                workingDirectory: directory,
                store: store,
                sessionID: sessionID,
                service: service
            )
            wire(coordinator)
            coordinators[agent.id] = coordinator
        }
    }

    private func wire(_ coordinator: AgentCoordinator) {
        coordinator.onResponseStarted = { [weak self] message, _ in
            self?.messages.append(message)
        }
        coordinator.onTextChanged = { [weak self] messageID, _, body, thinking in
            self?.updateMessage(messageID) { message in
                message.text = body
                message.thinkingText = thinking
            }
        }
        coordinator.onStatusChanged = { [weak self] agentID, status in
            guard let self, let index = agents.firstIndex(where: { $0.id == agentID }) else { return }
            agents[index].status = status
        }
        coordinator.onStreamComplete = { [weak self] messageID, agentID, body, isError, isCancelled, hops in
            guard let self else { return }
            updateMessage(messageID) { message in
                message.text = body
                message.isStreaming = false
                message.isError = isError
                message.isCancelled = isCancelled
            }
            guard !isCancelled, let agent = agents.first(where: { $0.id == agentID }) else { return }
            Task { await self.handleAgentCompletion(agent: agent, text: body, hops: hops) }
        }
    }

    private func handleAgentCompletion(agent: AgentConfig, text: String, hops: Int) async {
        do {
            _ = try await store.appendMessage(sender: agent.name, text: text)
            try await refreshConversations()
            guard !text.isEmpty else { return }
            let tagged = TaggedMessage(senderLabel: agent.name, text: text)
            let mentioned = extractMentions(from: text, agents: agents).subtracting([agent.id])
            route(tagged, excluding: agent.id, mentioned: mentioned, mode: .mentionedOnly, hops: hops + 1)
        } catch {
            errorMessage = String(describing: error)
        }
    }

    private func route(
        _ tagged: TaggedMessage,
        excluding: UUID?,
        mentioned: Set<UUID>,
        mode: RouteMode,
        hops: Int = 0
    ) {
        guard hops < maxRoutingHops else { return }

        let responding: Set<UUID>
        if mode == .allIfNoMentions && mentioned.isEmpty {
            responding = Set(agents.filter { $0.status != .disconnected }.map(\.id))
        } else {
            responding = mentioned
        }

        for agent in agents {
            guard agent.id != excluding, agent.status != .disconnected else { continue }
            guard let coordinator = coordinators[agent.id] else { continue }
            if responding.contains(agent.id) {
                coordinator.send(tagged, hops: hops)
            } else {
                coordinator.injectContext(tagged)
            }
        }
    }

    private func updateMessage(_ id: UUID, mutate: (inout ChatMessageRecord) -> Void) {
        guard let index = messages.firstIndex(where: { $0.id == id }) else { return }
        mutate(&messages[index])
    }

    private func routedDraftText() -> String {
        var text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return "" }
        guard !text.hasPrefix("/"), !hasBroadcastMention(text) else { return text }

        let activeAgents = agents.filter { selectedAgentIDs.contains($0.id) }
        let missingPrefixes = activeAgents
            .filter { !hasAgentMention(text, agentName: $0.name) }
            .map { "@\($0.name)" }
        if !missingPrefixes.isEmpty {
            text = "\(missingPrefixes.joined(separator: " ")) \(text)"
        }
        return text
    }

    private func startExternalPolling() {
        externalPollTask?.cancel()
        externalPollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(500))
                await self?.pollExternalMessages()
            }
        }
    }

    private func pollExternalMessages() async {
        do {
            let rows = try await store.checkExternalChanges()
            guard !rows.isEmpty else { return }
            let agentNames = Set(agents.map { $0.name.lowercased() })
            for row in rows {
                let name = sanitizeExternalName(row.sender, agentNames: agentNames)
                messages.append(ChatMessageRecord(
                    sender: .external(name),
                    text: row.text,
                    timestamp: row.timestamp
                ))
                let tagged = TaggedMessage(senderLabel: name, text: row.text)
                let mentioned = extractMentions(from: row.text, agents: agents)
                route(tagged, excluding: nil, mentioned: mentioned, mode: .mentionedOnly)
            }
        } catch {
            errorMessage = String(describing: error)
        }
    }

    func displayName(for sender: MessageSender) -> String {
        switch sender {
        case .user:
            return "You"
        case let .agent(id):
            return agents.first(where: { $0.id == id })?.name ?? "Agent"
        case let .external(name):
            return name
        }
    }

    func color(for sender: MessageSender) -> Color {
        switch sender {
        case .user:
            return .accentColor
        case let .agent(id):
            return agents.first(where: { $0.id == id })?.kind == .claude ? .orange : .green
        case .external:
            return .purple
        }
    }

    private static func launchDirectory() -> URL {
        if CommandLine.arguments.count > 1 {
            return URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
    }
}

struct ContentView: View {
    @EnvironmentObject private var model: PentaViewModel

    var body: some View {
        NavigationSplitView {
            List(selection: .constant(model.currentConversationTitle)) {
                ForEach(model.conversations) { conversation in
                    Button {
                        Task { await model.switchConversation(conversation.id) }
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(conversation.title)
                                .font(.body)
                            Text(conversation.updatedAt.formatted(date: .abbreviated, time: .shortened))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
            .navigationTitle("Penta")
        } detail: {
            VStack(spacing: 0) {
                HeaderView()
                Divider()
                MessageListView()
                Divider()
                ComposerView()
            }
            .frame(minWidth: 680, minHeight: 520)
        }
    }
}

private struct HeaderView: View {
    @EnvironmentObject private var model: PentaViewModel

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.currentConversationTitle)
                    .font(.headline)
                Text(model.directory.path)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer()
            HStack(spacing: 8) {
                ForEach(model.agents) { agent in
                    AgentStatusChip(agent: agent)
                }
            }
            if let error = model.errorMessage {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}

private struct AgentStatusChip: View {
    @EnvironmentObject private var model: PentaViewModel
    let agent: AgentConfig

    var body: some View {
        Button {
            if agent.status.isBusy {
                model.cancelAgent(agent.id)
            }
        } label: {
            HStack(spacing: 5) {
                Circle()
                    .fill(color)
                    .frame(width: 8, height: 8)
                Text(agent.name)
                    .font(.caption.weight(.medium))
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(.quinary, in: Capsule())
        }
        .buttonStyle(.plain)
        .help(agent.status.isBusy ? "Stop \(agent.name)" : "\(agent.name): \(agent.status.rawValue)")
    }

    private var color: Color {
        switch agent.status {
        case .idle:
            return .green
        case .processing:
            return .yellow
        case .waitingForUser:
            return .orange
        case .disconnected:
            return .secondary
        }
    }
}

private struct MessageListView: View {
    @EnvironmentObject private var model: PentaViewModel

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    ForEach(model.messages) { message in
                        MessageRow(message: message)
                            .id(message.id)
                    }
                }
                .padding(18)
            }
            .onChange(of: model.messages.count) {
                guard let last = model.messages.last else { return }
                proxy.scrollTo(last.id, anchor: .bottom)
            }
        }
    }
}

private struct MessageRow: View {
    @EnvironmentObject private var model: PentaViewModel
    let message: ChatMessageRecord

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(senderLabel)
                .font(.caption.weight(.semibold))
                .foregroundStyle(senderColor)
            if !message.thinkingText.isEmpty {
                DisclosureGroup("Thinking") {
                    Text(message.thinkingText)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
            Text(message.text.isEmpty ? "..." : message.text)
                .font(.body)
                .textSelection(.enabled)
            if message.isCancelled && !message.text.isEmpty {
                Text("interrupted")
                    .font(.caption.italic())
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var senderLabel: String {
        model.displayName(for: message.sender)
    }

    private var senderColor: Color {
        model.color(for: message.sender)
    }
}

private struct ComposerView: View {
    @EnvironmentObject private var model: PentaViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                ForEach(model.agents) { agent in
                    Button {
                        model.toggleAgent(agent.id)
                    } label: {
                        Text("@\(agent.name)")
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 8)
                            .padding(.vertical, 5)
                            .background(
                                model.selectedAgentIDs.contains(agent.id) ? model.color(for: .agent(agent.id)).opacity(0.22) : .clear,
                                in: Capsule()
                            )
                            .overlay {
                                Capsule().stroke(.quaternary)
                            }
                    }
                    .buttonStyle(.plain)
                    .disabled(agent.status == .disconnected)
                }
            }

            HStack(alignment: .bottom, spacing: 10) {
                TextEditor(text: $model.draft)
                    .font(.body)
                    .frame(minHeight: 44, maxHeight: 110)
                    .scrollContentBackground(.hidden)
                    .padding(6)
                    .background(.background, in: RoundedRectangle(cornerRadius: 6))
                    .overlay {
                        RoundedRectangle(cornerRadius: 6)
                            .stroke(.quaternary)
                    }
                Button {
                    Task { await model.sendDraft() }
                } label: {
                    Image(systemName: "paperplane.fill")
                }
                .keyboardShortcut(.return, modifiers: [.command])
                .disabled(model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(12)
    }
}
