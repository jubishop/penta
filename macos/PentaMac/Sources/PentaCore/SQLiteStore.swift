import Foundation
import GRDB

public enum PentaDatabaseError: Error, CustomStringConvertible, Sendable {
    case openFailed(String)
    case sqlite(message: String)
    case notConnected
    case invalidConversation(Int64)

    public var description: String {
        switch self {
        case let .openFailed(message):
            "Unable to open database: \(message)"
        case let .sqlite(message):
            "SQLite error: \(message)"
        case .notConnected:
            "Database is not connected"
        case let .invalidConversation(id):
            "Conversation \(id) does not exist"
        }
    }
}

public actor SQLiteStore {
    public static let schemaVersion = 1
    public static let maxMessages = 2_000

    private let directory: URL
    private let dbPath: URL
    private var dbPool: DatabasePool?
    private var lastDataVersion = 0
    private var lastSeenID: Int64 = 0
    private var localIDs = Set<Int64>()

    public private(set) var conversationID: Int64 = 1

    public init(directory: URL, storageRoot: URL? = nil) {
        let resolved = directory.resolvingSymlinksInPath().standardizedFileURL
        self.directory = resolved
        self.dbPath = PentaPaths.databasePath(for: resolved, storageRoot: storageRoot)
    }

    public func connect() throws {
        if dbPool != nil {
            return
        }

        try FileManager.default.createDirectory(
            at: dbPath.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )

        var configuration = Configuration()
        configuration.prepareDatabase { db in
            try db.execute(sql: "PRAGMA busy_timeout=5000")
            try db.execute(sql: "PRAGMA foreign_keys=ON")
        }
        let pool = try DatabasePool(path: dbPath.path, configuration: configuration)
        dbPool = pool

        let version = try read { db in
            try Int.fetchOne(db, sql: "PRAGMA user_version") ?? 0
        }
        if version == 0 {
            if try tableExists("messages") {
                try runMigrations(currentVersion: 0)
            } else {
                try execute(Self.createTablesSQL)
                try ensureDefaultConversation()
                try execute("PRAGMA user_version = \(Self.schemaVersion)")
            }
        } else {
            try runMigrations(currentVersion: version)
        }

        if let activeID = try read({ db in
            try Int64.fetchOne(db, sql: "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1")
        }) {
            conversationID = activeID
        }
        lastDataVersion = try read { db in
            try Int.fetchOne(db, sql: "PRAGMA data_version") ?? 0
        }
        lastSeenID = try maxMessageID()
    }

    public func close() {
        dbPool = nil
    }

    public func createConversation(title: String) throws -> Int64 {
        let now = isoString(Date())
        return try write { db in
            try db.execute(
                sql: "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
                arguments: [title, now, now]
            )
            return db.lastInsertedRowID
        }
    }

    public func listConversations() throws -> [ConversationInfo] {
        try read { db in
            try Row.fetchAll(
                db,
                sql: "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
            ).map { row in
                ConversationInfo(
                    id: row["id"],
                    title: row["title"],
                    createdAt: parseISODate(row["created_at"]) ?? Date(),
                    updatedAt: parseISODate(row["updated_at"]) ?? Date()
                )
            }
        }
    }

    public func conversationExists(_ id: Int64) throws -> Bool {
        try read { db in
            try Int.fetchOne(
                db,
                sql: "SELECT COUNT(*) FROM conversations WHERE id = ?",
                arguments: [id]
            ) ?? 0
        } > 0
    }

    public func deleteConversation(_ id: Int64) throws {
        guard try conversationExists(id) else {
            throw PentaDatabaseError.invalidConversation(id)
        }
        try write { db in
            try db.execute(sql: "DELETE FROM messages WHERE conversation_id = ?", arguments: [id])
            try db.execute(sql: "DELETE FROM sessions WHERE conversation_id = ?", arguments: [id])
            try db.execute(sql: "DELETE FROM conversations WHERE id = ?", arguments: [id])
        }
    }

    public func renameConversation(_ id: Int64, title: String) throws {
        guard try conversationExists(id) else {
            throw PentaDatabaseError.invalidConversation(id)
        }
        try write { db in
            try db.execute(sql: "UPDATE conversations SET title = ? WHERE id = ?", arguments: [title, id])
        }
    }

    public func setConversation(_ id: Int64) throws {
        guard try conversationExists(id) else {
            throw PentaDatabaseError.invalidConversation(id)
        }
        conversationID = id
        localIDs.removeAll()
        lastSeenID = try maxMessageID()
        lastDataVersion = try scalarInt("PRAGMA data_version")
    }

    public func appendMessage(sender: String, text: String) throws -> Int64 {
        let now = isoString(Date())
        let currentConversationID = conversationID
        let rowID = try write { db in
            try db.execute(
                sql: "INSERT INTO messages (conversation_id, sender, text, timestamp) VALUES (?, ?, ?, ?)",
                arguments: [currentConversationID, sender, text, now]
            )
            let rowID = db.lastInsertedRowID
            try db.execute(
                sql: "UPDATE conversations SET updated_at = ? WHERE id = ?",
                arguments: [now, currentConversationID]
            )
            return rowID
        }
        localIDs.insert(rowID)
        return rowID
    }

    public func getMessages(limit: Int = SQLiteStore.maxMessages) throws -> [StoredMessageRow] {
        let currentConversationID = conversationID
        let rows = try read { db in
            try Row.fetchAll(
                db,
                sql: """
                SELECT id, sender, text, timestamp FROM messages
                WHERE conversation_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                arguments: [currentConversationID, limit]
            ).map { row in
                StoredMessageRow(
                    id: row["id"],
                    sender: row["sender"],
                    text: row["text"],
                    timestamp: parseISODate(row["timestamp"]) ?? Date()
                )
            }
        }
        return Array(rows.reversed())
    }

    public func compact(maxMessages: Int = SQLiteStore.maxMessages) throws {
        let currentConversationID = conversationID
        try write { db in
            try db.execute(
                sql: """
                DELETE FROM messages WHERE conversation_id = ? AND id NOT IN
                (SELECT id FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?)
                """,
                arguments: [currentConversationID, currentConversationID, maxMessages]
            )
        }
    }

    public func saveSession(agentName: String, sessionID: String) throws {
        let currentConversationID = conversationID
        try write { db in
            try db.execute(
                sql: """
                INSERT OR REPLACE INTO sessions (agent_name, conversation_id, session_id)
                VALUES (?, ?, ?)
                """,
                arguments: [agentName, currentConversationID, sessionID]
            )
        }
    }

    public func loadSession(agentName: String) throws -> String? {
        let currentConversationID = conversationID
        return try read { db in
            try String.fetchOne(
                db,
                sql: "SELECT session_id FROM sessions WHERE agent_name = ? AND conversation_id = ?",
                arguments: [agentName, currentConversationID]
            )
        }
    }

    public func checkExternalChanges() throws -> [StoredMessageRow] {
        let dataVersion = try read { db in
            try Int.fetchOne(db, sql: "PRAGMA data_version") ?? 0
        }
        if dataVersion == lastDataVersion {
            return []
        }
        lastDataVersion = dataVersion

        let currentConversationID = conversationID
        let currentLastSeenID = lastSeenID
        let rows = try read { db in
            try Row.fetchAll(
                db,
                sql: """
                SELECT id, sender, text, timestamp FROM messages
                WHERE conversation_id = ? AND id > ? ORDER BY id
                """,
                arguments: [currentConversationID, currentLastSeenID]
            ).map { row in
                StoredMessageRow(
                    id: row["id"],
                    sender: row["sender"],
                    text: row["text"],
                    timestamp: parseISODate(row["timestamp"]) ?? Date()
                )
            }
        }

        if let last = rows.last {
            lastSeenID = last.id
        }
        let external = rows.filter { !localIDs.contains($0.id) }
        localIDs = localIDs.filter { $0 > lastSeenID }
        return external
    }

    private func runMigrations(currentVersion: Int) throws {
        var current = currentVersion
        if current < 1 {
            try execute("PRAGMA foreign_keys=OFF")
            try migrateV1()
            try execute("PRAGMA foreign_keys=ON")
            try execute("PRAGMA user_version = 1")
            current = 1
        }
        if current < Self.schemaVersion {
            throw PentaDatabaseError.sqlite(message: "Unsupported schema migration state")
        }
    }

    private func migrateV1() throws {
        let now = isoString(Date())
        let createdAt = try read { db in
            try String.fetchOne(db, sql: "SELECT MIN(timestamp) FROM messages")
        } ?? now

        try execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)

        if try scalarInt("SELECT COUNT(*) FROM conversations WHERE id = 1") == 0 {
            try execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (1, ?, ?, ?)",
                ["Default", createdAt, now]
            )
        }

        if try !hasColumn(table: "messages", column: "conversation_id") {
            try execute("""
                ALTER TABLE messages ADD COLUMN conversation_id INTEGER NOT NULL DEFAULT 1
                REFERENCES conversations(id);
                """)
        }

        try rebuildSessionsForV1()
        try execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_conversation
                ON sessions(conversation_id);
            """)
    }

    private func rebuildSessionsForV1() throws {
        let hasOld = try tableExists("sessions")
        let hasNew = try tableExists("sessions_new")

        if hasOld && !hasNew {
            try execute("""
                CREATE TABLE sessions_new (
                    agent_name TEXT NOT NULL,
                    conversation_id INTEGER NOT NULL DEFAULT 1 REFERENCES conversations(id),
                    session_id TEXT NOT NULL,
                    PRIMARY KEY (agent_name, conversation_id)
                );
                INSERT OR IGNORE INTO sessions_new (agent_name, conversation_id, session_id)
                    SELECT agent_name, 1, session_id FROM sessions;
                DROP TABLE sessions;
                ALTER TABLE sessions_new RENAME TO sessions;
                """)
        } else if hasNew && !hasOld {
            try execute("ALTER TABLE sessions_new RENAME TO sessions;")
        } else if hasNew && hasOld {
            try execute("""
                DROP TABLE sessions;
                ALTER TABLE sessions_new RENAME TO sessions;
                """)
        } else {
            try execute("""
                CREATE TABLE sessions (
                    agent_name TEXT NOT NULL,
                    conversation_id INTEGER NOT NULL DEFAULT 1 REFERENCES conversations(id),
                    session_id TEXT NOT NULL,
                    PRIMARY KEY (agent_name, conversation_id)
                );
                """)
        }
    }

    private func ensureDefaultConversation() throws {
        if try scalarInt("SELECT COUNT(*) FROM conversations") == 0 {
            let now = isoString(Date())
            try execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (1, ?, ?, ?)",
                ["Default", now, now]
            )
        }
    }

    private func maxMessageID() throws -> Int64 {
        try scalarOptionalInt64(
            "SELECT MAX(id) FROM messages WHERE conversation_id = ?",
            [conversationID]
        ) ?? 0
    }

    private func tableExists(_ table: String) throws -> Bool {
        try scalarInt(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = ?",
            [table]
        ) > 0
    }

    private func hasColumn(table: String, column: String) throws -> Bool {
        let rows = try read { db in
            try Row.fetchAll(db, sql: "PRAGMA table_info(\(table))").map { row -> String in
                row["name"]
            }
        }
        return rows.contains(column)
    }

    private func requirePool() throws -> DatabasePool {
        guard let dbPool else {
            throw PentaDatabaseError.notConnected
        }
        return dbPool
    }

    private func read<T>(_ body: (Database) throws -> T) throws -> T {
        try requirePool().read(body)
    }

    private func write<T>(_ body: (Database) throws -> T) throws -> T {
        try requirePool().write(body)
    }

    private func execute(_ sql: String, _ arguments: StatementArguments = []) throws {
        try write { db in
            try db.execute(sql: sql, arguments: arguments)
        }
    }

    private func scalarInt(_ sql: String, _ arguments: StatementArguments = []) throws -> Int {
        try read { db in
            try Int.fetchOne(db, sql: sql, arguments: arguments) ?? 0
        }
    }

    private func scalarOptionalInt64(_ sql: String, _ arguments: StatementArguments = []) throws -> Int64? {
        try read { db in
            try Int64.fetchOne(db, sql: sql, arguments: arguments)
        }
    }

    private static let createTablesSQL = """
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL DEFAULT 1 REFERENCES conversations(id),
            sender TEXT NOT NULL,
            text TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages(conversation_id);
        CREATE TABLE IF NOT EXISTS sessions (
            agent_name TEXT NOT NULL,
            conversation_id INTEGER NOT NULL DEFAULT 1 REFERENCES conversations(id),
            session_id TEXT NOT NULL,
            PRIMARY KEY (agent_name, conversation_id)
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_conversation
            ON sessions(conversation_id);
        """
}

private func isoString(_ date: Date) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    formatter.timeZone = TimeZone(secondsFromGMT: 0)
    return formatter.string(from: date)
}

private func parseISODate(_ value: String) -> Date? {
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = fractional.date(from: value) {
        return date
    }

    let plain = ISO8601DateFormatter()
    plain.formatOptions = [.withInternetDateTime]
    return plain.date(from: value)
}
