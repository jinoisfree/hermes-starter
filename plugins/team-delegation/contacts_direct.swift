import Contacts
import Foundation

let executorVersion = "contacts-direct-v2"
let null = NSNull()

struct ContactsFailure: Error {
    let code: String
    var fields: [String: Any] = [:]
}

func rejectNoteWrites(_ args: [String: Any]) throws {
    if args.keys.contains(where: { $0.lowercased().contains("note") }) {
        throw ContactsFailure(code: "CONTACTS_NOTE_WRITE_UNSUPPORTED")
    }
}

func digits(_ value: String) -> String { value.filter { $0.isNumber } }
func text(_ args: [String: Any], _ key: String) -> String {
    (args[key] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
}
func normalizedEmail(_ value: String) -> String {
    value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
}
func displayName(_ contact: CNContact) -> String {
    CNContactFormatter.string(from: contact, style: .fullName)
        ?? [contact.familyName, contact.givenName].filter { !$0.isEmpty }.joined(separator: " ")
}
func phoneLabel(_ label: String?) -> String {
    switch label {
    case CNLabelPhoneNumberMobile, CNLabelPhoneNumberiPhone: return "mobile"
    case CNLabelWork: return "work"
    case CNLabelPhoneNumberWorkFax: return "work_fax"
    case CNLabelPhoneNumberMain: return "main"
    default: return "other"
    }
}
func emailLabel(_ label: String?) -> String {
    switch label { case CNLabelWork: return "work"; case CNLabelHome: return "home"; default: return "other" }
}
func phoneValues(_ rows: [[String: String]]) -> [CNLabeledValue<CNPhoneNumber>] {
    let labels = ["mobile": CNLabelPhoneNumberMobile, "work": CNLabelWork,
                  "work_fax": CNLabelPhoneNumberWorkFax, "main": CNLabelPhoneNumberMain, "other": CNLabelOther]
    return rows.map { CNLabeledValue(label: labels[$0["label"] ?? "other"], value: CNPhoneNumber(stringValue: $0["value"]!)) }
}
func emailValues(_ rows: [[String: String]]) -> [CNLabeledValue<NSString>] {
    let labels = ["work": CNLabelWork, "home": CNLabelHome, "other": CNLabelOther]
    return rows.map { CNLabeledValue(label: labels[$0["label"] ?? "other"], value: $0["value"]! as NSString) }
}
func matchesQuery(_ contact: CNContact, _ query: String) -> Bool {
    if query.isEmpty { return true }
    let strings = [displayName(contact), contact.familyName, contact.givenName,
                   contact.organizationName] + contact.emailAddresses.map { $0.value as String }
    if strings.contains(where: { $0.localizedCaseInsensitiveContains(query) }) { return true }
    let number = digits(query)
    return !number.isEmpty && contact.phoneNumbers.contains { digits($0.value.stringValue).contains(number) }
}
func duplicates(_ contact: CNContact, phones: [[String: String]], emails: [[String: String]]) -> Bool {
    let mail = Set(emails.map { normalizedEmail($0["value"] ?? "") }.filter { !$0.isEmpty })
    if contact.emailAddresses.contains(where: { mail.contains(normalizedEmail($0.value as String)) }) { return true }
    return phones.contains { incoming in
        let number = digits(incoming["value"] ?? "")
        return !number.isEmpty && contact.phoneNumbers.contains {
            number == digits($0.value.stringValue) &&
                (incoming["label"] == "mobile" || phoneLabel($0.label) == "mobile")
        }
    }
}

final class Executor {
    let store = CNContactStore()
    var notesAvailable = true
    var before: Any = null
    var after: Any = null
    var mutationCalls = 0
    var applied = false
    var action = ""

    func envelope(_ fields: [String: Any]) -> [String: Any] {
        var value: [String: Any] = ["before": before, "after": after, "executorVersion": executorVersion,
                                    "action": action, "mutation_calls": mutationCalls, "applied": applied]
        value.merge(fields) { _, new in new }
        return value
    }
    func status() -> String {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .notDetermined: return "notDetermined"
        case .restricted: return "restricted"
        case .denied: return "denied"
        case .authorized: return "authorized"
        @unknown default: return "limited_or_unknown"
        }
    }
    func keys() -> [CNKeyDescriptor] {
        var result: [CNKeyDescriptor] = [CNContactIdentifierKey, CNContactFamilyNameKey, CNContactGivenNameKey,
            CNContactOrganizationNameKey, CNContactDepartmentNameKey, CNContactJobTitleKey,
            CNContactPhoneNumbersKey, CNContactEmailAddressesKey, CNContactPostalAddressesKey,
            CNContactImageDataAvailableKey, CNContactTypeKey, CNContactNamePrefixKey, CNContactMiddleNameKey,
            CNContactPreviousFamilyNameKey, CNContactNameSuffixKey, CNContactNicknameKey,
            CNContactPhoneticGivenNameKey, CNContactPhoneticMiddleNameKey, CNContactPhoneticFamilyNameKey,
            CNContactPhoneticOrganizationNameKey, CNContactBirthdayKey, CNContactNonGregorianBirthdayKey,
            CNContactImageDataKey, CNContactThumbnailImageDataKey, CNContactDatesKey, CNContactUrlAddressesKey,
            CNContactRelationsKey, CNContactSocialProfilesKey, CNContactInstantMessageAddressesKey].map { $0 as CNKeyDescriptor }
        result.append(CNContactFormatter.descriptorForRequiredKeys(for: .fullName))
        if notesAvailable { result.append(CNContactNoteKey as CNKeyDescriptor) }
        return result
    }
    func fetch(_ identifier: String? = nil) throws -> [CNContact] {
        func read() throws -> [CNContact] {
            let request = CNContactFetchRequest(keysToFetch: keys())
            if let identifier { request.predicate = CNContact.predicateForContacts(withIdentifiers: [identifier]) }
            var rows: [CNContact] = []
            try store.enumerateContacts(with: request) { contact, _ in rows.append(contact) }
            return rows
        }
        do { return try read() }
        catch let error as NSError where error.domain == CNErrorDomain && error.code == CNError.Code.unauthorizedKeys.rawValue {
            // macOS can protect note reads with an additional entitlement; snapshots expose availability.
            notesAvailable = false
            return try read()
        }
    }
    func snapshot(_ contact: CNContact) throws -> [String: Any] {
        let containers = try store.containers(matching: CNContainer.predicateForContainerOfContact(withIdentifier: contact.identifier))
        return ["identifier": contact.identifier, "display_name": displayName(contact),
                "family_name": contact.familyName, "given_name": contact.givenName,
                "organization": contact.organizationName, "department": contact.departmentName,
                "job_title": contact.jobTitle,
                "phones": contact.phoneNumbers.map { ["label": phoneLabel($0.label), "value": $0.value.stringValue] },
                "emails": contact.emailAddresses.map { ["label": emailLabel($0.label), "value": $0.value as String] },
                "addresses": contact.postalAddresses.map { row -> [String: String] in
                    let a = row.value
                    return ["label": row.label ?? "other", "street": a.street, "city": a.city,
                            "state": a.state, "postal_code": a.postalCode, "country": a.country]
                },
                "note": notesAvailable && contact.isKeyAvailable(CNContactNoteKey) ? contact.note as Any : null,
                "note_available": notesAvailable, "has_image": contact.imageDataAvailable,
                "container_names": containers.map { $0.name }]
    }
    func apply(_ args: [String: Any], to contact: CNMutableContact) throws {
        try rejectNoteWrites(args)
        if let v = args["family_name"] as? String { contact.familyName = v }
        if let v = args["given_name"] as? String { contact.givenName = v }
        if let v = args["organization"] as? String { contact.organizationName = v }
        if let v = args["department"] as? String { contact.departmentName = v }
        if let v = args["job_title"] as? String { contact.jobTitle = v }
        for key in ["phones", "replace_phones", "add_phones"] {
            if let rows = args[key] as? [[String: String]] {
                let values = phoneValues(rows)
                if key == "add_phones" {
                    contact.phoneNumbers += values.filter { new in !contact.phoneNumbers.contains { digits($0.value.stringValue) == digits(new.value.stringValue) } }
                } else { contact.phoneNumbers = values }
            }
        }
        for key in ["emails", "replace_emails", "add_emails"] {
            if let rows = args[key] as? [[String: String]] {
                let values = emailValues(rows)
                if key == "add_emails" {
                    contact.emailAddresses += values.filter { new in !contact.emailAddresses.contains { normalizedEmail($0.value as String) == normalizedEmail(new.value as String) } }
                } else { contact.emailAddresses = values }
            }
        }
        if let a = args["address"] as? [String: String] {
            let address = (contact.postalAddresses.first?.value.mutableCopy() as? CNMutablePostalAddress) ?? CNMutablePostalAddress()
            if let v = a["street"] { address.street = v }
            if let v = a["city"] { address.city = v }
            if let v = a["state"] { address.state = v }
            if let v = a["postal_code"] { address.postalCode = v }
            if let v = a["country"] { address.country = v }
            let old = contact.postalAddresses.first
            let value = old?.settingValue(address) ?? CNLabeledValue(label: CNLabelWork, value: address as CNPostalAddress)
            contact.postalAddresses = [value] + Array(contact.postalAddresses.dropFirst())
        }
        if let path = args["image_path"] as? String {
            let url = URL(fileURLWithPath: path)
            let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            guard size > 0 && size <= 5 * 1024 * 1024 else { throw ContactsFailure(code: "CONTACTS_IMAGE_TOO_LARGE_OR_EMPTY") }
            let data = try Data(contentsOf: url)
            guard data.count <= 5 * 1024 * 1024 else { throw ContactsFailure(code: "CONTACTS_IMAGE_TOO_LARGE_OR_EMPTY") }
            contact.imageData = data
        }
        guard !contact.familyName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ||
                !contact.givenName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw ContactsFailure(code: "CONTACTS_NAME_REQUIRED")
        }
    }
    func commit(_ request: CNSaveRequest, using execute: ((CNSaveRequest) throws -> Void)? = nil) throws {
        mutationCalls = 1
        do {
            if let execute { try execute(request) }
            else { try store.execute(request) }
        } catch {
            // The save transaction failed. A later readback error must keep applied=true instead.
            mutationCalls = 0
            applied = false
            after = null
            throw error
        }
        applied = true
    }
    func members(_ identifier: String) throws -> [CNContact] {
        let request = CNContactFetchRequest(keysToFetch: keys())
        request.predicate = CNContact.predicateForContactsInGroup(withIdentifier: identifier)
        var rows: [CNContact] = []
        do { try store.enumerateContacts(with: request) { contact, _ in rows.append(contact) } }
        catch let error as NSError where notesAvailable && error.domain == CNErrorDomain && error.code == CNError.Code.unauthorizedKeys.rawValue {
            notesAvailable = false
            return try members(identifier)
        }
        return rows
    }
    func group(_ identifier: String) throws -> CNGroup {
        guard let found = try store.groups(matching: CNGroup.predicateForGroups(withIdentifiers: [identifier])).first else {
            throw ContactsFailure(code: "CONTACTS_GROUP_NOT_FOUND")
        }
        return found
    }
    func groupSnapshot(_ group: CNGroup) throws -> [String: Any] {
        let containers = try store.containers(matching: CNContainer.predicateForContainerOfGroup(withIdentifier: group.identifier))
        let ids = try members(group.identifier).map { $0.identifier }.sorted()
        return ["identifier": group.identifier, "name": group.name, "member_count": ids.count,
                "member_ids": ids, "container_name": containers.first?.name ?? ""]
    }
    func vcard(_ contacts: [CNContact]) throws -> Data {
        // Populate all serializable keys, but never request or save an entitled note value.
        let clean = contacts.map { contact -> CNContact in
            let value = contact.mutableCopy() as! CNMutableContact
            value.note = "" // In-memory serialization only, never a store update.
            return value
        }
        guard clean.allSatisfy({ $0.areKeysAvailable([CNContactVCardSerialization.descriptorForRequiredKeys()]) }) else {
            throw ContactsFailure(code: "CONTACTS_EXPORT_KEYS_UNAVAILABLE")
        }
        return try CNContactVCardSerialization.data(with: clean)
    }
    func page(_ rows: [CNContact], _ args: [String: Any]) throws -> [String: Any] {
        let sorted = rows.sorted { displayName($0).localizedStandardCompare(displayName($1)) == .orderedAscending }
        let offset = max(0, args["offset"] as? Int ?? 0)
        let limit = min(100, max(1, args["limit"] as? Int ?? 50))
        let selected = sorted.dropFirst(offset).prefix(limit)
        let next: Any = offset + selected.count < sorted.count ? offset + selected.count : null
        return envelope(["ok": true, "items": try selected.map { try snapshot($0) }, "total": sorted.count, "next_offset": next])
    }
    func checkBackup(_ args: [String: Any], data: Data) throws {
        guard let path = args["_backup_path"] as? String,
              let saved = try? Data(contentsOf: URL(fileURLWithPath: path)), saved == data else {
            throw ContactsFailure(code: "CONTACTS_BACKUP_MISSING_OR_STALE")
        }
    }
    func importedContact(_ item: CNContact) -> CNMutableContact {
        let clean = CNMutableContact()
        // Assign only writable vCard fields. In particular, never assign even an empty NOTE for saving.
        let properties = [CNContactNamePrefixKey, CNContactGivenNameKey, CNContactMiddleNameKey,
            CNContactFamilyNameKey, CNContactPreviousFamilyNameKey, CNContactNameSuffixKey, CNContactNicknameKey,
            CNContactPhoneticGivenNameKey, CNContactPhoneticMiddleNameKey, CNContactPhoneticFamilyNameKey,
            CNContactPhoneticOrganizationNameKey, CNContactOrganizationNameKey, CNContactDepartmentNameKey,
            CNContactJobTitleKey, CNContactTypeKey, CNContactBirthdayKey, CNContactNonGregorianBirthdayKey,
            CNContactImageDataKey, CNContactPhoneNumbersKey, CNContactEmailAddressesKey, CNContactPostalAddressesKey,
            CNContactDatesKey, CNContactUrlAddressesKey, CNContactRelationsKey, CNContactSocialProfilesKey,
            CNContactInstantMessageAddressesKey]
        for key in properties where item.isKeyAvailable(key) { clean.setValue(item.value(forKey: key), forKey: key) }
        return clean
    }
    func importCards(_ args: [String: Any]) throws -> [String: Any] {
        let url = URL(fileURLWithPath: text(args, "file_path"))
        let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
        guard size > 0 && size <= 5 * 1024 * 1024 else { throw ContactsFailure(code: "CONTACTS_IMPORT_TOO_LARGE_OR_EMPTY") }
        let data = try Data(contentsOf: url)
        guard data.count <= 5 * 1024 * 1024 else { throw ContactsFailure(code: "CONTACTS_IMPORT_TOO_LARGE_OR_EMPTY") }
        let input = try CNContactVCardSerialization.contacts(with: data)
        let groupID = text(args, "group_id")
        let destination = groupID.isEmpty ? nil : try group(groupID)
        let container = try destination.flatMap { try store.containers(matching: CNContainer.predicateForContainerOfGroup(withIdentifier: $0.identifier)).first?.identifier }
        var existing = try fetch()
        var created: [CNMutableContact] = []
        var skipped: [[String: Any]] = []
        let save = CNSaveRequest()
        for (index, item) in input.enumerated() {
            let phones = item.phoneNumbers.map { ["label": phoneLabel($0.label), "value": $0.value.stringValue] }
            let emails = item.emailAddresses.map { ["label": emailLabel($0.label), "value": $0.value as String] }
            let matches = existing.filter { duplicates($0, phones: phones, emails: emails) }
            if args["allow_duplicate"] as? Bool != true && !matches.isEmpty {
                skipped.append(["index": index, "display_name": displayName(item), "matching_ids": matches.map { $0.identifier }])
                continue
            }
            let contact = importedContact(item)
            save.add(contact, toContainerWithIdentifier: container)
            if let destination { save.addMember(contact, to: destination) }
            existing.append(contact); created.append(contact)
        }
        before = ["import_count": input.count]
        if !created.isEmpty { try commit(save) }
        after = try created.map { contact -> [String: Any] in
            guard let value = try fetch(contact.identifier).first else { throw ContactsFailure(code: "CONTACTS_READBACK_FAILED") }
            return try snapshot(value)
        }
        return envelope(["ok": true, "affected": after, "skipped_duplicates": skipped, "notes_included": false])
    }
    func run(_ args: [String: Any]) async throws -> [String: Any] {
        action = text(args, "action")
        guard ["access", "search", "get", "list", "groups", "group_members", "containers", "export_vcf", "create", "update", "delete", "group_create", "group_rename", "group_delete", "group_add", "group_remove", "import_vcf"].contains(action) else {
            throw ContactsFailure(code: "CONTACTS_ACTION_REFUSED")
        }
        if action == "create" || action == "update" { try rejectNoteWrites(args) }
        if status() == "notDetermined" { _ = try? await store.requestAccess(for: .contacts) }
        guard status() == "authorized" else {
            throw ContactsFailure(code: "CONTACTS_PERMISSION_REQUIRED", fields: ["authorization_status": status()])
        }
        if action == "access" { return envelope(["ok": true, "authorization_status": status()]) }
        if action == "containers" {
            let items = try store.containers(matching: nil).map {
                ["identifier": $0.identifier, "name": $0.name,
                 "type": $0.type == .local ? "local" : $0.type == .exchange ? "exchange" : "cardDAV",
                 "is_default": $0.identifier == store.defaultContainerIdentifier()] as [String: Any]
            }
            return envelope(["ok": true, "items": items])
        }
        if action == "groups" { return envelope(["ok": true, "items": try store.groups(matching: nil).map { try groupSnapshot($0) }]) }
        if action == "list" || action == "group_members" {
            let groupID = text(args, "group_id")
            if action == "group_members" && groupID.isEmpty { throw ContactsFailure(code: "CONTACTS_GROUP_ID_REQUIRED") }
            if !groupID.isEmpty { _ = try group(groupID) }
            let rows = try groupID.isEmpty ? fetch() : members(groupID)
            let organization = text(args, "organization")
            return try page(rows.filter { organization.isEmpty || $0.organizationName.localizedCaseInsensitiveContains(organization) }, args)
        }
        if action == "export_vcf" {
            let ids = args["contact_ids"] as? [String] ?? []
            let groupID = text(args, "group_id")
            guard (ids.isEmpty != groupID.isEmpty) && ids.count <= 500 else { throw ContactsFailure(code: "CONTACTS_EXPORT_TARGET_REQUIRED") }
            let rows: [CNContact]
            if !groupID.isEmpty { _ = try group(groupID); rows = try members(groupID) }
            else {
                rows = try ids.map { id in
                    guard let value = try fetch(id).first else { throw ContactsFailure(code: "CONTACTS_NOT_FOUND") }
                    return value
                }
            }
            return envelope(["ok": true, "vcf_base64": try vcard(rows).base64EncodedString(),
                             "items": try rows.map { try snapshot($0) }, "notes_included": false])
        }
        if action == "import_vcf" { return try importCards(args) }
        if action.hasPrefix("group_") {
            let save = CNSaveRequest()
            if action == "group_create" {
                let name = text(args, "name")
                guard !name.isEmpty else { throw ContactsFailure(code: "CONTACTS_GROUP_NAME_REQUIRED") }
                if try store.groups(matching: nil).contains(where: { $0.name.localizedCaseInsensitiveCompare(name) == .orderedSame }) {
                    throw ContactsFailure(code: "CONTACTS_GROUP_EXISTS")
                }
                let created = CNMutableGroup(); created.name = name
                save.add(created, toContainerWithIdentifier: nil); try commit(save)
                after = try groupSnapshot(group(created.identifier))
                return envelope(["ok": true, "target_id": created.identifier])
            }
            let found = try group(text(args, "group_id"))
            before = try groupSnapshot(found)
            if action == "group_add" || action == "group_remove" {
                let ids = args["contact_ids"] as? [String] ?? []
                guard !ids.isEmpty && ids.count <= 200 else { throw ContactsFailure(code: "CONTACTS_IDS_REQUIRED") }
                let memberIDs = Set((before as! [String: Any])["member_ids"] as! [String])
                var changed: [String] = []; var skipped: [String] = []
                for id in Set(ids).sorted() {
                    guard let contact = try fetch(id).first else { throw ContactsFailure(code: "CONTACTS_NOT_FOUND") }
                    if memberIDs.contains(id) == (action == "group_add") { skipped.append(id); continue }
                    if action == "group_add" { save.addMember(contact, to: found) }
                    else { save.removeMember(contact, from: found) }
                    changed.append(id)
                }
                if !changed.isEmpty { try commit(save) }
                after = try groupSnapshot(group(found.identifier))
                return envelope(["ok": true, "affected": changed, "skipped": skipped])
            }
            guard !text(args, "expected_name").isEmpty && text(args, "expected_name") == found.name else { throw ContactsFailure(code: "CONTACTS_IDENTITY_MISMATCH") }
            if action == "group_delete" {
                guard let path = args["_backup_path"] as? String,
                      let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
                      let backup = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      backup["identifier"] as? String == found.identifier, backup["name"] as? String == found.name,
                      (backup["member_ids"] as? [String])?.sorted() == (before as! [String: Any])["member_ids"] as? [String] else {
                    throw ContactsFailure(code: "CONTACTS_BACKUP_MISSING_OR_STALE")
                }
                save.delete(found.mutableCopy() as! CNMutableGroup); try commit(save)
                return envelope(["ok": true, "target_id": found.identifier, "members_deleted": false])
            }
            let name = text(args, "name")
            guard !name.isEmpty else { throw ContactsFailure(code: "CONTACTS_GROUP_NAME_REQUIRED") }
            if try store.groups(matching: nil).contains(where: { $0.identifier != found.identifier && $0.name.localizedCaseInsensitiveCompare(name) == .orderedSame }) {
                throw ContactsFailure(code: "CONTACTS_GROUP_EXISTS")
            }
            let changed = found.mutableCopy() as! CNMutableGroup; changed.name = name
            save.update(changed); try commit(save)
            after = try groupSnapshot(group(found.identifier))
            return envelope(["ok": true, "target_id": found.identifier])
        }
        if action == "search" {
            let limit = min(50, max(1, args["limit"] as? Int ?? 20))
            let matches = try fetch().filter { matchesQuery($0, text(args, "query")) }
            let items = try matches.prefix(limit).map { try snapshot($0) }
            return envelope(["ok": true, "items": items, "result_count": items.count, "truncated": matches.count > limit])
        }
        if action == "get" || action == "update" || action == "delete" {
            guard !text(args, "contact_id").isEmpty, let contact = try fetch(text(args, "contact_id")).first else {
                throw ContactsFailure(code: "CONTACTS_NOT_FOUND")
            }
            before = try snapshot(contact)
            if action == "get" { after = before; return envelope(["ok": true]) }
            guard !text(args, "expected_name").isEmpty && text(args, "expected_name") == displayName(contact) else {
                throw ContactsFailure(code: "CONTACTS_IDENTITY_MISMATCH")
            }
            if action == "delete" {
                try checkBackup(args, data: vcard([contact]))
                let save = CNSaveRequest(); save.delete(contact.mutableCopy() as! CNMutableContact)
                try commit(save)
                return envelope(["ok": true, "target_id": contact.identifier])
            }
            let mutable = contact.mutableCopy() as! CNMutableContact
            try apply(args, to: mutable)
            let save = CNSaveRequest(); save.update(mutable)
            try commit(save)
            guard let refreshed = try fetch(mutable.identifier).first else { throw ContactsFailure(code: "CONTACTS_READBACK_FAILED") }
            after = try snapshot(refreshed)
            return envelope(["ok": true, "target_id": mutable.identifier])
        }
        let existing = try fetch()
        let phones = args["phones"] as? [[String: String]] ?? []
        let emails = args["emails"] as? [[String: String]] ?? []
        let matches = existing.filter { duplicates($0, phones: phones, emails: emails) }
        if !matches.isEmpty && (args["allow_duplicate"] as? Bool != true) {
            throw ContactsFailure(code: "CONTACTS_DUPLICATE", fields: ["matches": try matches.map { try snapshot($0) }])
        }
        let contact = CNMutableContact()
        try apply(args, to: contact)
        let save = CNSaveRequest(); save.add(contact, toContainerWithIdentifier: nil)
        try commit(save)
        guard let refreshed = try fetch(contact.identifier).first else { throw ContactsFailure(code: "CONTACTS_READBACK_FAILED") }
        after = try snapshot(refreshed)
        return envelope(["ok": true, "target_id": contact.identifier])
    }
}

@main struct ContactsDirect {
    static func main() async {
        let executor = Executor()
        let response: [String: Any]
        do {
            guard CommandLine.arguments.count == 2,
                  let data = CommandLine.arguments[1].data(using: .utf8),
                  let args = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw ContactsFailure(code: "CONTACTS_INVALID_ARGUMENTS")
            }
            response = try await executor.run(args)
        } catch let error as ContactsFailure {
            response = executor.envelope(["ok": false, "error": error.code].merging(error.fields) { _, new in new })
        } catch let error as NSError {
            let code = error.domain == CNErrorDomain && error.code == CNError.Code.authorizationDenied.rawValue
                ? "CONTACTS_PERMISSION_REQUIRED" : "CONTACTS_NATIVE_ERROR"
            response = executor.envelope(["ok": false, "error": code, "authorization_status": executor.status(),
                                           "native_error_domain": error.domain, "native_error_code": error.code])
        }
        if let data = try? JSONSerialization.data(withJSONObject: response, options: [.sortedKeys]),
           let string = String(data: data, encoding: .utf8) { print(string) }
    }
}
