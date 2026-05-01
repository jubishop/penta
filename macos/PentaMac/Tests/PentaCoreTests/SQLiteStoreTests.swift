import Foundation
import PentaCore
import Testing

@Test func storePersistsMessagesAndSessions() async throws {
    let root = FileManager.default.temporaryDirectory
        .appendingPathComponent("PentaMacTests-\(UUID().uuidString)", isDirectory: true)
    let directory = root.appendingPathComponent("project", isDirectory: true)
    let storage = root.appendingPathComponent("storage", isDirectory: true)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }

    let store = SQLiteStore(directory: directory, storageRoot: storage)
    try await store.connect()

    _ = try await store.appendMessage(sender: "User", text: "hello")
    try await store.saveSession(agentName: "codex", sessionID: "thread-1")

    let rows = try await store.getMessages()
    #expect(rows.count == 1)
    #expect(rows[0].sender == "User")
    #expect(rows[0].text == "hello")
    #expect(try await store.loadSession(agentName: "codex") == "thread-1")
}
