// Read-only Messages (SMS / RCS / iMessage) lookup for personal-secretary.
//
// Reads ~/Library/Messages/chat.db and prints JSON. It never writes, sends, or opens links.
// The built binary is the only thing that needs Full Disk Access.
//
// Usage: hermes-messages-reader '<json options>'
//   since_hours, limit, query, sender, service (all|sms|imessage), include_sent, exclude, db
// Exit codes: 0 ok, 64 bad options, 66 database missing, 77 permission denied, 70 other.

import Foundation
import SQLite3

let appleEpochOffset: Double = 978_307_200
let maxTextLength = 2000
let rowCap: Int32 = 5000

func emit(_ payload: [String: Any], code: Int32) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])) ?? Data("{}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(code)
}

func fail(_ error: String, code: Int32) -> Never {
    emit(["ok": false, "error": error], code: code)
}

// Newer macOS versions keep the text only in attributedBody (a typedstream archive).
// The string sits after the NSString class marker: 5 header bytes, a length, then UTF-8.
func decodeAttributedBody(_ data: Data) -> String? {
    guard let marker = data.range(of: Data("NSString".utf8)) else { return nil }
    var index = marker.upperBound + 5
    guard index < data.count else { return nil }
    var length = Int(data[index])
    index += 1
    if length == 0x81 {
        guard index + 2 <= data.count else { return nil }
        length = Int(data[index]) | Int(data[index + 1]) << 8
        index += 2
    } else if length == 0x82 {
        guard index + 4 <= data.count else { return nil }
        length = (0..<4).reduce(0) { $0 | Int(data[index + $1]) << (8 * $1) }
        index += 4
    }
    guard length > 0, index + length <= data.count else { return nil }
    return String(data: data.subdata(in: index..<(index + length)), encoding: .utf8)
}

func column(_ statement: OpaquePointer?, _ index: Int32) -> String {
    guard let raw = sqlite3_column_text(statement, index) else { return "" }
    return String(cString: raw)
}

func digits(_ value: String) -> String {
    return value.filter { $0.isNumber }
}

guard CommandLine.arguments.count == 2,
      let optionData = CommandLine.arguments[1].data(using: .utf8),
      let options = (try? JSONSerialization.jsonObject(with: optionData)) as? [String: Any]
else { fail("MESSAGES_BAD_OPTIONS", code: 64) }

let sinceHours = min(720.0, max(0.1, (options["since_hours"] as? NSNumber)?.doubleValue ?? 24))
let limit = min(200, max(1, (options["limit"] as? NSNumber)?.intValue ?? 50))
let query = ((options["query"] as? String) ?? "").trimmingCharacters(in: .whitespaces).lowercased()
let sender = ((options["sender"] as? String) ?? "").trimmingCharacters(in: .whitespaces).lowercased()
let service = ((options["service"] as? String) ?? "all").lowercased()
let includeSent = (options["include_sent"] as? Bool) ?? false
let excluded = Set(((options["exclude"] as? [String]) ?? []).map { $0.lowercased() })
let databasePath = (options["db"] as? String) ?? (NSHomeDirectory() + "/Library/Messages/chat.db")
guard ["all", "sms", "imessage"].contains(service) else { fail("MESSAGES_BAD_OPTIONS", code: 64) }

guard FileManager.default.fileExists(atPath: databasePath) else { fail("MESSAGES_DATABASE_MISSING", code: 66) }

func databaseFailure(_ db: OpaquePointer?) -> Never {
    let code = sqlite3_errcode(db)
    let message = String(cString: sqlite3_errmsg(db))
    if code == SQLITE_AUTH || code == SQLITE_CANTOPEN || code == SQLITE_PERM
        || message.contains("authorization denied") {
        fail("MESSAGES_PERMISSION_DENIED", code: 77)
    }
    fail("MESSAGES_DATABASE_ERROR:\(code)", code: 70)
}

var db: OpaquePointer?
let uri = "file:" + (databasePath.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? databasePath) + "?mode=ro"
if sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil) != SQLITE_OK {
    databaseFailure(db)
}
defer { sqlite3_close(db) }
sqlite3_busy_timeout(db, 3000)

let sql = """
SELECT m.ROWID, m.guid, m.text, m.attributedBody, m.date, m.service, m.is_from_me,
       h.id, c.chat_identifier, c.display_name, c.style, m.cache_has_attachments
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join j ON j.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = j.chat_id
WHERE m.date >= ?1 AND m.item_type = 0 AND m.associated_message_type = 0
  AND (?2 = 1 OR m.is_from_me = 0)
ORDER BY m.date DESC
LIMIT ?3
"""
var statement: OpaquePointer?
if sqlite3_prepare_v2(db, sql, -1, &statement, nil) != SQLITE_OK { databaseFailure(db) }
defer { sqlite3_finalize(statement) }

let sinceUnix = Date().timeIntervalSince1970 - sinceHours * 3600
sqlite3_bind_int64(statement, 1, Int64((sinceUnix - appleEpochOffset) * 1_000_000_000))
sqlite3_bind_int(statement, 2, includeSent ? 1 : 0)
sqlite3_bind_int(statement, 3, rowCap)

let formatter = ISO8601DateFormatter()
formatter.timeZone = TimeZone(identifier: "Asia/Seoul")
formatter.formatOptions = [.withInternetDateTime]

var items: [[String: Any]] = []
var seen = Set<Int64>()
var scanned = 0
var matched = 0
var step = sqlite3_step(statement)
while step == SQLITE_ROW {
    defer { step = sqlite3_step(statement) }
    let rowID = sqlite3_column_int64(statement, 0)
    guard seen.insert(rowID).inserted else { continue }
    scanned += 1

    var text = column(statement, 2)
    if text.isEmpty, let bytes = sqlite3_column_blob(statement, 3) {
        let body = Data(bytes: bytes, count: Int(sqlite3_column_bytes(statement, 3)))
        text = decodeAttributedBody(body) ?? ""
    }
    text = text.replacingOccurrences(of: "\u{FFFC}", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
    let hasAttachments = sqlite3_column_int(statement, 11) != 0
    if text.isEmpty && !hasAttachments { continue }

    let messageService = column(statement, 5)
    let isIMessage = messageService.lowercased() == "imessage"
    if service == "sms" && isIMessage { continue }
    if service == "imessage" && !isIMessage { continue }

    let handle = column(statement, 7)
    let chatIdentifier = column(statement, 8)
    if excluded.contains(handle.lowercased()) || excluded.contains(chatIdentifier.lowercased()) { continue }
    let chatName = column(statement, 9)
    if !sender.isEmpty {
        let haystack = [handle, chatIdentifier, chatName].joined(separator: " ").lowercased()
        // REPLACE_PHONE is stored as REPLACE_PHONE: compare without the national leading zero.
        var senderDigits = digits(sender)
        if senderDigits.hasPrefix("0") { senderDigits.removeFirst() }
        let digitMatch = senderDigits.count >= 4 && digits(haystack).contains(senderDigits)
        if !haystack.contains(sender) && !digitMatch { continue }
    }
    if !query.isEmpty && !text.lowercased().contains(query) { continue }

    matched += 1
    if items.count >= limit { continue }
    let rawDate = Double(sqlite3_column_int64(statement, 4))
    let seconds = rawDate > 100_000_000_000 ? rawDate / 1_000_000_000 : rawDate
    let fromMe = sqlite3_column_int(statement, 6) != 0
    var item: [String: Any] = [
        "messageIdentifier": column(statement, 1),
        "received_at": formatter.string(from: Date(timeIntervalSince1970: seconds + appleEpochOffset)),
        "sender": fromMe ? "me" : handle,
        "from_me": fromMe,
        "service": messageService,
        "group": sqlite3_column_int(statement, 10) == 43,
        "has_attachments": hasAttachments,
        "text": String(text.prefix(maxTextLength)),
        "text_truncated": text.count > maxTextLength,
    ]
    if !chatName.isEmpty { item["chat_name"] = chatName }
    if fromMe { item["chat"] = chatIdentifier }
    items.append(item)
}
if step != SQLITE_DONE { databaseFailure(db) }

emit([
    "ok": true,
    "items": items,
    "matched": matched,
    "scanned": scanned,
    "truncated": matched > items.count || scanned >= Int(rowCap),
    "since_hours": sinceHours,
], code: 0)
