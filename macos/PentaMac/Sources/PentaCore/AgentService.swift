import Darwin
import Foundation

public enum StreamEventKind: Sendable {
    case sessionStarted
    case textDelta
    case textComplete
    case toolUseStarted
    case thinking
    case warning
    case error
    case usage
    case done
}

public struct StreamEvent: Sendable {
    public var kind: StreamEventKind
    public var sessionID: String?
    public var text: String?
    public var toolID: String?
    public var toolName: String?
    public var error: String?
    public var usageDescription: String?

    public init(
        kind: StreamEventKind,
        sessionID: String? = nil,
        text: String? = nil,
        toolID: String? = nil,
        toolName: String? = nil,
        error: String? = nil,
        usageDescription: String? = nil
    ) {
        self.kind = kind
        self.sessionID = sessionID
        self.text = text
        self.toolID = toolID
        self.toolName = toolName
        self.error = error
        self.usageDescription = usageDescription
    }
}

public protocol AgentService: AnyObject {
    func send(
        prompt: String,
        sessionID: String?,
        workingDirectory: URL,
        systemPrompt: String?
    ) -> AsyncStream<StreamEvent>

    func cancel()
    func shutdown()
}

open class CLIAgentService: AgentService, @unchecked Sendable {
    private let agentName: String
    private let executable: String?
    let model: String?
    private let lock = NSLock()
    private var currentProcess: Process?

    public init(agentName: String, executable: String?, model: String? = nil) {
        self.agentName = agentName
        self.executable = executable
        self.model = model
    }

    open func buildArguments(
        prompt: String,
        sessionID: String?,
        systemPrompt: String?
    ) -> [String] {
        fatalError("Subclasses must implement buildArguments")
    }

    open func parseLine(_ data: [String: Any]) -> [StreamEvent] {
        fatalError("Subclasses must implement parseLine")
    }

    open func resetParseState() {}

    public func send(
        prompt: String,
        sessionID: String?,
        workingDirectory: URL,
        systemPrompt: String?
    ) -> AsyncStream<StreamEvent> {
        cancel()
        resetParseState()

        return AsyncStream { continuation in
            guard let executable else {
                continuation.yield(StreamEvent(
                    kind: .error,
                    error: "\(agentName) CLI not found. Set PENTA_\(agentName.uppercased())_PATH or install \(agentName.lowercased())."
                ))
                continuation.yield(StreamEvent(kind: .done))
                continuation.finish()
                return
            }

            let task = Task { [weak self] in
                guard let self else { return }
                let process = Process()
                let stdout = Pipe()
                let stderr = Pipe()

                process.executableURL = URL(fileURLWithPath: executable)
                process.arguments = self.buildArguments(
                    prompt: prompt,
                    sessionID: sessionID,
                    systemPrompt: systemPrompt
                )
                process.currentDirectoryURL = workingDirectory
                process.environment = Self.buildCLIEnvironment()
                process.standardOutput = stdout
                process.standardError = stderr

                do {
                    try process.run()
                } catch {
                    continuation.yield(StreamEvent(kind: .error, error: error.localizedDescription))
                    continuation.yield(StreamEvent(kind: .done))
                    continuation.finish()
                    return
                }

                self.setCurrentProcess(process)
                let stderrTask = Task {
                    stderr.fileHandleForReading.readDataToEndOfFile()
                }

                do {
                    try await self.streamLines(from: stdout.fileHandleForReading) { line in
                        guard
                            let payload = line.data(using: .utf8),
                            let json = try? JSONSerialization.jsonObject(with: payload),
                            let data = json as? [String: Any]
                        else {
                            return
                        }
                        for event in self.parseLine(data) {
                            continuation.yield(event)
                        }
                    }
                } catch {
                    continuation.yield(StreamEvent(kind: .error, error: error.localizedDescription))
                }

                process.waitUntilExit()
                self.clearCurrentProcess(process)

                let stderrData = await stderrTask.value
                if process.terminationStatus != 0,
                   let stderrText = String(data: stderrData, encoding: .utf8)?
                    .trimmingCharacters(in: .whitespacesAndNewlines),
                   !stderrText.isEmpty {
                    continuation.yield(StreamEvent(kind: .error, error: stderrText))
                }

                continuation.yield(StreamEvent(kind: .done))
                continuation.finish()
            }

            continuation.onTermination = { @Sendable _ in
                task.cancel()
                self.cancel()
            }
        }
    }

    public func cancel() {
        let process: Process? = lock.withLock {
            let process = currentProcess
            currentProcess = nil
            return process
        }
        guard let process, process.isRunning else { return }
        process.terminate()

        Task.detached {
            try? await Task.sleep(for: .seconds(5))
            if process.isRunning {
                kill(process.processIdentifier, SIGKILL)
            }
        }
    }

    public func shutdown() {
        cancel()
    }

    private func setCurrentProcess(_ process: Process) {
        lock.withLock {
            currentProcess = process
        }
    }

    private func clearCurrentProcess(_ process: Process) {
        lock.withLock {
            if currentProcess === process {
                currentProcess = nil
            }
        }
    }

    private func streamLines(
        from handle: FileHandle,
        onLine: (String) -> Void
    ) async throws {
        var buffer = Data()
        for try await byte in handle.bytes {
            if byte == 10 {
                if let line = String(data: buffer, encoding: .utf8) {
                    onLine(line.trimmingCharacters(in: .newlines))
                }
                buffer.removeAll(keepingCapacity: true)
            } else {
                buffer.append(byte)
            }
        }
        if !buffer.isEmpty, let line = String(data: buffer, encoding: .utf8) {
            onLine(line.trimmingCharacters(in: .newlines))
        }
    }

    private static func buildCLIEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        let extras = [
            "\(NSHomeDirectory())/.local/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin"
        ]
        var path = environment["PATH"] ?? ""
        for extra in extras where !path.split(separator: ":").contains(Substring(extra)) {
            path = "\(extra):\(path)"
        }
        environment["PATH"] = path
        return environment
    }
}

private extension NSLock {
    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock()
        defer { unlock() }
        return try body()
    }
}
