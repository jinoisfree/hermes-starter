// Metadata enumeration only, including Recently Deleted for an informational result.
function run() {
    const rows = [];
    Application('Notes').accounts().forEach(a => a.folders().forEach(f => {
        const scope = {id:f.id(), name:f.name(), account:a.name(), account_id:a.id()};
        f.notes().forEach(n => rows.push({source_id:n.id(), scope:scope,
            identity:{title:n.name(), created_at:n.creationDate().toISOString(),
                folder_id:scope.id, folder_name:scope.name, account_id:scope.account_id}}));
    }));
    return JSON.stringify(rows);
}
